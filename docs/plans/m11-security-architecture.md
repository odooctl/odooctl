# M11 — Security Architecture

## Goal

Define and implement the platform security model before exposing API/UI surfaces.

## V1 security boundary

V1 is single-host Docker Compose. The privileged runner runs locally on the same host as Docker. The web/API process stays socket-free. Remote runners and hosted multi-tenant worker pools are explicitly future work.

## Principles

- Web/API process never mounts Docker socket.
- Privileged runner executes Docker/Postgres/git/tar work.
- RBAC gates every mutating service action.
- Secrets are encrypted or env-referenced.
- Logs/events/audit never reveal secret values.
- Destructive operations require typed confirmation or privileged approval.

## Files to create

- `odooctl/security/__init__.py`
- `odooctl/security/principals.py`
- `odooctl/security/rbac.py`
- `odooctl/security/secrets.py`
- `odooctl/security/tokens.py`
- `odooctl/security/redaction.py`
- `odooctl/security/runner_contract.py`
- `odooctl/commands/security.py`
- `docs/rbac.md`
- `docs/runner-architecture.md`

## Roles

- owner: all actions
- admin: manage projects/envs/secrets/promote production
- operator: deploy non-prod, backup, clone, restore staging
- viewer: read status/logs/backups/operations only

## Secret store

Support:

- env-var references
- encrypted local store
- rotation metadata
- secret names only in config
- redaction in logs and audit

## Runner contract

API/web may:

- read state
- enqueue operations
- stream events
- read audit according to RBAC

Runner may:

- access Docker/Compose
- run Postgres commands
- manage filestore archives
- run git operations

## Acceptance criteria

- RBAC tests cover all roles/actions.
- Secret values never appear in operation events/audit logs.
- Audit verify detects tampering.
- API package cannot import Docker/Compose adapters directly.
