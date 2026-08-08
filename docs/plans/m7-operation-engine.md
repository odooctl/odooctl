# M7 — Operation Engine, Events, Audit, Locks

## Goal

Make every mutating action a durable operation with events, audit trail, and concurrency locks.

## Why

A web UI needs progress streaming. A platform needs auditability. Operators need safe concurrency. Long-running operations cannot remain anonymous command executions.

## Files to create

- `odooctl/operations/__init__.py`
- `odooctl/operations/models.py`
- `odooctl/operations/store.py`
- `odooctl/operations/events.py`
- `odooctl/operations/locks.py`
- `odooctl/operations/audit.py`
- `odooctl/operations/engine.py`
- `odooctl/commands/ops.py`

## Operation model

Fields:

- `id`
- `kind`: deploy, backup, restore, clone, promote, import, env_create, env_destroy, update_modules, rollback, migrate
- `project`
- `environment`
- `status`: queued, running, succeeded, failed, cancelled
- `actor`
- `params_redacted`
- `result_ref`
- `error`
- timestamps

## Event model

Append JSONL events:

- operation id
- sequence
- timestamp
- level
- phase
- message
- structured data

## Audit model

Append-only audit entries with hash chain:

- previous hash
- current hash
- actor
- action
- target
- redacted params
- outcome
- operation id

## Lock model

Use per-project/environment file locks under `.odooctl/locks/`.

Examples:

- backup production: lock production read/write according to operation design
- clone production → staging: lock target staging, read source production
- restore staging: lock staging
- promote staging → production: lock production

## CLI

Add:

- `odooctl ops list`
- `odooctl ops show <id>`
- `odooctl ops logs <id>`
- `odooctl ops logs <id> --follow`
- `odooctl ops cancel <id>`

## Acceptance criteria

- Every mutating command writes an operation record.
- Events are visible through `ops logs`.
- Audit chain verifies and detects tampering.
- Concurrent conflicting clone/restore/deploy fails safely.
- Live Odoo backup and clone create complete operation timelines.
