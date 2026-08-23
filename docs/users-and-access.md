# User accounts, sessions, and ownership

odooctl is a multi-user system: the API/UI knows *who* is acting, *what they
may do* (RBAC), and *who owns what*. This page covers the account layer. The
role → action matrix itself is documented in [RBAC](rbac.md).

## Accounts

User accounts are **server-level**, like the project registry: one odooctl
host has one set of accounts spanning every registered project. They are
stored next to the registry (`~/.config/odooctl/users.json`) with owner-only
file permissions; passwords are hashed (salted scrypt — the hash format is
scheme-prefixed so the algorithm can be upgraded without invalidating stored
hashes).

Manage accounts from the server shell:

```bash
# First admin after install (password prompted, or --stdin / --password-env)
odooctl user add alice@example.com --role admin --name "Alice"

odooctl user list
odooctl user role alice@example.com --role operator   # replace roles
odooctl user passwd alice@example.com                 # reset + revoke sessions
odooctl user disable alice@example.com                # blocks login AND live sessions
odooctl user enable alice@example.com
odooctl user remove alice@example.com --yes
```

Admins can do the same from the web UI (**Access → User accounts**) or the API
(`/users`); the granted role can never exceed your own, you cannot modify an
account that outranks you, and you cannot disable or delete yourself.

A shell on the server is the local-admin principal — `odooctl user ...`
commands take no login. Protect the box itself; see [Security](security.md).

## Two credentials: sessions and tokens

| | Browser session | Bearer token |
| --- | --- | --- |
| For | humans in the web UI | CLI, CI, scripts |
| Created by | `POST /auth/login` (email + password) | `odooctl security token mint` / `POST /tokens` |
| Carried as | `odooctl_session` cookie (HttpOnly, SameSite=Lax) | `Authorization: Bearer ...` header |
| Roles | read from the user store **on every request** — role changes and disables apply immediately | embedded in the token payload |
| Revocable | yes (logout, disable, password change) | no — keep TTLs short (max 7 days) |
| Lifetime | 12 hours | chosen at mint time |

The server never stores session ids in the clear (only SHA-256 digests) and
never stores minted tokens at all.

Login is throttled per email address (5 failures / 15 minutes), and failed
logins do not reveal whether the email exists.

## Ownership: who owns what

Two informational owner fields answer "who owns what" in the API and UI.
They are attribution, not an access-control boundary — RBAC roles still
decide who may act:

- **Project owner** — stored in the machine-local registry.
  `odooctl project add <name> --owner alice@example.com`,
  `odooctl project owner <name> alice@example.com` (empty string clears), or
  `PATCH /projects/{project}/owner` (admin+).
- **Environment owner** — the optional `owner:` key on an environment in
  `odooctl.yml` (an email or team label, e.g. `platform-team`). Shared with
  the team via git, shown on environment cards and in
  `GET /projects/{p}/environments`.

## Attribution: who did what

Every operation and audit record carries the acting principal:

- **API/UI**: the authenticated identity — the user's email for session
  logins, the token subject for bearer tokens.
- **CLI**: `local:<os-user>` (the shell account that ran the command). Set
  `ODOOCTL_ACTOR` to override, e.g. `ODOOCTL_ACTOR=ci:release-pipeline` in a
  pipeline.
- **`odooctl sync`** (systemd timer) records `sync`.

The audit trail (`odooctl ops audit`, `GET /projects/{p}/audit`) is an
append-only hash chain; set `ODOOCTL_AUDIT_KEY` to make it HMAC-keyed
(tamper-evident). See [Security](security.md).

## Google / OIDC login

The account layer is provider-pluggable (records carry a `provider` field);
Google OIDC login is planned post-1.0 as configuration, not a refactor.
Email/password is the only provider today.
