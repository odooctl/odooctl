# M15 — Migration and Upgrade Assistant

## Goal

Provide safe Odoo version upgrade rehearsal and reporting.

## Positioning

This is not one-click blind production upgrade. It is rehearsal, evidence, and readiness reporting.

## Files to create

- `odooctl/migration/__init__.py`
- `odooctl/migration/matrix.py`
- `odooctl/migration/scan.py`
- `odooctl/migration/rehearse.py`
- `odooctl/migration/openupgrade.py`
- `odooctl/migration/data/paths.yaml`
- `odooctl/commands/migrate.py`
- `docs/migration.md`

## Commands

- `odooctl migrate matrix`
- `odooctl migrate scan --env production`
- `odooctl migrate rehearse --env production --to 19.0`
- `odooctl migrate rehearse --env production --to 19.0 --openupgrade`

## Rehearsal flow

1. Clone production into throwaway migration environment.
2. Sanitize if needed.
3. Swap image/version to target.
4. Run upgrade command with explicit DB flags.
5. Capture logs.
6. Run healthcheck.
7. Compare module states.
8. Produce migration report.
9. Drop throwaway DB/container unless `--keep`.

## Report includes

- source version
- target version
- installed modules
- failed modules
- warnings
- duration
- healthcheck status
- log references
- recommended next actions

## Acceptance criteria

- Migration matrix prints supported paths.
- Scan identifies installed modules and likely blockers.
- Rehearsal never writes to production DB/filestore.
- Failed rehearsal leaves clear report and cleanup status.
- OpenUpgrade hook is optional and pinned.
