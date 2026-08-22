# Getting started with odooctl

This guide takes a globally installed `odooctl` from zero to a first verified Docker Compose project.

## 1. Install

Install the current stable release from PyPI:

```bash
pipx install 'odooctl==0.2.0'
# or: uv tool install 'odooctl==0.2.0'
odooctl --version
```

The `0.3.0b1` beta is opt-in and never an implicit stable upgrade. Use the
exact beta/rollback commands in [Installation](installation.md#release-channels).

Install optional S3 support only when you need remote backup uploads:

```bash
pipx install 'odooctl[s3]'
# or
uv tool install 'odooctl[s3]'
```

## 2. Prepare a project repo

Run `odooctl` from a tracked Odoo project directory that contains your `docker-compose.yml`, addons, Odoo config, and `odooctl.yml`.

You do not have to write `odooctl.yml` by hand: `odooctl import <path>` generates it from an existing Docker Compose deployment (read-only preview first, `--yes` to write), and `odooctl setup` scaffolds it for a brand-new project from a catalog stack template.

For Docker Compose deployments, prefer Docker-native execution mode so operators do not need host PostgreSQL clients:

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
odoo:
  service: odoo
  db_host: db
  db_user: odoo
  db_password_env: ODOO_DB_PASSWORD
```

## 3. Register the project

```bash
odooctl project add acme --path /srv/odoo/acme
odooctl project use acme
odooctl project current
```

You can also run one-off commands without registry state:

```bash
odooctl -C /srv/odoo/acme doctor
```

## 4. Run preflight checks

Set referenced secrets in the operator environment, then run `doctor`:

```bash
export ODOO_DB_PASSWORD='change-me'
odooctl -p acme doctor
```

`doctor` verifies config loading, project paths, required environment variables, Docker Compose files, sanitization SQL files, and redaction-secret quality. Short/common test values such as `odoo` can be listed under `redaction.ignore_values` for local experiments.

## 5. Back up production

```bash
odooctl -p acme backup production --verify
```

A full backup contains:

- `db.dump` — PostgreSQL custom-format dump.
- `filestore.tar` — plain POSIX tar archive of the Odoo filestore.
- `manifest.json` — project/environment metadata and checksums.
- Optional redacted Odoo config snapshot.

## 6. Create or refresh staging

Define a non-production environment with `clone_from: production`, `sanitize: true`, and distinct DB/filestore names. Then run:

```bash
odooctl -p acme clone production staging --sanitize
```

For local multi-database stacks served by one Odoo HTTP service, enable `db_selector: true`; health checks will use `?db=<database>`.

## 7. Schedule unattended checks/backups

Generate installable systemd or cron snippets:

```bash
odooctl -p acme schedule backup --env production --format cron --interval '0 2 * * *'
odooctl -p acme schedule doctor --env production --format systemd --interval daily
```

`odooctl schedule` prints files/snippets only. Review and install them with your OS process manager.

## 8. Before production use

- Confirm `odooctl doctor` passes.
- Run a real `backup`, a restore into a safe target, and a `clone --sanitize` rehearsal.
- Confirm `/web/health` (or your configured health path) returns HTTP 200. Redirects are treated as unhealthy: point the healthcheck at a 2xx endpoint (`/web/health` exists on Odoo 15+).
- Store real secrets outside the repo and reference them through environment variables.
