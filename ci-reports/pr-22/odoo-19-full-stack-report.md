# Odoo 19 full-stack test report

## Environment

- PR head: `80d260e5e9208b218989ade59ebcebd85a2ee65c`
- Odoo image: `odoo:19.0`
- PostgreSQL image: `postgres:16-alpine`
- Runtime: Docker Engine 29.5.2 / Docker Compose v5.1.4
- Python harness: 3.12.13
- Isolation: unique Compose project, port, volumes, temporary project directory, and XDG registry

## Version-specific result

Command:

```bash
ODOOCTL_IT_VERSIONS=19.0 uv run pytest -m integration tests/integration -q -ra
```

Result: **7 passed, 4 opt-in tests skipped in 125.20 seconds**.

The seven passing real-Odoo tests cover validation and doctor checks, environment status, verified database/filestore backup, production-to-staging clone with sanitization, restore into staging, API enqueue/runner execution parity, and container ownership isolation.

## Supplemental infrastructure suites

The four initially skipped tests were subsequently enabled independently because they are not Odoo-version-parametrized:

- S3 object filestore round trip against local MinIO: passed.
- S3 remote backup round trip against local MinIO: passed.
- S3 PITR/base-backup round trip: failed before network I/O; see `005-pitr-s3-mutable-image-fixture.md`.
- Disposable k3d production simulation: passed in 250.92 seconds. This simulation uses the repository's Odoo 19 k3d example.

## Verdict

Odoo 19 database, filestore, sanitization, restore, API/runner, Compose lifecycle, and k3d production simulation pass. The only supplemental failure is the shared PITR test-fixture validation error.
