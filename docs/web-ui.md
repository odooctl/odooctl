# odooctl Web UI

Vanilla JS single-page application served by `odooctl serve`.

## Architecture

The SPA is a single `dist/` directory of hand-authored HTML, CSS, and plain
JavaScript — no build tooling, no bundler, no Node.js required.

**How it is served:**
`odooctl/api/app.py` registers a catch-all FastAPI route
(`GET /{full_path:path}`) after all API routes. The handler resolves the
requested path inside the dist directory and:

- Returns `FileResponse` for known asset files (`app.js`, `style.css`).
- Returns `HTMLResponse` (index.html) for everything else — client-side hash
  routes, unknown paths, and any path that resolves outside the dist directory
  (traversal attempts).

A `relative_to` guard rejects any resolved path that escapes the dist
directory before serving, so path traversal cannot reach files outside it.

**API routes always take priority.** FastAPI's router matches `/projects` and
`/operations` before the catch-all route is reached, so authenticated API
calls always hit the API layer regardless of whether a static SPA is mounted.

**API-only data access.** The SPA talks exclusively to the odooctl REST API.
It has no direct Docker, Postgres, filesystem, or Python service access.

## Files

```
odooctl/web/
├── __init__.py          Python package init (runner contract hook)
├── README.md            Developer notes
└── dist/
    ├── index.html       SPA entry point
    ├── app.js           Application JavaScript (vanilla, no framework)
    └── style.css        Styles (custom properties + flexbox/grid)
```

Edit `dist/app.js` and `dist/style.css` directly — no build step needed.
Note: `index.html` is read once when the server starts and cached in memory
for the SPA fallback (`odooctl serve` is a long-running process), so changes
to `index.html` require a server restart; `app.js` and `style.css` are served
from disk on each request.

## Running the UI

```bash
# Install API extras
pip install odooctl[api]

# Start server (auto-serves bundled SPA at /)
export ODOOCTL_API_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
odooctl serve

# Override with a custom dist directory (for local development)
ODOOCTL_API_KEY=mysecret odooctl serve --static-dir path/to/custom/dist

# Custom port (keep the default localhost-only bind)
ODOOCTL_API_KEY=mysecret odooctl serve --host 127.0.0.1 --port 9000
```

> **Warning:** the server speaks plain HTTP. Do not expose `--host 0.0.0.0`
> directly: it puts bearer tokens on the wire unencrypted and lets anyone with
> a token enqueue privileged operations. The default is loopback-only.

### Remote access

For occasional access, keep the server bound to loopback and tunnel it:

```bash
ssh -L 8787:127.0.0.1:8787 operator@odoo-host
```

Then open `http://127.0.0.1:8787/` locally. For a persistent deployment,
place a TLS-terminating, authenticating reverse proxy in front of the service,
restrict its firewall scope to the proxy/Tailscale network, and name every
public host explicitly:

```bash
odooctl serve --host 0.0.0.0 --port 8787 \
  --allowed-host odooctl.ops.example.com
```

`--allowed-host` is repeatable but accepts exact hostnames/IP addresses only;
wildcards are rejected. Configure the proxy to forward the matching `Host`
header, terminate TLS, require its own authentication where appropriate, and
keep `ODOOCTL_API_KEY` in a root/service-account-readable environment file
(mode `0600`), never in the proxy config, command line, or repository.

For systemd, run `odooctl serve` as an unprivileged dedicated account with an
`EnvironmentFile=` containing the API key, `Restart=on-failure`, and a
firewall rule allowing only the reverse proxy or Tailscale interface. Run the
privileged `odooctl runner` separately; it alone needs Docker/filestore access.

### Troubleshooting

| Symptom | Cause and resolution |
| --- | --- |
| `Invalid host header` | The request host is not loopback or an explicit `--allowed-host`. Correct the reverse-proxy `Host` header and add only the exact public hostname. |
| `401 Unauthorized` | The UI token is absent, expired, malformed, or signed by a different `ODOOCTL_API_KEY`. Mint a new token with the server's key and paste it into the UI. |
| Dashboard does not show Odoo | Port `8787` is the odooctl dashboard/API. Odoo environment ports (often `8069` or a reverse-proxy port) are separate and are not interchangeable. |

Open `http://localhost:8787/` and paste an API token. Generate one with:

```bash
odooctl security token mint \
  --action api --env "*" --project "*" \
  --key-env ODOOCTL_API_KEY \
  --role operator
```

The mint command signs with `ODOOCTL_API_KEY` by default — the same key the
API server verifies with, so the explicit `--key-env` above is optional. See
`docs/rbac.md`.

## Pages

| Hash route | Description |
|---|---|
| `#/` | Dashboard — list all registered projects |
| `#/project/:name` | Project detail — environment grid + recent operations |
| `#/project/:name/env/:env` | Environment detail — Overview, Doctor, Operations, Backups, Clone, Promote tabs |

## RBAC in the UI

The SPA decodes the bearer token payload (base64url, unverified client-side)
to read the `roles` field for **display gating only**. The server always
re-checks RBAC independently. Role mapping:

| Role | Read | Backup/Deploy/Clone/Restore | Admin ops (protected envs, promote) |
|---|---|---|---|
| viewer | ✓ | — | — |
| operator | ✓ | ✓ (non-protected envs) | — |
| admin | ✓ | ✓ | ✓ |
| owner | ✓ | ✓ | ✓ |

Tabs and action buttons are hidden for roles that do not have the required
permission. A viewer sees only the Overview, Operations, and Backups tabs;
Clone and Promote tabs appear only for operator+ (and admin+ respectively).

## Destructive action confirmation

Clone and promote operations show a typed confirmation dialog before
enqueueing. The user must type an exact keyword:

| Operation | Keyword to type |
|---|---|
| Backup | none (confirm button only) |
| Clone a non-protected env | `clone` |
| Clone a protected env | the source environment name (e.g. `production`) |
| Promote | `promote` |

The confirm button remains disabled until the typed value matches the keyword
exactly, then submits via `enqueueOp()` → `POST /projects/{p}/operations`.

## Operation log streaming (SSE)

The SPA streams SSE events from `GET /operations/{id}/events` using `fetch()`
with a `ReadableStream` reader. `EventSource` is not used because it does not
support custom `Authorization` headers in browsers.

The stream reader:

1. Decodes UTF-8 chunks incrementally, splitting on newlines.
2. Parses `data: <json>` SSE lines into event objects.
3. On stream close, fetches the final operation record to display terminal status.

## Runner contract

`odooctl/web/__init__.py` is scanned by `odooctl.security.runner_contract` to
verify no privileged adapter imports are present. The dist JS/CSS files are not
Python and are ignored by the scanner. Run:

```bash
uv run odooctl security runner-check
```

The test `test_web_package_no_privileged_imports` in `tests/test_web.py`
enforces this contract in CI.
