# M6 — Service Layer and Result Models

> For implementation agents: use TDD, preserve current CLI behavior, and keep all existing commands backward compatible.

## Goal

Extract command orchestration into reusable service modules so CLI, API, runner, and web UI can all call the same engine.

## V1 scope constraints

Service abstractions must support **single-host Docker Compose** first. Do not add remote runner or multi-host abstractions in M6 beyond clean interfaces. The service layer should make future adapters possible without expanding v1 scope.

## Why

Current command modules contain business logic directly. That blocks a safe UI/API because the frontend would either shell out to CLI or duplicate logic. M6 creates the seam.

## Files to create

- `odooctl/services/__init__.py`
- `odooctl/services/models.py`
- `odooctl/services/context.py`
- `odooctl/services/project.py`
- `odooctl/services/environment.py`
- `odooctl/services/backup.py`
- `odooctl/services/restore.py`
- `odooctl/services/clone.py`
- `odooctl/services/deploy.py`

## Files to modify

- `odooctl/commands/deploy.py`
- `odooctl/commands/backup.py`
- `odooctl/commands/restore.py`
- `odooctl/commands/clone.py`
- `odooctl/commands/status.py`
- `odooctl/commands/doctor.py`
- `odooctl/commands/env.py`
- related tests under `tests/`

## Result models

Add structured models/dataclasses:

- `ServiceResult[T]`
- `ProjectSummary`
- `EnvironmentSummary`
- `BackupResult`
- `RestoreResult`
- `CloneResult`
- `DeployResult`
- `DoctorReport`
- `StatusReport`

## Implementation tasks

1. Add result model tests first.
2. Add `ServiceContext` wrapper around `ProjectContext` and adapter construction.
3. Extract read-only project/status/doctor logic.
4. Extract backup logic.
5. Extract restore logic.
6. Extract clone logic.
7. Extract deploy/update logic.
8. Convert commands to render service results.
9. Preserve CLI stdout and JSON output.
10. Run full gates.

## Acceptance criteria

- Commands are thin wrappers: parse args, call services, render output.
- No behavior regression in existing tests.
- New service tests cover success/error paths.
- `uv run pytest -q` passes.
- `uv run ruff check .` passes.
- Real Odoo clone flow still passes.
