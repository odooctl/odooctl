# <img src="https://raw.githubusercontent.com/odooctl/odooctl/master/docs/assets/logo.svg" width="26" alt=""> odooctl

[![CI](https://github.com/odooctl/odooctl/actions/workflows/ci.yml/badge.svg)](https://github.com/odooctl/odooctl/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://github.com/odooctl/odooctl/blob/master/pyproject.toml)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

`odooctl` is a CLI-first, Odoo-aware control plane for self-hosted Odoo on
Docker Compose or Kubernetes — think open-source Odoo.sh for your own
infrastructure. It handles the operational lifecycle generic deploy tools miss:
verified backups, sanitized staging clones, module updates, rollback,
environment promotion, upgrade rehearsal, and health checks, all Odoo- and
PostgreSQL-aware. Existing Compose projects remain the zero-migration default.

## Why odooctl

- **Safety by default.** Deploys to protected environments take a database + filestore backup first; failed module updates and failed health checks stop the deployment with a non-zero exit.
- **Verify before destroy.** Restores land in a temporary database and are only swapped into place after the restore succeeds — a bad backup never destroys a working environment.
- **Off-host backup policy is explicit.** S3 copies can be `required`,
  `best_effort`, or `disabled`, with byte verification, project-scoped object
  namespaces, and retention that never treats an unknown old upload as safe to
  delete.
- **Filestore migrations verify every byte.** Local paths and Docker volumes
  can migrate to a POSIX object mount or a project-scoped S3-compatible
  generation; cutover is explicit and source deletion is a separate,
  fully-confirmed operation.
- **Staging clones you can trust.** `odooctl clone production staging` sanitizes by default: mail servers, crons, payment providers, queue jobs, OAuth secrets, IAP tokens, and webhook URLs are neutralized — including wiping Odoo 19 WebAuthn passkeys — so staging cannot email customers or charge cards.
- **Adopt what you already run.** `odooctl import` detects an existing compose deployment read-only, previews the generated config, and only writes on `--yes` — then registers the project, runs preflight checks, and takes a safety backup.
- **Protected environments.** Production-tier environments require elevated confirmation for destructive operations, in the CLI, API, and web UI alike.
- **Secrets stay secret.** Config references secrets by env-var name only; logs, errors, and streamed operation events are redacted.
- **A local API and web UI, RBAC'd.** `odooctl serve` runs a localhost-only REST API plus a no-build web UI with viewer/operator/admin/owner roles, HMAC bearer tokens, and a privileged runner kept in a separate process.
- **Upgrade rehearsal, not upgrade roulette.** `odooctl migrate rehearse` clones production into a throwaway DB, runs the (OpenUpgrade-backed) upgrade there, health-checks, and writes a report. Production is never touched.

## Quickstart

### A. Adopt an existing Docker Compose deployment

```bash
# Read-only detection and preview — no containers touched, no secrets stored
odooctl import /srv/odoo/acme

# Adopt: write odooctl.yml, register the project, run doctor, take a safety backup
odooctl import /srv/odoo/acme --name acme --yes

# First verified backup, then a sanitized staging environment in one command
odooctl -p acme backup production --verify
odooctl -p acme env create staging --clone-from production
```

### B. Start a fresh project

```bash
mkdir acme && cd acme
odooctl setup --name acme --stack odoo-19-community   # or run interactively: odooctl setup
```

`setup` scaffolds `odooctl.yml` from a pinned stack template (secrets referenced by env-var name, never inlined). Edit the domain, database names, and your `docker-compose.yml`, then:

```bash
export ODOO_DB_PASSWORD='a-strong-secret'
odooctl validate
odooctl doctor
odooctl deploy production --branch main
```

Either way, day-2 operations look like:

```bash
odooctl backup production --verify
odooctl clone production staging --sanitize
odooctl update-modules staging --modules sale,stock
odooctl restore staging --backup latest
odooctl rollback production --mode code
odooctl migrate rehearse --env production --to 19.0 --openupgrade
odooctl status
```

## Features

| Feature | Command(s) | Docs |
| --- | --- | --- |
| Import / takeover of existing stacks | `odooctl import` | [docs/getting-started.md](docs/getting-started.md) |
| Deploy with pre-deploy backups | `odooctl deploy` | [docs/deployment.md](docs/deployment.md) |
| Verified local/remote backups, safe restores | `odooctl backup --verify`, `odooctl backup-remote`, `odooctl restore` | [docs/backup-restore.md](docs/backup-restore.md) |
| Sanitized staging clones | `odooctl clone`, `odooctl env create` | [docs/staging-clone.md](docs/staging-clone.md), [docs/environments.md](docs/environments.md) |
| Rollback (code or full) | `odooctl rollback` | [docs/rollback.md](docs/rollback.md) |
| Environment promotion | `odooctl promote` | [docs/environments.md](docs/environments.md) |
| Preflight checks | `odooctl doctor` | [docs/doctor.md](docs/doctor.md) |
| Upgrade rehearsal (17 → 18 → 19) | `odooctl migrate matrix / scan / rehearse` | [docs/migration.md](docs/migration.md) |
| Disaster recovery and provider snapshots | `odooctl dr drill`, `odooctl dr snapshot` | [docs/disaster-recovery.md](docs/disaster-recovery.md) |
| PostgreSQL WAL archiving and PITR | `odooctl pitr` | [docs/pitr.md](docs/pitr.md) |
| Object-storage filestore migration | `odooctl filestore` | [docs/filestore-storage.md](docs/filestore-storage.md) |
| Kubernetes runtime and production manifests | `odooctl deploy`, `odooctl logs`, `odooctl status` | [docs/kubernetes.md](docs/kubernetes.md) |
| GitOps and expiring PR environments | `odooctl gitops` | [docs/gitops.md](docs/gitops.md) |
| Progressive rollout and automated rollback | `odooctl deploy`, `odooctl promote` | [docs/progressive-deployment.md](docs/progressive-deployment.md) |
| Domains / SSL via reverse proxy | `odooctl domain` | [docs/domains-ssl.md](docs/domains-ssl.md) |
| Local REST API + operation queue | `odooctl serve`, `odooctl runner`, `odooctl ops` | [docs/api.md](docs/api.md) |
| RBAC, tokens, secret store | `odooctl security` | [docs/rbac.md](docs/rbac.md) |
| Stack / addon catalog | `odooctl catalog`, `odooctl setup` | [docs/catalog.md](docs/catalog.md) |
| Scheduled backups / checks / DR drills | `odooctl schedule` | [docs/backup-restore.md#scheduling-backups-verification-and-drills](docs/backup-restore.md#scheduling-backups-verification-and-drills) |

Provider snapshots are deliberately coarse DR artifacts: one provider source is
bound to one environment, recovery is plan-only by default, and execution
creates isolated resources only after exact snapshot/source confirmation. They
supplement rather than replace portable database + filestore backups; see the
[lifecycle and cost notes](docs/disaster-recovery.md#provider-snapshots).

## Web UI

The optional web UI is a no-build vanilla-JS SPA served by the local API:

```bash
pip install 'odooctl[api]'
export ODOOCTL_API_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
odooctl serve            # binds 127.0.0.1:8787 by default
```

Open `http://localhost:8787/` and paste a token minted with:

```bash
odooctl security token mint --action api --env "*" --project "*" --role operator
```

<!-- TODO: screenshot -->

The server is localhost-only by design and speaks plain HTTP — do not bind it to a public address; use an SSH tunnel for remote access. The API process never touches Docker or PostgreSQL directly: privileged work is delegated to `odooctl runner` through a durable queue with short-lived, single-use capability tokens. See [docs/web-ui.md](docs/web-ui.md) and [docs/api.md](docs/api.md).

## How it compares

Compared to **Odoo.sh**, odooctl is self-hosted and free: your server, your data, no per-worker pricing — with the same core workflow of environments, backups, staging builds, and upgrade testing. Compared to **doodba**, odooctl is not a project scaffolding framework; it manages the operational lifecycle (backup, clone, restore, promote, rehearse) of whatever compose project you already have — including one built with doodba. Compared to **hand-rolled compose scripts**, every destructive path here ships with tested safety rails: pre-deploy backups, restore-into-temp-then-swap, sanitization that is on by default, protected-environment confirmation, and secret redaction, backed by 700+ unit tests plus a real-Odoo integration suite.

## Supported Odoo versions

Odoo **17, 18, and 19** (Community) are integration-tested: a disposable-stack harness in `tests/integration/` runs the full operator lifecycle against real `odoo:17`/`odoo:18`/`odoo:19` images. See [docs/odoo-versions.md](docs/odoo-versions.md) for per-version notes.

## Install

PyPI publication is coming soon — `pipx install odooctl` / `uv tool install odooctl` will work once released. Today, install from source:

```bash
git clone https://github.com/odooctl/odooctl && cd odooctl
pipx install .          # or: uv tool install .
odooctl --help
```

The host also needs Docker Engine with the Compose plugin and `tar`. With the recommended `execution_mode: docker`, PostgreSQL client tools run inside your DB container, so the host needs none. See [docs/installation.md](docs/installation.md).

## Development

```bash
uv venv
uv pip install -e '.[dev]'
pytest -q                              # unit suite
pytest -m integration tests/integration  # opt-in real-Odoo matrix (needs Docker)
```

## Safety defaults

- Deploys to protected environments create database and filestore backups first.
- Module-update and health-check failures fail the deployment with a non-zero exit.
- Restores go into a temporary database and are swapped in only after success.
- DR drills restore into disposable PostgreSQL and Odoo containers on an
  internal network with dedicated volumes; live services and data volumes are
  never restore targets.
- PITR restores into an isolated PostgreSQL runtime and a new verified database
  before an explicit, OID-fenced cutover. WAL recovery is database-only and
  never claims to rewind the Odoo filestore.
- Filestore migration cutover re-verifies both the target and unchanged source;
  source deletion is a separate operation requiring exact environment and
  migration confirmations.
- Clone sanitization is on by default and disables mail, fetchmail, crons, payment providers, queue jobs, and automation rules; scrubs OAuth/IAP/webhook credentials; deletes Odoo 19 passkeys; and rewrites and freezes `web.base.url`.
- Secrets are referenced via environment variables (`password_env`) and redacted from logs, errors, and operation events; never commit secret values.
- The API/runner split is structurally enforced: `odooctl security runner-check` verifies the API layer imports no privileged adapters.

See [docs/security.md](docs/security.md) for the trust model.

## Contributing, security, license

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md). Good entry points are issues labeled [`good first issue`](https://github.com/odooctl/odooctl/labels/good%20first%20issue) and [`help wanted`](https://github.com/odooctl/odooctl/labels/help%20wanted). Report vulnerabilities privately per [SECURITY.md](SECURITY.md).

odooctl is free software, dual-licensed: [AGPL-3.0-or-later](LICENSE) for open use — including running it commercially for yourself or your clients — with a commercial license available for embedding or reselling it in proprietary products. See [LICENSING.md](LICENSING.md).
