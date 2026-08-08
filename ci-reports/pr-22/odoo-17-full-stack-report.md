# Odoo 17 full-stack test report

## Environment

- PR head: `80d260e5e9208b218989ade59ebcebd85a2ee65c`
- Odoo image: `odoo:17.0`
- PostgreSQL image: `postgres:16-alpine`
- Runtime: Docker Engine 29.5.2 / Docker Compose v5.1.4
- Python harness: 3.12.13
- Isolation: unique Compose project, port, volumes, temporary project directory, and XDG registry

## Version-specific result

Command:

```bash
ODOOCTL_IT_VERSIONS=17.0 uv run pytest -m integration tests/integration -q -ra
```

Result: **7 passed, 4 opt-in tests skipped in 123.36 seconds**.

The seven passing real-Odoo tests cover validation and doctor checks, environment status, verified database/filestore backup, production-to-staging clone with sanitization, restore into staging, API enqueue/runner execution parity, and container ownership isolation.

## Supplemental infrastructure suites

The four initially skipped tests were subsequently enabled independently because they are not Odoo-version-parametrized:

- S3 object filestore round trip against local MinIO: passed.
- S3 remote backup round trip against local MinIO: passed.
- S3 PITR/base-backup round trip: failed before network I/O; see `005-pitr-s3-mutable-image-fixture.md`.
- Disposable k3d production simulation: passed in 250.92 seconds.

## Verdict

Odoo 17 database, filestore, sanitization, restore, API/runner, and Compose lifecycle behavior passes. The only supplemental failure is a shared PITR test-fixture validation error, not an Odoo 17 runtime failure.
