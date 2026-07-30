# Configuration

Run `odooctl init` to create `odooctl.yml`.

Guidelines:

- Keep secrets out of YAML.
- Use `password_env` and provider-specific environment variable references.
- Define both `production` and at least one non-production environment so clone/deploy flows stay explicit.
- Point `staging.clone_from` at the source environment you want to clone.
- Prefer Docker execution mode for Docker Compose stacks where PostgreSQL is not exposed on the host.

Key sections:

- `project`: project name and Odoo version.
- `runtime`: Docker Compose file, reverse-proxy mode, and `execution_mode` (`docker` or `host`).
- `environments`: per-environment branch, scheme/domain/port, database, filestore, clone source, sanitization flag, `db_selector`, and module update list.
- `postgres`: host-side connection settings plus Docker service/internal-host settings for container-native operations.
- `odoo`: image, native CLI command, config path, addons paths, service name,
  DB flags for module updates, and container filestore root.
- `backups`: local backup path, UTC GFS retention, and policy-controlled S3
  remote storage.
- `snapshots`: optional AWS EBS or Hetzner Cloud coarse-DR provider and
  protected pre-deploy policy.
- `sanitization`: native Odoo neutralization policy, SQL extension files, and
  built-in staging safety toggles.
- `healthcheck`: path and retry timing used after clone/deploy/restore operations.
- `redaction`: log-redaction policy for sensitive environment values.

## Docker vs host execution

`runtime.execution_mode: docker` runs PostgreSQL operations through the configured Compose DB service. Use this for the common topology where the DB service is named `db` and port `5432` is not published to the host:

```yaml
runtime:
  type: docker_compose
  compose_file: docker-compose.yml
  execution_mode: docker
postgres:
  user: odoo
  password_env: ODOO_DB_PASSWORD
  service: db
  internal_host: db
```

`runtime.execution_mode: host` keeps the older behavior: `psql`, `pg_dump`, and `pg_restore` run on the operator host and connect to `postgres.host:postgres.port`.

## Multi-db local staging

For local/shared-stack experiments, two environments may share the same `domain` only when both use the same `stack` and set `db_selector: true`. Health checks then append `?db=<db_name>`.

```yaml
environments:
  production:
    stack: local
    scheme: http
    domain: localhost
    port: 18069
    db_selector: true
    db_name: odoo_prod
  staging:
    stack: local
    scheme: http
    domain: localhost
    port: 18069
    db_selector: true
    clone_from: production
    sanitize: true
    db_name: odoo_staging
```

Keep production isolation stricter for real routed deployments.

## Native Odoo neutralization

Production-to-staging clone/restore uses Odoo's native `neutralize` command
before applying odooctl's additional safeguards:

```yaml
odoo:
  cli_command: odoo
  addons_paths:
    - /mnt/extra-addons

sanitization:
  native_neutralize: preferred
```

`preferred` is the default. Use `required` when every configured image is
expected to support native neutralization, or `disabled` only when an image is
known not to provide it. Addon paths are passed to the native command so custom
module `neutralize.sql` files are included.

## S3 remote backups

Install the optional extra when you want real S3 uploads:

```bash
pipx install 'odooctl[s3]'
# or
uv tool install 'odooctl[s3]'
```

Configure a bucket and optional prefix/region/endpoint. Credentials can come from AWS defaults or the configured env vars:

```yaml
backups:
  local_path: backups
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
    endpoint_env: ODOO_S3_ENDPOINT
    access_key_env: ODOO_S3_ACCESS_KEY
    secret_key_env: ODOO_S3_SECRET_KEY
```

Remote uploads never switch to another destination. `policy` controls the
outcome when S3 support, credentials, upload, verification, or retention is
unavailable:

- `required`: fail the command after preserving the validated local backup and
  recording the remote failure.
- `best_effort`: keep the local operation successful and record/report a
  degraded remote result. This is the default.
- `disabled`: perform no remote work; `bucket` is optional in this mode.

`verify_after_upload` defaults to `true`. It reads the newly stored objects
back and compares their actual bytes with the manifest SHA-256 checksums before
publishing success. Set it to `false` only when immediate provider read-back is
too expensive, then schedule `odooctl backup-remote verify` separately.

`orphan_grace_hours` defaults to `24` and must be at least one hour. It is a
minimum age, not permission to delete arbitrary objects. Automatic cleanup of
an incomplete prefix additionally requires the publisher's valid abandonment
marker; an old markerless prefix without one is retained and reported for
manual review because another host may still be uploading it.

The configured `prefix` is a base only. New writes are automatically scoped
below `projects/<project-slug>-<project-hash>/`, and every read/delete validates
the manifest project and environment. Backup IDs include a UTC timestamp and
an unconditional random 24-hex suffix so concurrent publishers do not share an
ID.
Local and remote retention use `retention.daily`, `weekly`, and `monthly` as
UTC GFS tiers and always keep the newest owned backup. `retention.grace_hours`
defaults to one and temporarily protects every newly completed backup, so
concurrent hosts cannot prune each other's just-published restore points.

See [Backup and restore](backup-restore.md#remote-s3-copies) for object
publication ordering, remote inspection/download commands, and scheduling.

## Schedule environment files

The schedule generator supports `backup`, `backup-remote-verify`, `dr-drill`,
and `doctor`. Provide credentials without embedding them in generated units:

```bash
odooctl schedule backup-remote-verify --env production --interval daily \
  --environment-file /etc/odooctl/acme.env
```

Systemd output uses `EnvironmentFile=`; cron output sources the same path and
exports its assignments. Keep the file outside the repository, restrict it to
the service account, and use plain `NAME=value` lines compatible with both
formats. The generator prints configuration only—it does not install or enable
the timer or cron entry. An explicit or scheduled `backup-remote verify`
returns non-zero for any retention reconciliation alert even with
`best_effort`; that policy keeps backup creation available, not monitoring
green when remote cleanup needs attention.

## VM and volume snapshots

Snapshots are separate from portable database + filestore backups. Leave the
provider disabled when the project has no supported infrastructure provider:

```yaml
snapshots:
  provider: none
  pre_deploy: disabled
```

AWS EBS mode snapshots all attached instance volumes as one crash-consistent
set and restores to new, unattached volumes:

```yaml
snapshots:
  provider: aws_ebs
  environment: production
  pre_deploy: required
  aws_ebs:
    instance_id: i-0123456789abcdef0
    region: eu-central-1
    recovery_availability_zone: eu-central-1a
    include_root_volume: true
    completion_timeout_seconds: 600
    poll_interval_seconds: 15
```

Hetzner Cloud mode snapshots the server root disk. It refuses servers with
attached Hetzner Volumes because those would not be included:

```yaml
snapshots:
  provider: hetzner_cloud
  environment: production
  pre_deploy: preferred
  hetzner_cloud:
    server: odoo-production
    recovery_server_type: cx23
    recovery_location: nbg1
    recovery_network: odoo-recovery
    token_env: HCLOUD_TOKEN
```

`environment` is a security boundary, not a descriptive label. One snapshot
provider configuration is bound to one declared environment and its one
infrastructure source (`instance_id` or `server`). Snapshot create, reconcile,
and restore operations must use that exact environment. If `pre_deploy` is
enabled, the bound environment must also be protected. Other protected
environments still receive their portable pre-deploy backup, but do not use
this provider snapshot configuration; use a separate project/configuration
when infrastructure sources need independent snapshot policies.

`pre_deploy` accepts `disabled`, `preferred`, or `required`. Provider commands
use the installed AWS or hcloud CLI. Credentials stay in the standard AWS CLI
credential chain, the selected hcloud context, or the configured Hetzner token
environment variable. `recovery_network` must name an existing private Hetzner
network: recovery servers are attached to it, created powered off, and given no
public IPv4 or IPv6 address.

See [Disaster recovery](disaster-recovery.md) for required provider
permissions, recovery behavior, lifecycle costs, and typed confirmation. The
AWS zone is the destination for isolated recovery volumes; the source zone is
discovered and recorded in each snapshot manifest. The legacy key
`availability_zone` remains accepted as an input alias.

## Redaction policy

Sensitive environment variables are redacted in command output when their names contain markers like `PASSWORD`, `SECRET`, `TOKEN`, `KEY`, or `PASSWD`. Short/common values are deliberately not replaced globally because values like `odoo` make logs unreadable when over-redacted.

```yaml
redaction:
  min_secret_length: 6
  ignore_values:
    - odoo
    - admin
    - postgres
```

`odooctl doctor` warns when a referenced secret is too short or ignored by the redaction policy.

See `examples/odooctl.yml` for a complete starter configuration.
