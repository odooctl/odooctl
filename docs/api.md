# odooctl Local API

The optional local API exposes project and operation management over HTTP. It is
intentionally unprivileged: it reads state, enqueues operations, and streams
events — but never touches Docker, Postgres, or the filestore directly. Mutating
work is delegated to the privileged runner via the durable queue.

## Installation

FastAPI and uvicorn are optional extras:

```bash
pip install odooctl[api]
```

## Starting the server

```bash
# Localhost-only (default) on port 8787
export ODOOCTL_API_KEY="your-hmac-key"   # must be at least 32 characters
odooctl serve

# Custom port with a pre-built SPA (still localhost-only)
odooctl serve --host 127.0.0.1 --port 8080 --static-dir ./spa/dist
```

The server binds to `127.0.0.1` by default, and `TrustedHostMiddleware`
restricts accepted `Host` headers to `127.0.0.1` / `localhost`. Keep it that
way: the API is designed for localhost-only operation.

> **Warning:** do not bind the API to a non-loopback address (e.g.
> `--host 0.0.0.0`) without an authenticating reverse proxy, TLS, and firewall
> rules in front of it. The API speaks plain HTTP, so bearer tokens would
> cross the network unencrypted, and anyone who obtains one can enqueue
> privileged operations. If remote access is needed, prefer an SSH tunnel to
> `127.0.0.1:8787`.

Rebuilding the SPA dist requires a server restart: `index.html` is read once
at startup and served from memory for the lifetime of the process.

Keys shorter than 32 characters are rejected at startup (both `odooctl serve`
and `odooctl runner`): a short HMAC key makes bearer and capability tokens
brute-forceable offline. Generate one with
`python -c 'import secrets; print(secrets.token_hex(32))'`.

## Starting the runner

The privileged runner must run as a user with Docker socket and filestore access:

```bash
export ODOOCTL_API_KEY="your-hmac-key"   # same key as the API server
odooctl runner              # loop forever
odooctl runner --once       # process one operation and exit
```

## Authentication

All requests require a bearer token:

```
Authorization: Bearer <token>
```

Tokens are minted with `odooctl security token mint` (see `docs/rbac.md`):

```bash
# Operator token valid for 8 hours
odooctl security token mint \
  --action api --environment "*" --project "*" \
  --key-env ODOOCTL_API_KEY \
  --ttl 28800 \
  --role operator
```

**Signing key:** the API server verifies bearer tokens with the key it was
started with (`ODOOCTL_API_KEY`), and `token mint`'s `--key-env` option
defaults to the same `ODOOCTL_API_KEY`, so tokens minted with defaults verify
against the API. Pass `--key-env ODOOCTL_RUNNER_KEY` only if your deployment
deliberately uses a separate signing domain.

Token payload fields:

| Field    | Description                                              |
|----------|----------------------------------------------------------|
| `act`    | Token action scope (`"api"` for session tokens)          |
| `env`    | Environment scope (`"*"` for session tokens)             |
| `proj`   | Project scope (`"*"` for session tokens)                 |
| `roles`  | List of roles: `["viewer"]` or `["operator"]`, etc.      |
| `iat`    | Issued-at Unix timestamp                                 |
| `exp`    | Expiry Unix timestamp                                    |
| `nonce`  | Random per-token nonce (enables future single-use checks)|

## RBAC roles

| Role       | Allowed operations                                          |
|------------|-------------------------------------------------------------|
| `viewer`   | Read-only: projects, environments, status, backup/snapshot manifests, audit |
| `operator` | Viewer + backup, deploy, clone, restore                     |
| `admin`    | Operator + promote, env management, secrets                 |
| `owner`    | All actions including protected-environment destructive ops |

## Routes

### Projects

| Method | Path                                    | Required role | Description                        |
|--------|-----------------------------------------|---------------|------------------------------------|
| GET    | `/projects`                             | viewer        | List all registered projects       |
| GET    | `/projects/{project}`                   | viewer        | Get project info                   |
| GET    | `/projects/{project}/environments`      | viewer        | List environments from config      |
| GET    | `/projects/{project}/status`            | viewer        | Metadata-derived status            |
| GET    | `/projects/{project}/backups`           | viewer        | List backup manifests              |
| GET    | `/projects/{project}/snapshots`         | viewer        | List local snapshot manifests      |
| GET    | `/projects/{project}/snapshots/{id}`    | viewer        | Read one snapshot and exact IDs    |
| GET    | `/projects/{project}/restore-points`    | viewer        | List portable restore points       |
| GET    | `/projects/{project}/audit`             | viewer        | Read audit trail entries           |

**Status note**: `GET /projects/{project}/status` returns metadata-store-derived
state (last deployment commit, last backup timestamp). It does NOT run
`docker compose ps`; live container status requires a queued operation via the
runner.

### Operations

| Method | Path                                   | Required role | Description                         |
|--------|----------------------------------------|---------------|-------------------------------------|
| POST   | `/projects/{project}/operations`       | operator+     | Enqueue a mutating operation        |
| GET    | `/operations/{id}`                     | viewer        | Fetch operation record              |
| GET    | `/operations/{id}/events`              | viewer        | Stream operation events (SSE)       |
| POST   | `/operations/{id}/cancel`              | operator+     | Cancel a queued operation           |

Cancelling is a write action (`cancel` in the RBAC matrix): viewer tokens get
`403`. Additionally, `/operations/{id}` routes are project-scoped through the
token: a token minted with a concrete `proj` claim (anything other than `"*"`)
can only read, stream, or cancel operations belonging to that project — other
projects' operations answer `404`.

#### POST /projects/{project}/operations

Request body:

```json
{
  "kind": "backup",
  "environment": "production",
  "params": {}
}
```

The API accepts the following `kind` values, but only a subset is executed by
the runner (`odooctl/runner/worker.py::_dispatch`); the rest are CLI-only by
design:

| Kind                | Enqueueable via API | Executed by runner | Notes                                          |
|---------------------|---------------------|--------------------|------------------------------------------------|
| `backup`            | yes                 | yes                |                                                |
| `clone`             | yes                 | yes                | `params.source` defaults to `production`       |
| `dr_drill`          | yes                 | yes                |                                                |
| `snapshot_create`   | yes                 | yes                | uses the config-bound snapshot environment     |
| `snapshot_reconcile`| yes                 | yes                | requires `params.snapshot_id`                  |
| `snapshot_restore`  | yes                 | yes                | plan-only unless `params.execute` is `true`    |
| `pitr_base_create`  | yes                 | yes                | creates and verifies a physical base backup    |
| `pitr_reconcile`    | yes                 | yes                | reconciles the configured recovery graph       |
| `pitr_restore`      | yes                 | yes                | requires a safe `params.plan_id`                |
| `pitr_cutover`      | yes                 | yes                | requires exact typed confirmations             |
| `filestore_migrate` | yes                 | yes                | action-specific params; see below               |
| `migrate_rehearsal` | yes                 | yes                | requires `params.to` (target version)          |
| `restore`           | yes                 | no — CLI only      | run `odooctl restore` on the host              |
| `deploy`            | yes                 | no — CLI only      | run `odooctl deploy` on the host               |
| `promote`           | yes                 | no — CLI only      | run `odooctl promote` on the host              |
| `env_create`        | yes                 | no — CLI only      | run `odooctl env create` on the host           |
| `env_destroy`       | yes                 | no — CLI only      | run `odooctl env destroy` on the host          |
| `update_modules`    | yes                 | no — CLI only      | run `odooctl update-modules` on the host       |
| `rollback`          | yes                 | no — CLI only      | run `odooctl rollback` on the host             |

Enqueueing a CLI-only kind is accepted (202) and recorded in the operation
store, but the runner rejects it at dispatch time and marks the operation
`failed` with `Unsupported operation kind in runner`. This is deliberate:
these workflows involve interactive confirmation and host-level judgment and
are intentionally kept on the CLI for now.

User-supplied `params` are redacted (via `odooctl.security.redaction.redact`)
before being recorded in the operation store and queue entry.

`filestore_migrate` accepts `action` values `plan`, `sync`, `verify`,
`cutover`, `download`, `delete_source`, and `delete_remote_marker`. Every
action except `plan` requires a safe `migration_id`. Cutover additionally
requires the exact `confirm_environment` and
`confirm_source_retained: true`; source deletion requires exact environment
and migration confirmations plus `delete_source: true`. The configured
environment must have an explicit `object_mirror`, `posix_object_mount`, or
`odoo_module` backend. Queued downloads accept only a safe project-relative
destination; the runner repeats this containment check even if a queue entry
bypasses the API. Protected environments retain restore-class RBAC.

Response (202 Accepted):

```json
{
  "op_id": "abc123def456",
  "kind": "backup",
  "project": "my-project",
  "environment": "production",
  "status": "queued",
  "created_at": "2026-05-30T12:00:00+00:00"
}
```

### Snapshot operations

All three snapshot operation kinds require `environment` to equal
`snapshots.environment`. That setting binds one project environment to one
provider source; the API rejects a different label before queueing, and the
runner rechecks the environment while holding its lock. `snapshot_create` and
`snapshot_reconcile` use backup-class RBAC. `snapshot_restore` uses
restore-class RBAC, so a protected bound environment requires an `admin` or
`owner`.

| Kind | `params` | Result |
|------|----------|--------|
| `snapshot_create` | `{}` | Requests a provider snapshot and emits its manifest ID/status. |
| `snapshot_reconcile` | `{"snapshot_id": "..."}` | Refreshes a requested/pending manifest without creating a new snapshot. |
| `snapshot_restore` | `{"snapshot_id": "...", "execute": false}` | Generates and emits a local recovery plan; no provider command or credentials are required. |
| `snapshot_restore` | `{"snapshot_id": "...", "execute": true, "confirm_snapshot": "...", "confirm_resource": "..."}` | Executes only when both confirmations exactly match the stored identities. |

Use a plan-then-execute workflow:

1. Read the local manifest and exact confirmation identities:

   ```http
   GET /projects/my-project/snapshots?environment=production
   GET /projects/my-project/snapshots/production-20260730T120000Z-deadbeef
   ```

   The detail response includes `snapshot_id`, `source_resource_id`, provider
   resource IDs, consistency, and status. Listing and detail reads do not call
   the provider.

2. Queue a plan-only restore:

   ```json
   {
     "kind": "snapshot_restore",
     "environment": "production",
     "params": {
       "snapshot_id": "production-20260730T120000Z-deadbeef",
       "execute": false
     }
   }
   ```

3. Stream `GET /operations/{op_id}/events` and inspect the
   `snapshot_restore` event. Its `data` includes the canonical
   `source_resource_id`, `status`, `message`, restored IDs (if any), and a
   structured `plan` containing `provider`, `commands`, `destructive`, and
   `notes`. Planning reads local state only, which allows review even when AWS
   or Hetzner credentials are unavailable.

4. Queue a second operation only after reviewing that event:

   ```json
   {
     "kind": "snapshot_restore",
     "environment": "production",
     "params": {
       "snapshot_id": "production-20260730T120000Z-deadbeef",
       "execute": true,
       "confirm_snapshot": "production-20260730T120000Z-deadbeef",
       "confirm_resource": "i-0123456789abcdef0"
     }
   }
   ```

   `execute` must be a JSON boolean; strings such as `"false"` are rejected by
   the runner. Both confirmation values must match exactly. Execution requires
   the provider CLI and credentials documented in
   [Disaster recovery](disaster-recovery.md).

A successfully handled provider request can still report `pending`. For
snapshot creation, queue `snapshot_reconcile` for the same manifest before
considering another create. For recovery, the result event and restore metadata
retain every created resource ID; retrying the same typed execution uses
provider idempotency markers to discover that recovery set. Provider snapshots
and recovery resources are not deleted or retained by the API and can remain
billable.

#### GET /operations/{id}/events

Returns a `text/event-stream` (SSE) response. Each event is a JSON-encoded
operation event:

```
data: {"op_id":"abc123","seq":0,"timestamp":"...","level":"info","phase":"start","message":"operation started: backup on production","data":{}}

data: {"op_id":"abc123","seq":1,"timestamp":"...","level":"info","phase":"backup","message":"backup complete: bk-20260530-120001","data":{}}
```

The stream terminates when the operation reaches `succeeded`, `failed`, or
`cancelled`. Pass `?max_polls=N` to limit poll iterations (useful in tests or
short-lived clients). `max_polls` is clamped server-side to `[1, 600]`
(600 × 0.5 s = 5 minutes) so a client cannot pin a worker indefinitely.

## Security model

- **API / runner split**: the API never imports `odooctl.adapters` or
  `odooctl.odoo`. All privileged work runs in the separate runner process. This
  is enforced structurally by `odooctl security runner-check`.
- **Capability tokens**: when the API enqueues an operation, it mints a
  short-lived HMAC-signed capability token scoped to the exact
  action/environment/project. The default TTL is 300 seconds (5 minutes),
  keeping the replay window small. The runner verifies this token before
  executing, preventing forged queue entries even if the queue directory is
  writable.
- **Nonce tracking**: the runner records consumed token nonces in
  `{state_dir}/consumed_nonces.json` (e.g. `.odooctl/consumed_nonces.json`)
  as `{nonce: consumed_at}` timestamps to prevent token replay within the
  TTL. Entries older than 2 hours (2 × the maximum token TTL) are purged on
  each write so the store cannot grow unbounded.
- **Param redaction**: user-supplied operation params are passed through
  `odooctl.security.redaction.redact` before being stored or logged.
- **Localhost-only default**: `TrustedHostMiddleware` restricts the API to
  `127.0.0.1` / `localhost` by default.

## Queue format

The durable queue lives at `{project_root}/.odooctl/queue/`. Each entry is a
JSON file named `{op_id}.json`:

```json
{
  "op_id": "abc123def456",
  "kind": "backup",
  "project": "my-project",
  "environment": "production",
  "actor": "api-client",
  "params_redacted": {},
  "token": "<capability-token>",
  "created_at": "2026-05-30T12:00:00+00:00"
}
```

The runner claims an entry by atomically renaming `{op_id}.json` →
`{op_id}.running`. On success it removes the file; on failure it renames it to
`{op_id}.failed`.
