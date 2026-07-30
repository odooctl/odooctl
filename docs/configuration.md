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
- `backups`: local backup path, optional S3 remote storage, and retention policy.
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
  remote:
    type: s3
    bucket: acme-odoo-backups
    prefix: acme/production
    region: eu-central-1
    endpoint_env: ODOO_S3_ENDPOINT
    access_key_env: ODOO_S3_ACCESS_KEY
    secret_key_env: ODOO_S3_SECRET_KEY
```

If `boto3` or credentials are unavailable, `odooctl` warns and mirrors the remote backup under `.odooctl/remote-backups/` so backup creation does not fail because remote upload is unavailable.

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
