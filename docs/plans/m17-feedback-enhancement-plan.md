# Feedback Enhancement Plan

Date: 2026-07-21
Status: locked (decisions confirmed by Rami, 2026-07-21)
Relates to: `m16-roadmap-2026-07-production-readiness.md`, plan pack `README.md`

> **Pre-adoption freedom:** the project has no external users yet. Until first real
> adoption, breaking changes to config schema, CLI, API, and storage layouts are
> allowed whenever they produce a better 1.0 design. This explicitly overrides
> plan-pack invariant #7 ("existing CLI/config remains backward compatible") for now;
> that invariant re-activates at 1.0 GA.

## Context

A Reddit thread on safe Odoo backups/clones/rollbacks surfaced two kinds of feedback:

- Platform engineers recommending the full k8s stack (CloudNativePG, ArgoCD, JuiceFS/seaweedfs).
- Small-scale operators using simple tools (borgmatic, VM snapshots, `odoo-bin neutralize`).

Decision: **do not pivot to Kubernetes.** The target market (1–few VMs, Docker Compose,
no platform team) is real and underserved; the k8s answers describe the mature end state,
not our wedge. But the thread produced concrete, correct steers, captured below.

## 1. Neutralize-first sanitization

Today `odooctl/odoo/sanitize.py` is ~150 lines of hand-rolled guarded SQL. Odoo ≥ 16
ships `odoo-bin neutralize` (and `--neutralize` on restore), which runs per-module
`neutralize.sql` scripts maintained upstream — it tracks new integration tables so we
don't have to.

Change the clone/sanitize pipeline to:

1. Run `odoo-bin neutralize` inside the container when target Odoo ≥ 16.
2. Then run our SQL as a **supplement**, covering what upstream does not:
   - pre-16 databases (SQL remains the primary mechanism there)
   - third-party modules without neutralize scripts (`queue_job`, `base_automation`)
   - `ir_config_parameter` secret/webhook/token scrubbing
   - base URL rewrite
3. Record in the clone manifest which mechanism(s) ran.

Positioning line: "uses Odoo's own neutralization, plus the gaps it leaves."

General principle for 1.0: for each workflow, check whether `odoo-bin` or
`click-odoo-contrib` already provides the primitive; odooctl's value is the
orchestration/safety layer, not reimplemented primitives.

## 2. Orchestrator-agnostic core (direction, not a 1.0 deliverable)

The durable value is Odoo domain logic: DB+filestore as one atomic unit,
restore-into-temp-then-swap, registry-level health checks, upgrade rehearsal,
promote/rollback. None of that is Compose-specific, and none of it is provided by the
k8s stack either.

- Keep the runner/adapter seams clean so a k3s/k8s backend is a future feature, not a
  rewrite (already a locked constraint in the plan pack README).
- Do **not** build k8s support now. Note it in README as direction only.
- Do **not** rebuild platform features (PITR, replication, GitOps engines). Where
  storage robustness matters, integrate: evaluate restic/borg as backup repository
  backends (restic brings S3, encryption, dedup, retention for free).

## 3. Git sync (pull-based auto-deploy)

Already built: `deploy <env> --branch` does fetch/checkout/ff-only-pull + compose pull +
module update + healthcheck; `branch status` computes drift; `schedule` generates
systemd timers; `auto_deploy: bool` exists in config (`config.py`) but is **dead — nothing
reads it**.

Deliverables:

1. `odooctl sync <env>`: check drift; if behind and `auto_deploy: true`, run the existing
   deploy pipeline (pre-deploy backup + healthcheck + explicit rollback path). No-op
   with a clear message otherwise.
2. Wire `sync` into `odooctl schedule` (systemd timer, e.g. every 2–5 min).
3. Docs: pull-based sync is the primary CI/CD model (read-only deploy key, no inbound
   secrets, works behind NAT — the ArgoCD model at Compose scale). The generated GitHub
   Actions workflow is secondary and requires a self-hosted runner (it currently renders
   `runs-on: ubuntu-latest`, which cannot reach the VPS Docker daemon — document or fix).
4. Later (post-1.0): optional webhook endpoint on `odooctl serve` that triggers the same
   sync, as a latency optimization only. Polling stays the source of truth.

## 4. Config overlay for machine-local settings

`odooctl.yml` lives in the git repo and is shared. Add an untracked overlay
(`odooctl.local.yml`, gitignored) merged over the main config for machine-specific
values: ports, resource limits, TLS off, local paths. Precedence: env vars >
`odooctl.local.yml` > `odooctl.yml`.

This is the prerequisite for the local-dev story (§5).

## 5. Local dev instance from deployed production (post-1.0)

Decision: **same project, not separate projects.** One project = one repo = one
`odooctl.yml`, with environments spanning machines (production/staging on the VPS,
`local` tier on laptops). The multi-project registry remains for genuinely separate
deployments (different clients).

Model (Odoo.sh-style, self-hosted):

- **Code travels via git** — `git clone` + overlay (§4) gives a runnable definition.
- **Data travels via sanitized backups only** — invariant: raw production data never
  leaves the server; only neutralized artifacts do.

Deliverable sketch: `odooctl dev pull` / `dev up` — local odooctl authenticates to the
VPS `odooctl serve` API, requests a sanitized snapshot (server runs clone+neutralize,
streams artifact), local side restores into a local compose stack.

Server-side half already exists: `env open <name> --from <branch> --from-env production`.
Depends on: §1 (neutralize), §4 (overlay), and the clone `db_selector` atomic-rename fix.

Prior art to study: Odoo.sh branch model; Tecnativa doodba (best local-dev parity story;
dev-first/ops-weak — we converge from the ops side).

## 6. Product shape: multi-user GUI system with odooctl as engine (locked)

Decision (Rami, 2026-07-21): odooctl is a full system with a proper GUI and the CLI
engine behind it, **with real user accounts as a first-class concept** — not just an
API surface. The system must know who owns what and who did what.

The architecture side is already the locked plan-pack thesis ("CLI-first, UI-on-top";
FastAPI + SPA via `odooctl serve`; web/runner split; m13 UI MVP shipped). The new locked
scope is the identity layer on top of it:

### User accounts and identity (locked)

- [x] **User store**: persistent user accounts (email + password with proper hashing,
      e.g. argon2), building on the existing `principals.py` identity model.
      *(2026-07-21: `security/users.py`, salted scrypt with scheme-prefixed hashes so
      argon2 can slot in without invalidating stored hashes.)*
- [x] **Login flows**: email/password first; **Google OAuth (OIDC) as a second
      provider** — design the auth layer provider-pluggable from the start so adding
      OIDC is config, not a refactor. *(email/password shipped; user records carry
      `provider`/`provider_subject` fields for OIDC, which stays post-1.0.)*
- [x] **Ownership**: projects/environments record an owning user/team; operations
      record the acting principal. "Who owns what" is queryable in API and visible
      in UI.
- [x] **Attribution**: every mutation's audit record carries the authenticated
      principal (the audit store already takes `actor` — wire real identity through
      instead of `"cli"` literals).
- [x] **Sessions/tokens**: browser sessions for the SPA; API tokens for CLI/CI
      (capability-token primitives in `security/tokens.py` are the starting point).
      *(revocable cookie sessions in `security/sessions.py`; bearer tokens unchanged.)*

### Enforcement and team features

- [x] Role → action enforcement on all API mutations (admin / operator / developer /
      viewer) — RBAC primitives exist in `odooctl/security/`, wire them everywhere.
      *(2026-07-21: all mutating routes gated, plus an introspection test that fails
      if any mutating route lacks an auth dependency. Role names kept as the shipped
      owner/admin/operator/viewer set — "developer" in the parenthetical was loose
      wording; a developer tier can be added to the matrix later without breakage.)*
- [ ] Approval gates on protected flows (promote to production requires approver — the
      m9 plan already anticipated "required approvers later when RBAC/UI exists").
- [ ] Team-facing UI views: operation timeline, audit log, per-environment status,
      who-did-what.
- [ ] Invariant to preserve: the UI is a client of the API only; no second code path.
      CLI parity remains — everything doable in UI is doable headless (CLI acts as a
      local-admin principal or via an API token).

## 7. Flexible storage/DB backends: S3 filestore, S3 backups, managed Postgres (locked)

Decision (Rami, 2026-07-21): all three directions confirmed — managed Postgres
first-class, restic as backup backend, filestore-on-S3 awareness (not our own sync).

Current state (verified in code):

- **S3 backups: already shipped** — `adapters/s3.py`, `backups.remote` config with
  client-side encryption metadata.
- **DB seam exists** — `DbAdapter` Protocol with `DockerPostgresAdapter` and
  `HostPostgresAdapter`.
- **Filestore seam exists** — `FilestoreBackend` Protocol.

Remaining gaps, in priority order:

1. **Managed Postgres as first-class** (RDS / DO / Crunchy): extend the `DbAdapter` seam
   for TLS/remote connections, no-superuser constraints, provider-safe dump/restore,
   and doctor checks that detect and validate a managed DB. Highest leverage: it removes
   the scariest component from the user's VPS. Additional upside (Rami): managed
   providers ship their own backups/PITR, so odooctl can treat provider backups as the
   DB safety net and focus on the Odoo-level pieces (filestore pairing, sanitized
   clones, restore verification). Note: odooctl-created restore points must still pair
   a DB state with a filestore state — with a managed DB that means recording the
   provider snapshot/PITR timestamp alongside the filestore snapshot in the manifest.
2. **Restic repository backend for backups** (see §2) — supersedes bespoke retention and
   adds any-S3 target, dedup, encryption. Given pre-adoption freedom (header note),
   restic may replace rather than sit beside the bespoke remote-backup format if that
   simplifies the code.
3. **Filestore-on-S3 awareness**: do not build our own live sync. Support and detect the
   OCA `fs_attachment`/storage modules; make backup/clone flows understand "filestore is
   in a bucket" (snapshot via bucket copy or restic, not tar of a local dir).

Principle: flexibility = adapter interfaces at the 1.0 boundary, implementations can land
after 1.0 without config breakage.

## Sequencing

| Order | Item | Target |
| --- | --- | --- |
| 1 | §1 neutralize-first sanitize | 1.0 |
| 2 | §3 `sync` + `auto_deploy` wiring | 1.0 |
| 3 | §4 config overlay | 1.0 |
| 4 | §6 user accounts (email/password), sessions/tokens, ownership + attribution | 1.0 (identity is hard to retrofit after GA) |
| 5 | §6 RBAC wiring on all API mutations | 1.0 (security-relevant before GA) |
| 6 | §7.1 managed Postgres first-class | post-1.0, design seam + config shape at 1.0 |
| 7 | §5 `dev pull` local dev | post-1.0 |
| 8 | §7.2 restic backend, §7.3 S3 filestore awareness | post-1.0 (restic-replaces-bespoke decision may pull it earlier) |
| 9 | §6 Google OIDC login, approval gates, team UI views | post-1.0 |

## Verification standard

Per plan-pack README: `uv run pytest -q`, `uv run ruff check .`, live check against
`experiments/odoo19-community-staging` for engine-touching work, update `progress.md`.
For §1 specifically: real-fixture evidence that `odoo-bin neutralize` ran and the
supplement SQL applied on an Odoo 19 clone.
