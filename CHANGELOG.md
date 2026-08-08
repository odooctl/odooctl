# Changelog

All notable changes to `odooctl` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **License changed from MIT to AGPL-3.0-or-later** with a commercial
  license available for proprietary embedding/resale — see `LICENSING.md`.
  (No prior release was distributed under MIT.)
- Contributions now require a Developer Certificate of Origin sign-off
  (`git commit -s`) plus a commercial-relicensing grant; see
  `CONTRIBUTING.md`.

### Added

- A workload runtime protocol and central factory for deploy, restart, exec,
  logs, status, and Odoo command execution. Docker Compose remains the
  configuration-compatible default.
- A project/environment-scoped Kubernetes runtime with canonical Deployment,
  Service, Ingress, filestore PVC, and Namespace rendering; secret-key
  references; exec/log/rollout/status support; and fail-closed ownership
  labels. Externally managed PostgreSQL is the default, with CloudNativePG
  integration documented.
- Declarative GitOps environment overlays and deterministic pull-request
  environments with isolated namespace/domain/database/filestore identities,
  fail-closed neutralized clone initialization, expiry metadata, guarded
  cleanup, and GitHub Actions/Argo CD examples.
- Validated recreate, rolling, blue/green, and NGINX canary strategies with
  revision-owned candidates, stable-Service promotion, Odoo readiness/public
  health gates, native or selector-based automated rollback, and explicit
  shared-database schema-migration limitations.
- Reproducible, path-scoped k3d clusters that reuse production Kubernetes
  resources, add disposable PostgreSQL, generate grouped Tilt resources with
  image rebuild/live sync, exercise deploy/native neutralize/backup/restore/
  progressive rollback in a smoke lifecycle, and require an exact ownership
  record for teardown.
- Policy-controlled remote portable backups with `required`, `best_effort`,
  and `disabled` modes; post-upload byte verification; project-scoped S3
  namespaces; globally unique 24-hex-suffixed backup IDs; UTC
  daily/weekly/monthly GFS retention with a completed-backup concurrency grace;
  abandonment-marker/grace-period orphan reconciliation; plus the
  `backup-remote list`, `backup-remote verify`, and `backup-remote download`
  commands.
- Schedule generation for verified backups, remote verification, and DR drills,
  including systemd `EnvironmentFile=` and cron environment-file loading.
- Isolated DR drills that restore the newest owned backup into disposable
  PostgreSQL-on-tmpfs and Odoo containers on an internal network, with
  drill-only filestore/config volumes, loopback-only health probing, exact
  project/environment validation, and exhaustive teardown after partial
  failure.
- Open-source contribution infrastructure: label taxonomy
  (`.github/labels.yml`) with automated sync, path-based PR auto-labeling,
  issue triage flow (`status/needs-triage`), a documentation issue
  template, issue-form contact links, `SUPPORT.md`, and GitHub Discussions.
- Production-to-staging clone and cross-environment restore now run Odoo's
  native `neutralize` command when available, followed by odooctl extension
  sanitizers and fail-closed verification. Policies support `required`,
  `preferred`, and `disabled`; results are recorded without secrets.
- Provider-native DR snapshots for AWS EBS multi-volume sets and Hetzner
  Cloud server disks, with a separate durable requested/pending/complete/failed
  manifest index, one config-bound environment/source identity, canonical
  provider scope and reconstruction metadata, explicit consistency labels,
  reconciliation after interruption, bound protected-environment pre-deploy
  policy, plan-only recovery by default, idempotent isolated recovery resources
  (including private-network-only Hetzner servers), partial-resource tracking,
  and exact snapshot/resource confirmation before execution.
- PostgreSQL WAL archiving and point-in-time recovery with an independent
  S3-compatible archive, immutable WAL receipts, verified physical base
  backups, recovery-graph retention, isolated digest-pinned recovery,
  restore-to-new-database verification, crash-reconcilable OID-fenced cutover,
  and compare-and-swap cross-host leases with explicit expired-lease recovery.
- A filestore backend and migration contract for local paths, Docker volumes,
  POSIX-mounted object storage, S3-compatible content-addressed mirrors, and
  operator-selected Odoo storage modules. The new `odooctl filestore` workflow
  plans immutable inventories, syncs and reads back SHA-256 checksums, performs
  compare-and-swap cutover, and keeps source and remote-content deletion behind
  separate exact-confirmation controls.

### Security

- Removed remote-destination ambiguity: unavailable S3 dependencies,
  credentials, or provider access now follow the configured policy and never
  count another destination as an off-host copy.
- DR drills no longer restore into the live PostgreSQL cluster or live Odoo
  filestore. Generated database credentials remain off argv, the drill has no
  external network egress, and cleanup failure is reported as drill failure.

## [0.2.0] - 2026-07-19

Production-hardening pass driven by the 2026-05-31 security audits
(reports 1–4 in `experiments/2026-05-31-kanban-scan-suite/`).

### Security

- Removed all `sh -lc` command composition: filestore operations use
  list-argv execs and DB cloning pipes `pg_dump` into `pg_restore` through a
  new no-shell `run_pipe()` helper. A guard test keeps shell sinks out.
- Config-boundary validation: environment/db/service/volume names must match
  a strict identifier rule; domains must be valid DNS hostnames (normalized
  to lowercase) and are re-validated when Traefik rules are built.
- Every protected-environment policy check goes through `is_protected()`
  (name `production` or `tier: production`); literal name comparisons are
  gone and a guard test keeps them out.
- Database passwords never appear on process argv (passed via
  `docker compose exec -e PGPASSWORD` name-only injection); command errors,
  operation-store errors, and streamed events are redacted.
- Sanitization now also covers the legacy `payment_acquirer` table, freezes
  `web.base.url`, clears OAuth client secrets and IAP tokens, and deletes
  Odoo 19 `auth_passkey` WebAuthn credentials; crons are disabled under
  every profile including `minimal`.
- Capability tokens default to a 300 s TTL; consumed-nonce records are
  purged after 2 h; `ODOOCTL_API_KEY` must be at least 32 characters.
- Operation cancel is a write action (viewers get 403) and `/operations/*`
  endpoints enforce the token's project scope.
- Audit chains can be HMAC-keyed via `ODOOCTL_AUDIT_KEY`, making
  truncate-and-rehash tampering detectable.
- Path containment for backup ids, registry config paths, project names,
  migration report paths, and `import --output` (new `--allow-outside`).
- `security token mint`/`verify` sign with `ODOOCTL_API_KEY` by default —
  the key the API and runner actually verify with.

### Safety

- `restore` restores into a temporary database and swaps only after
  `pg_restore` succeeds (verify-before-destroy), for both same-environment
  and cross-environment restores.
- A failed protected-environment deploy automatically restores its own
  pre-deploy backup when the database may have been mutated, and records
  the recovery outcome in deployment metadata.
- `restore` and `rollback --mode full` require `--yes` or interactive
  confirmation.
- Health checks require HTTP 2xx and treat redirects as unhealthy; the
  default health path is now `/web/health` (Odoo 15+).
- `runner --once` exits non-zero when the processed operation failed;
  `runner --fail-fast` stops the loop on the first failure.

### Added

- GitHub Actions CI (ruff, pytest on Python 3.11–3.13 with a coverage
  floor, package build + wheel smoke test) and a tag-driven release
  workflow using PyPI trusted publishing.
- Real-Odoo integration harness (`tests/integration/`): disposable Docker
  stacks per Odoo version covering the full operator lifecycle, including
  API-enqueue → runner execution parity.
- `--project`/`--project-dir` regression matrix across every config-taking
  command; the selector is threaded explicitly through `typer.Context`.
- Shell completion; web UI empty states, running-operation indicator, and
  role-aware Migrate gating; MkDocs documentation site configuration.

### Changed

- `click` is a direct dependency (typer ≥ 0.27 no longer provides it).
- The default healthcheck path changed from `/web/login` to `/web/health`.

## [0.1.0] - 2026-05-30

Initial public release of `odooctl`, a CLI-first, Odoo-aware deployment
platform for self-hosted Odoo projects using Docker Compose.

This release closes the v-next milestones M0 through M5 and ships the MVP
foundation: Docker-native database and filestore operations, project and
environment management, scheduled operation generation, install metadata,
secret redaction, optional real S3 uploads, documentation, and tests.

### Added

- **M0 — Test-harness hygiene.** Pytest environment isolation and
  registered `unit`, `integration`, and `docker` markers so the default
  suite runs without Docker or live infrastructure.
- **M1 — Project context and `doctor`.** `ProjectContext` resolves all
  paths relative to the configuration root, and a new `odooctl doctor`
  command runs side-effect-free preflight checks with human and JSON
  output. Context is threaded through deploy, backup, clone, restore,
  rollback, status, logs, update-modules, and validate.
- **M2 — Docker execution mode.** Additive `runtime.execution_mode` and
  container PostgreSQL/Odoo configuration fields. New host and Docker
  PostgreSQL adapters, binary-safe command helpers, Docker Compose byte
  stream helpers, and an adapter factory. Module updates build official
  image-safe Odoo invocations with `-c`, `--db_host`, `--db_user`, and
  `--db_password` from config and environment.
- **M3 — Safer clone and Docker filestores.** Clone restores into a
  temporary database, sanitizes it before exposure, then terminates target
  connections, drops the old target, and renames into place. Named-volume
  filestore adapter for Docker, with archive/restore/copy command
  construction and same-stack `db_selector` validation.
- **M4 — Project and environment registry.** XDG-backed global project
  registry with `odooctl project add/list/use/remove/current`, plus global
  `-p/--project` and `-C/--project-dir` resolution. Environment lifecycle
  commands: `odooctl env list/show/create/destroy`, including a guarded
  env purge.
- **M5 — Productization.** PyPI/pipx install metadata, scheduled
  operation generation (`odooctl schedule backup`, `odooctl schedule
  doctor`), precise secret redaction with configurable
  `redaction.min_secret_length` and `redaction.ignore_values`, an optional
  real S3 adapter behind the `s3` extra, expanded documentation under
  `docs/`, runnable examples under `examples/`, and broader test
  coverage.

### Security

- Secrets are referenced via environment variables and `*_env` config
  fields and are never stored in the repository or in checked-in
  configuration.
- Logs redact environment values whose variable names look secret-bearing
  (`PASSWORD`, `SECRET`, `TOKEN`, `KEY`, `PASSWD`); the redaction policy
  is configurable.
- `odooctl doctor` warns when referenced secrets are shorter than the
  configured minimum or fall on the redaction ignore list.

[Unreleased]: https://github.com/odooctl/odooctl/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/odooctl/odooctl/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/odooctl/odooctl/releases/tag/v0.1.0
