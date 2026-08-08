# Error 005: PITR S3 integration fixture violates immutable-image validation

## Observed failure

Test: `tests/integration/test_pitr_s3.py::test_real_s3_wal_and_physical_base_round_trip`

With a real local S3-compatible MinIO endpoint configured, manifest construction fails before any S3 operation:

```text
ValidationError: postgres_image
postgres_image must be an immutable sha256 image reference
input_value='postgres:17.6'
```

## Root cause

`tests/integration/test_pitr_s3.py:108` constructs `PitrBaseBackupManifest` with the mutable tag `postgres:17.6`. The production model correctly requires an immutable digest reference.

## Where to fix

Primary fix: `tests/integration/test_pitr_s3.py`, replacing the mutable fixture value with a syntactically valid immutable reference such as `postgres@sha256:<64 hex characters>`.

The validation in `odooctl/metadata/models.py` should remain strict. Weakening the production invariant would hide the fixture defect and reduce restore reproducibility.

## Reproduce

Run a local S3-compatible endpoint with an existing bucket, export the four `ODOOCTL_TEST_S3_*` variables, then:

```bash
uv run pytest -m integration -q tests/integration/test_pitr_s3.py
```

## Scope

This test is shared infrastructure coverage and is not parametrized by Odoo 17/18/19. It therefore affects the overall PITR/S3 validation, but does not indicate a failure in any version-specific Odoo lifecycle.
