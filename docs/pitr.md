# PostgreSQL WAL archiving and PITR

Point-in-time recovery (PITR) is an advanced, opt-in PostgreSQL recovery path.
It stores physical base backups and an immutable WAL stream in an independent
S3-compatible destination. It does not replace portable odooctl backups.

!!! warning
    PITR v1 protects the PostgreSQL database only. It does not rewind the Odoo
    filestore. Keep verified database + filestore backups and explicitly assess
    attachment consistency before any PITR cutover.

## Prerequisites and limits

- PostgreSQL 13 or newer with `wal_level = replica`.
- `archive_mode = on` and either `archive_command` or `archive_library`
  configured.
- No non-default PostgreSQL tablespaces. odooctl refuses them rather than
  silently producing an incomplete base backup.
- A dedicated S3-compatible archive namespace and `odooctl[s3]`.
- Host-side `psql`, `pg_basebackup`, `pg_verifybackup`, and `pg_controldata`
  binaries matching the source major version, with network access to
  `postgres.host:postgres.port`.
- An immutable recovery image pinned by SHA-256 and matching the source
  PostgreSQL major version.
- Enough local state/storage to stage and verify a base backup, WAL segments,
  and a recovered custom-format database dump.
- `pitr.filestore_policy: database_only`, acknowledging that the filestore is
  not part of the PITR recovery point.

PITR archives must be independent of `backups.remote`. Portable backups contain
the database and filestore together; the WAL archive is a PostgreSQL physical
recovery graph with separate retention and credentials.

## Configuration

Find the source cluster identity directly from PostgreSQL:

```sql
SELECT system_identifier FROM pg_control_system();
```

Pin a digest-qualified recovery image and configure only environment-variable
names for credentials:

```yaml
pitr:
  enabled: true
  environment: production
  cluster_id: primary-eu-1
  system_identifier: "7623400000000000001"
  recovery_image: postgres@sha256:1111111111111111111111111111111111111111111111111111111111111111
  filestore_policy: database_only
  replication_user: odoo_replicator
  replication_password_env: ODOO_PITR_REPLICATION_PASSWORD
  retention:
    base_backups: 2
    grace_hours: 24
  destination:
    type: s3
    bucket: acme-postgres-pitr
    prefix: production
    region: eu-central-1
    endpoint_env: ODOO_PITR_S3_ENDPOINT
    access_key_env: ODOO_PITR_S3_ACCESS_KEY
    secret_key_env: ODOO_PITR_S3_SECRET_KEY
```

Static access keys are optional; omit both access/secret references to use the
AWS provider chain. Session tokens require both static credential references.
KMS keys are referenced by environment variable and require
`encryption_algorithm: aws:kms`.

Validate the source and archive before enabling PostgreSQL archiving:

```bash
odooctl pitr check production
odooctl pitr archive-config production
```

`archive-config` prints a secret-free PostgreSQL setting. Install its exact
value as `archive_command`, reload PostgreSQL, and run `pitr check` again.
PostgreSQL expands `%p` and `%f`; odooctl escapes operator-controlled percent
characters and passes secrets only through the environment.

## Base backups and scheduling

Create and byte-verify a physical base backup:

```bash
odooctl pitr base create production
odooctl pitr base list production
odooctl pitr base verify production --base production_base_...
```

Generate systemd or cron definitions without embedding credentials:

```bash
odooctl schedule pitr-base --env production --interval daily \
  --environment-file /etc/odooctl/acme-pitr.env
odooctl schedule pitr-reconcile --env production --interval daily \
  --environment-file /etc/odooctl/acme-pitr.env
```

Base backups are published with a final immutable manifest. WAL objects and
local receipts are immutable: an idempotent retry must match size and SHA-256,
while conflicting bytes fail closed.

## Recovery workflow

Recovery is deliberately split into planning, isolated execution, and explicit
cutover:

```bash
odooctl pitr restore plan production \
  --target-time 2026-07-30T10:15:00Z
odooctl pitr restore execute production --plan production_pitr_plan_...
odooctl pitr restore cutover production \
  --restore production_pitr_restore_... \
  --confirm-environment production \
  --confirm-database odoo_prod \
  --accept-database-only
```

Planning verifies a complete base/WAL graph and creates a remote retention pin.
Execution downloads and checks every object, starts an isolated PostgreSQL
runtime with the pinned image, pauses recovery at the requested point, exports
a dump, restores it into a new database name, and verifies the Odoo schema. It
never overwrites the live database.

Cutover records the incoming and live PostgreSQL OIDs plus a deterministic
aside name before the first rename. Promotion is reconciled by OID after a
crash or lost response. The old database is deleted only after promotion is
durably recorded; interrupted cleanup can be resumed by rerunning the same
cutover command with the same confirmations.

## Retention and cross-host coordination

```bash
odooctl pitr retention reconcile production
```

Retention keeps the configured base-backup floor, grace-period backups, and
every locally or remotely pinned recovery graph. It verifies all retained and
deletion-candidate bytes before deleting the first object.

PITR mutations use an S3 compare-and-swap lease. An expired lease is never
stolen automatically because the original owner may still be running with a
slow or partitioned connection. Inspect it first:

```bash
odooctl pitr lease inspect production --json
```

Only after independently proving that the owner process stopped, recover the
exact expired lease:

```bash
odooctl pitr lease recover-expired production \
  --confirm-lease-id LEASE_ID \
  --confirm-owner OWNER \
  --confirm-purpose PURPOSE \
  --confirm-owner-stopped OWNER_STOPPED:LEASE_ID
```

An active lease, a changed ETag, clock ambiguity, or any confirmation mismatch
leaves the object untouched.
