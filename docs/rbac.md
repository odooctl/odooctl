# RBAC and the security model

odooctl now has role-based access control (RBAC) primitives for the upcoming
API/runner boundary. This document describes the identity model, the role →
action matrix, secret handling, and capability tokens. It complements
[`runner-architecture.md`](runner-architecture.md), which covers the
web/API vs. privileged runner split.

Enforcement scope: every mutating API route authenticates a principal and
checks the matrix (a test asserts this for all mutating routes), and the
privileged runner re-checks the capability token's roles before executing.
The local CLI is the local-admin principal — a shell on the server outranks
any API role — so CLI commands are attributed (`local:<os-user>`) but not
role-gated. User accounts and sessions are covered in
[Users & access](users-and-access.md).

The implementation lives in `odooctl/security/`:

| Module | Responsibility |
| --- | --- |
| `principals.py` | org / user / role / principal identity models |
| `rbac.py` | action matrix, `is_allowed` / `require` helpers |
| `secrets.py` | encrypted + env-referenced secret store, rotation metadata |
| `tokens.py` | signed capability tokens for queued runner actions |
| `redaction.py` | central helpers that scrub secret values from strings/maps |
| `runner_contract.py` | structural API-vs-runner import contract |

## Identity model

A **principal** is the single identity object that RBAC checks and audit
records reason about. It is transport-agnostic so the future API (M12) can
build one from an authenticated request, a token subject, or a service account.

- **Org** — tenant boundary. V1 is effectively single-tenant, but every
  principal carries an `org_id` so multi-tenant scoping can be added later.
- **User** — a human account within an org.
- **Principal** — `id`, `org_id`, `kind` (`user` / `service` / `token`), and a
  set of `roles`. Its `identity` string (`user:alice@acme`) is what appears in
  audit records — never a secret.

```python
from odooctl.security import Principal, Role, User

alice = User(id="alice", org_id="acme", email="alice@acme.test")
principal = Principal.for_user(alice, {Role.OPERATOR})
```

## Roles

| Role | Intent |
| --- | --- |
| `owner` | Every action, including protected/production destructive operations. |
| `admin` | Manage projects/envs/secrets and promote to production. |
| `operator` | Deploy non-prod, backup, clone, restore staging. |
| `viewer` | Read-only: status, logs, backups, operations, audit. |

Roles are ordered by privilege (`viewer < operator < admin < owner`).
`Principal.has_at_least(role)` answers elevation questions.

## Action matrix

`rbac.role_matrix()` returns the full `{role: {action: allowed}}` table; the
CLI renders it with `odooctl security rbac`.

| Action | viewer | operator | admin | owner |
| --- | :---: | :---: | :---: | :---: |
| read | ✓ | ✓ | ✓ | ✓ |
| status | ✓ | ✓ | ✓ | ✓ |
| logs | ✓ | ✓ | ✓ | ✓ |
| backups (view) | ✓ | ✓ | ✓ | ✓ |
| operations (view) | ✓ | ✓ | ✓ | ✓ |
| audit (read) | ✓ | ✓ | ✓ | ✓ |
| backup (create) | · | ✓ | ✓ | ✓ |
| deploy | · | ✓ | ✓ | ✓ |
| clone | · | ✓ | ✓ | ✓ |
| restore | · | ✓ | ✓ | ✓ |
| cancel (operation) | · | ✓ | ✓ | ✓ |
| promote | · | · | ✓ | ✓ |
| env (manage) | · | · | ✓ | ✓ |
| secrets (manage) | · | · | ✓ | ✓ |

### Protected / production escalation

Destructive actions — `deploy`, `clone`, `restore`, `promote`, `env`,
`secrets` — require **admin or higher** when they target a *protected*
(production) environment, even if the base matrix would allow an operator:

```python
from odooctl.security import rbac
from odooctl.security.rbac import Action

rbac.is_allowed(operator, Action.DEPLOY)                 # True  (non-prod)
rbac.is_allowed(operator, Action.DEPLOY, protected=True) # False (production)
rbac.require(admin, Action.DEPLOY, protected=True)       # ok
```

`require()` raises `AccessDenied` (a `PermissionError` subclass) whose message
names the principal and action but contains no secret material.

Some operation kinds have a **project-wide blast radius** because compose
services are shared by every environment: `rbac.kind_protected(cfg, kind, env)`
computes the effective protected flag, and for `service_restart` it returns
true when *any* environment in the project is protected — an operator cannot
bounce the container serving production by targeting staging. Both the API
enqueue path and the runner's defensive re-check use this helper.

### Managing access from the web UI

The web UI's **Access** page (`#/access`) renders this matrix live from
`GET /rbac/matrix` and lets admins mint scoped bearer tokens via
`POST /tokens` (minted role capped at the minter's own rank, TTL ≤ 7 days,
token shown once). See `docs/web-ui.md`.

## Secrets

The secret store (`secrets.py`) supports two sources:

- **env reference** — only the env-var *name* is persisted; the value lives in
  the process environment and is resolved on read.
- **encrypted local store** — the value is encrypted at rest. Confidentiality
  uses an HMAC-SHA256 keystream (counter mode); integrity uses encrypt-then-MAC
  with a separate HMAC-SHA256 key. This is **stdlib-only** — no third-party
  `cryptography` dependency for single-host v1.

Rotation metadata (`version`, `rotated_at`, `rotation_interval_days`) is tracked
per secret; `SecretRecord.is_due_for_rotation()` flags overdue secrets.

### No-leak guarantees

- Revealed values are wrapped in `SecretValue`, whose `repr`/`str` return a
  constant mask. The only way to obtain the raw string is `.reveal()`.
- The on-disk store contains ciphertext envelopes and value-free metadata only.
- `SecretRecord.to_public_dict()` is the safe view for CLI/JSON/audit output.
- `redaction.redact()` scrubs known secret values and collapses
  `${VAR:-default}` to `${VAR}` (the default may itself be a secret) before any
  string or mapping reaches logs, operation events, or the audit trail.

### Key management

`resolve_key()` chooses the master key in this order:

1. an explicit passphrase argument, derived (PBKDF2-HMAC-SHA256) against a
   persisted per-store salt;
2. the `ODOOCTL_SECRET_KEY` env var, derived the same way;
3. a random 32-byte key persisted at `<state>/secrets/master.key` (created with mode `0600`).

Threat-model note: the local encrypted store protects against casual plaintext
leakage in config, logs, metadata, and backups of `secrets.json` alone. If an
attacker can read both `secrets.json` and the colocated `master.key`, they can
decrypt stored secrets. Operators who need stronger at-rest separation should
provide `ODOOCTL_SECRET_KEY` from a host secret manager or environment source
outside the backed-up state directory.

### CLI

```console
$ odooctl security secret put db_password --value-env DB_PW_SOURCE
$ odooctl security secret put db_password --reference ODOO_DB_PASSWORD
$ odooctl security secret list
$ odooctl security secret get db_password            # metadata only
$ odooctl security secret get db_password --reveal   # the one place a value prints
$ odooctl security secret rotate db_password --value-env NEW_DB_PW
```

Raw secret values are **never** accepted as command-line arguments — they would
leak through shell history and `ps`. Values arrive via `--value-env` (a named
env var) or `--stdin`.

## Capability tokens

The API enqueues an operation and mints a signed capability token authorizing
*one* scoped action; the runner verifies it before executing. Tokens are
stdlib-only `base64url(header).base64url(payload).HMAC-SHA256` triples.

A token is scoped to an `action`, `environment`, and `project`, carries an
expiry (`exp`) and a random `nonce`, and may carry an optional `subject`.
Verification rejects:

- a wrong signing key or tampered payload (`TokenInvalid`),
- an expired token (`TokenExpired`),
- a mismatched action/environment/project scope (`TokenScopeError`).

Tokens are **signed, not encrypted** — the payload is readable, so no secret
value is ever placed inside one. They are also replayable within their TTL for
the same scope unless the runner records consumed nonces and rejects repeats;
the runner does exactly that (`consumed_nonces.json`), and the default mint
TTL is 300 seconds to keep the replay window small.

Cancelling an operation is a **write action** (`cancel`): it requires
operator-or-higher, never a viewer/read token.

```console
$ export ODOOCTL_API_KEY=...     # signing key, never on argv
$ odooctl security token mint --action backup --env production --project acme --ttl 300
$ odooctl security token verify --stdin --action backup --env production --project acme < token.txt
```

### Which signing key?

`odooctl security token mint` / `token verify` read the signing key from the
env var named by `--key-env`, which **defaults to `ODOOCTL_API_KEY`** — the
same key both the API server (`odooctl serve`) and the runner
(`odooctl runner`) verify with. Tokens minted with defaults therefore work
against the running services. `--key-env ODOOCTL_RUNNER_KEY` remains
available for deployments that intentionally split signing domains; tokens
minted under a different key are rejected by the API/runner.

Example minting a session token the API will accept:

```console
$ odooctl security token mint \
    --action api --env "*" --project "*" \
    --key-env ODOOCTL_API_KEY \
    --role operator
```
