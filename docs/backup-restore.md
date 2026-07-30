# Backup and Restore

`odooctl backup production` creates a backup directory with:

- `db.dump`
- `filestore.tar`
- redacted `odoo.conf.redacted` when an Odoo config file exists
- `git_commit.txt`
- `docker_image.txt`
- `manifest.json` with checksums

`odooctl restore staging --backup latest` recreates the target database, restores the dump, restores the filestore, applies target-safe config through the Odoo project config, and runs health checks.

Backups are assembled under a hidden staging directory, checked for non-empty
artifacts and matching SHA-256 digests, then published with an atomic rename.
Each ID has the form
`<environment>_<UTC timestamp>_<random 24-hex suffix>`. The random suffix is
always present so independent hosts publishing at the same second do not reuse
an ID.

## Execution modes

For Docker Compose stacks, prefer:

```yaml
runtime:
  execution_mode: docker
postgres:
  service: db
  internal_host: db
```

In Docker mode, `odooctl` runs PostgreSQL dump/restore through `docker compose exec -T <db-service>` and keeps custom-format dumps binary-safe. This works when PostgreSQL is not exposed on the host.

Host mode runs `pg_dump`, `pg_restore`, and `psql` on the operator host and requires host PostgreSQL client tools plus network access to the DB.

## GFS retention

Local and remote retention use the same UTC grandfather-father-son policy:

```yaml
backups:
  retention:
    daily: 7
    weekly: 4
    monthly: 6
    grace_hours: 1
```

Each tier retains the newest completed backup in each represented UTC
day, ISO week, or month. The union of those sets is retained, and the newest
owned backup is always kept even when every tier is zero. Selection and
deletion validate the manifest's project and environment, so a shared backup
root or bucket prefix does not grant authority over another project's backup.
`grace_hours` also preserves every newly published local or remote backup for
at least that long. This lets concurrent publishers finish before a later,
deterministic retention pass removes points outside the GFS union.

## Remote S3 copies

Add the optional dependency and configure `backups.remote`:

```bash
pipx install 'odooctl[s3]'
```

```yaml
backups:
  retention:
    daily: 7
    weekly: 4
    monthly: 6
    grace_hours: 1
  remote:
    type: s3
    bucket: acme-odoo-backups
    prefix: acme
    region: eu-central-1
    policy: required
    verify_after_upload: true
    orphan_grace_hours: 24
```

Credentials use normal AWS resolution, or explicit env references:

```yaml
    access_key_env: ODOO_S3_ACCESS_KEY
    secret_key_env: ODOO_S3_SECRET_KEY
    endpoint_env: ODOO_S3_ENDPOINT
```

There is no substitute destination when S3 dependencies, credentials, or the
provider are unavailable. Choose the failure behavior explicitly:

| Policy | Behavior |
| --- | --- |
| `required` | A remote upload, byte-verification, or retention failure makes the command fail. The already validated local backup remains available. Upload/verification failure records `remote_status: failed`; a retention alert is recorded separately from the verified-copy status. |
| `best_effort` | The local backup succeeds. Upload/verification failure records `remote_status: degraded`; a retention alert leaves a verified copy marked complete but records and warns about the reconciliation error. This is the default. |
| `disabled` | No remote client is created and no remote operation is attempted. A bucket is not required. |

`verify_after_upload` defaults to `true`: it streams every uploaded payload
object and the final manifest back from S3 and hashes the actual bytes before
declaring the remote copy complete. With `false`, publication still checks
object identity, size/checksum metadata, and completion-marker ordering, but skips that
immediate read-back. Run `backup-remote verify` later for a full byte check.

### Namespace and completion model

New objects are stored below a deterministic project namespace:

```text
<prefix>/projects/<project-slug>-<project-hash>/<backup-id>/
```

The payload is uploaded first. `manifest.json` is published last and is the
completion marker; list, latest, verify, download, and retention operations
consider only completed manifests whose project and environment match the
active configuration. Existing owned backups in the older unscoped prefix
remain readable for migration, but new writes and automatic retention use the
project-scoped namespace.

An interrupted upload may leave payload objects without a completion marker.
When the publisher knows it has abandoned that globally unique backup ID, it
tries to publish an identity-bound abandonment marker. Retention deletes
such an incomplete prefix only after `orphan_grace_hours`, after re-reading the
objects and validating that marker. Age alone is never deletion authority:
an old markerless prefix without an abandonment marker is left untouched and
reported as requiring manual review. This prevents cleanup from deleting a
slow upload that is still active on another host.

### Inspect, verify, and retrieve

```bash
# Completed backups owned by this project/environment
odooctl backup-remote list production
odooctl backup-remote list production --json

# Stream and hash every remote object; "latest" is the default
odooctl backup-remote verify production
odooctl backup-remote verify production \
  --backup production_2026-07-30_020000_deadbeefcafefeed01234567

# Verify first, then stage, fsync, and atomically publish the local download
odooctl backup-remote download production --backup latest
odooctl backup-remote download production \
  --backup production_2026-07-30_020000_deadbeefcafefeed01234567 \
  --destination recovered-backups
```

`backup-remote verify` also reconciles remote GFS retention. A retention alert
is recorded without pretending that a verified upload is corrupt. The explicit
verify command exits non-zero on any reconciliation alert under both
`required` and `best_effort`, so scheduled verification reliably alerts an
operator. The policy difference applies to backup creation: `best_effort`
keeps the validated local backup command successful with a warning, while
`required` fails it. Downloads refuse to overwrite an existing backup
directory.

## Scheduling backups, verification, and drills

`odooctl schedule` renders systemd service/timer pairs or cron entries; it does
not install them. Supported schedule names are `backup`,
`backup-remote-verify`, `dr-drill`, and `doctor`. Generated backup invocations
always include `--verify`.

```bash
odooctl schedule backup --env production --interval daily \
  --environment-file /etc/odooctl/acme.env
odooctl schedule backup-remote-verify --env production --interval weekly \
  --environment-file /etc/odooctl/acme.env
odooctl schedule dr-drill --env production --interval weekly \
  --environment-file /etc/odooctl/acme.env

# Render a cron entry instead
odooctl schedule backup --env production --format cron \
  --environment-file /etc/odooctl/acme.env
```

For systemd, the generated service contains
`EnvironmentFile=/etc/odooctl/acme.env`. For cron, the generated command
sources the file with `set -a` so its assignments are exported. Use simple
`NAME=value` entries compatible with both formats, restrict the file to the
service account (normally mode `0600`), and never commit it. Unit names include
a project/root identity hash so separate checkouts do not overwrite one
another's schedules.
