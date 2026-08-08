# M8 — Import / Takeover and Setup Wizard

## Goal

Support two onboarding paths:

1. Newcomer path: create a managed Odoo project from scratch.
2. Existing-user path: import/take over a running Odoo deployment without redeploying it.

## Strategic importance

This is the main differentiator over simple Odoo hosting panels. Existing self-hosted users need a way to adopt Odoo.sh-like management without moving to SaaS or rebuilding production.

## Files to create

- `odooctl/importer/__init__.py`
- `odooctl/importer/models.py`
- `odooctl/importer/detect.py`
- `odooctl/importer/report.py`
- `odooctl/importer/adopt.py`
- `odooctl/commands/import_cmd.py`
- `odooctl/commands/setup.py`
- tests under `tests/test_import_*.py` and `tests/test_setup.py`


## Locked v1 deployment mode

M8 targets **single-host Docker Compose only**. Detection may inspect local compose files and local Docker Compose state. Do not implement SSH, remote Docker contexts, Kubernetes, multi-host runners, or SaaS import in v1. Keep data models extensible, but fail clearly if a topology is outside v1 scope.

## Non-negotiable import safety contract

Detection must be strictly read-only. The import detector is allowed to read files and inspect running containers, but it must not mutate the system.

Forbidden during detection:

- container restart/stop/start
- `docker compose up/down/restart`
- Odoo redeploy
- DB writes, DDL, sanitize, restore, or module update
- volume writes or filestore changes
- writing generated config before explicit adoption
- printing secret values

Required behavior:

- preview-first UX
- generated config only after adoption confirmation
- secrets referenced by env/secret names, never inlined
- backup-after-adoption unless explicitly skipped
- doctor after adoption

## Import detection

Read-only probes should detect:

- compose file path
- Odoo service name
- Postgres service name
- Odoo image/version
- published HTTP port
- DB host/user/password env references
- DB name candidates
- addons paths
- filestore volume/path
- `odoo.conf` settings if mounted
- `dbfilter`
- `proxy_mode`
- `workers`
- longpolling/gevent config
- custom domains when inferable

## Commands

- `odooctl import [PATH] --preview`
- `odooctl import [PATH] --name <project> --yes`
- `odooctl import [PATH] --force`
- `odooctl setup`
- `odooctl setup --yes --stack odoo-19-community`

## Safety rules

- Preview first by default.
- Detection must not run `compose up`, `compose down`, restart/stop/start containers, drop/create/alter DBs, write volumes, write filestore data, run module updates, or redeploy Odoo.
- Never inline detected secret values into config, reports, logs, events, audit entries, or exceptions.
- Use env-var references or secret store references.
- Refuse to overwrite existing `odooctl.yml` unless `--force`.
- After adoption: run validate, doctor, and verified backup unless explicitly skipped. This backup is the first managed safety net.

## Acceptance criteria

- Import preview works on `experiments/odoo19-community-staging` without disruption and with no mutating Docker/DB/volume commands.
- Adopted config validates.
- Doctor passes.
- Backup after import is verified.
- Odoo remains reachable during and after import.
- Setup wizard can scaffold a greenfield project.
