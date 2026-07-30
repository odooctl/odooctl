# odooctl Control-Plane Plan Pack

Date: 2026-05-30
Status: active planning baseline

## Purpose

Replace the completed M0–M5 engine plans with the next product direction: turn `odooctl` from a verified CLI engine into an Odoo.sh-style control plane for self-hosted Odoo.

## Ground truth

Current engine is already verified for Odoo Docker operations:

- validate / doctor / status
- backup / restore
- production → staging clone
- sanitization
- module update
- Docker-native PostgreSQL and filestore handling
- S3 adapter
- docs/governance/release evidence
- real Odoo 19 Community fixture evidence

The next work is not another CLI-only hardening pass. The next work is a product/control-plane pass.


## Locked v1 product decisions

These are now hard constraints for the plan pack. Do not broaden scope unless Rami explicitly changes them.

### 1. Supported deployment mode

**v1 supports single-host Docker Compose only.**

- One host/server.
- One local Docker Engine / Docker Compose context.
- Multiple Odoo environments may run on the same host.
- Remote SSH runners, multi-host fleets, Kubernetes, and SaaS worker pools are explicitly out of v1 scope.
- Keep adapter seams clean so remote runners can be added later, but do not implement them in M6–M15.

### 2. Reverse proxy support

**v1 uses a Traefik adapter behind an explicit reverse-proxy abstraction.**

- Implement `ReverseProxyAdapter` interface.
- Ship `TraefikAdapter` first.
- Nginx and Caddy are future adapters, not v1 deliverables.
- Domain/SSL commands must be written against the abstraction, not directly hardcoded to Traefik in service callers.

### 3. UI technology

**v1 uses FastAPI API + static SPA served by `odooctl serve`.**

- FastAPI is an optional extra.
- Static SPA assets are served by the API process.
- The SPA talks only to API endpoints.
- No Django-style monolith.
- No server-side UI logic that duplicates service-layer logic.

### 4. Import safety contract

`odooctl import` detection is strictly read-only.

Non-negotiable rules:

- read-only during detection
- no restart
- no redeploy
- no DB writes
- no volume changes
- no secret printing
- preview-first
- backup-after-adoption

Any implementation that violates these rules is incorrect, even if tests pass.

## Product thesis

Build an open-source Odoo.sh-style control plane for self-hosted Odoo:

- Newcomers can install, create a managed Odoo stack, deploy production, clone staging, schedule backups, and operate from UI/CLI.
- Existing self-hosted users can import/take over their running Odoo deployment without redeploying or losing control.
- Operators get Odoo.sh-like environments, safe clone/sanitize/promote flows, operation logs, audit, domains, backups, and migration rehearsal.

## Strategic wedge

`community-sh` has useful newcomer UX, but it lacks the existing-user path.

Our killer feature:

> `odooctl import` detects an existing Odoo Docker/Compose deployment, generates `odooctl.yml`, verifies doctor checks, takes a tested backup, and creates a sanitized staging clone — without disrupting production.

## Architecture invariants

1. CLI-first, UI-on-top.
2. One service layer powers CLI, API, runner, and web UI.
3. Every mutation is an operation with events, audit, and locks.
4. Web/API tier never mounts Docker socket.
5. Privileged runner executes Docker/Postgres/filestore work.
6. Strong secrets, RBAC, redaction, typed confirmations.
7. Existing CLI/config remains backward compatible.
8. Evidence before support claims: tests + real Odoo fixture where applicable.

## Milestone order

1. `m6-service-layer.md` — extract command logic into reusable services and result models.
2. `m7-operation-engine.md` — durable operations, events, audit log, locks.
3. `m8-import-takeover.md` — existing Odoo import and newcomer setup wizard.
4. `m9-environment-branch-model.md` — Odoo.sh-like environments, branches, promote flow.
5. `m10-onboarding-catalog.md` — stack/addon/companion-service catalog.
6. `m11-security-architecture.md` — RBAC, secrets, runner/web split.
7. `m12-api-runner.md` — local API and privileged runner handoff.
8. `m13-web-ui-mvp.md` — dashboard UI over API.
9. `m14-domain-backup-ux.md` — domain/SSL, restore points, DR drills.
10. `m15-migration-assistant.md` — upgrade rehearsal and OpenUpgrade hooks.

## Post-v1 program

- `reddit-enhancements.md` — native Odoo neutralization, snapshot DR,
  scheduled off-site backup verification, PITR, object filestores,
  Kubernetes/GitOps runtimes, progressive delivery, and k3d/Tilt simulation.

## Verification standard

For each milestone:

- `uv run pytest -q`
- `uv run ruff check .`
- `uv run python -m build` when packaging/config changes occur
- For engine-touching work: live check against `experiments/odoo19-community-staging`
- Update `progress.md`
- Commit and push only after verification
