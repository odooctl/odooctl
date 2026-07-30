# Execution Modes

`odooctl` supports two PostgreSQL execution modes. These are independent of
the workload runtime: Docker Compose may use either mode, while Kubernetes uses
host mode against an externally managed PostgreSQL endpoint.

## Docker mode

Use for Docker Compose Odoo stacks where PostgreSQL is reachable inside the Compose network but not from the host.

```yaml
runtime:
  execution_mode: docker
postgres:
  service: db
  internal_host: db
  user: odoo
  password_env: ODOO_DB_PASSWORD
```

Operations run through the DB service:

- backup: `docker compose exec -T db pg_dump ...`
- restore: `docker compose exec -T db pg_restore ...`
- SQL: `docker compose exec -T db psql ...`

Binary backup streams are captured as bytes, not text, so log redaction cannot corrupt PostgreSQL custom-format dumps.

## Host mode

Use when the operator host has PostgreSQL client tools and direct network access to the database.

```yaml
runtime:
  execution_mode: host
postgres:
  host: localhost
  port: 5432
  user: odoo
  password_env: ODOO_DB_PASSWORD
```

Host mode is backward-compatible with the original MVP behavior.

See [Kubernetes runtime](kubernetes.md) for Kubernetes workload execution and
CloudNativePG integration.
