# odooctl Control-Plane Progress

Primary plan index: `docs/plans/README.md`

> Compacted 2026-07-21: the original chronological worklog (hourly kanban
> check-ins, per-review entries, May 2026 milestone run) was condensed into the
> summaries below. Full detail lives in git history of this folder up to that
> date (folder is now untracked; see README).

## Operating rules

- Plans are numbered `m##`; the highest number is the newest plan.
- Before each run: inspect git status, read the active plan, inspect current code.
- After each run: update this file with what changed, tests, result, and next step.
- Do not mark a task complete unless verified.
- Engine-touching work requires real Odoo fixture evidence
  (`uv run pytest -q`, `uv run ruff check .`, live check on a real stack).

## Milestones (May 2026 run) — all DONE

- **M6 — Service layer**: `odooctl/services/` package, structured results, CLI as thin wrappers.
- **M7 — Operation engine**: operation models/store/events/audit/locks, `odooctl ops`, live-fixture verified.
- **M8 — Import/takeover + setup wizard**: compose/Odoo detector, import preview, no-redeploy config generation, `odooctl setup`.
- **M9 — Environment/branch model**: tiers + protected production, branch drift, promote flow, ephemeral envs, rollback-on-failed-promote.
- **M10 — Onboarding catalog**: manifest schema, bundled stack templates, addon source model, wizard integration.
- **M11 — Security architecture**: principals/RBAC matrix, secret store + rotation, capability tokens, web/runner privilege split.
- **M12 — API and runner**: FastAPI service, SPA serving, durable queue handoff, privileged runner, event streaming, auth/RBAC tests.
- **M13 — Web UI MVP**: projects/environment/doctor/backups/operations views, clone/promote buttons, streaming logs, UI-is-API-client-only.
- **M14 — Domain/SSL and backup UX**: domain attach/verify/detach, Traefik adapter + ACME, restore-point browser, restore-to-staging, DR drill, encrypted off-site backups.
- **M15 — Migration assistant**: migration matrix, module readiness scan, upgrade rehearsal, OpenUpgrade hooks, report output/API/UI.

Each milestone passed a review gate (M11/M12/M14 additionally passed security
review with remediations: protected-env RBAC, production-source restore
sanitization). Verified against the real Odoo 19 fixture where engine-touching.

## m16 — Production-readiness roadmap (2026-07)

Plan: `m16-roadmap-2026-07-production-readiness.md`; live tracker:
`final-run-progress.md` (phase status, ground rules, resumption notes).
Highlights to date: CI green with cov floor, security hardening (shell-sink
removal, config-boundary validators, PGPASSWORD hygiene, verify-before-destroy
restore), 0.2.0 published to PyPI with a clean-room install test suite passing.

## m17 — Feedback enhancement plan (2026-07-21, locked)

Plan: `m17-feedback-enhancement-plan.md`. Sequencing: §1 neutralize → §3 sync →
§4 overlay → §6 identity/RBAC (all 1.0); managed Postgres seam, `dev pull`,
restic, OIDC post-1.0.

### §1 — Neutralize-first sanitization ✓ DONE (2026-07-21)

- [x] `odooctl/odoo/neutralize.py`: run `odoo-bin neutralize` in the compose
      service for Odoo >= 16 (`sanitization.use_odoo_neutralize`, default on;
      password via PGPASSWORD env, never argv).
- [x] `sanitize_database` runs neutralize first, keeps the odooctl SQL as the
      supplement, validates configured SQL files up front, and returns the list
      of mechanisms that ran.
- [x] Clone manifest (`.odooctl/clones/<target>-*.json`, `CloneManifest` model)
      records `sanitization_mechanisms`; `CloneResult` carries them and the
      clone operation log emits them.
- [x] `restore_to_env` mirrors the neutralize-first contract for
      protected-source restores (guarded on compose file presence).
- [x] Verification: full pytest + ruff clean (one pre-existing
      `test_import_detect` failure from the untracked `experiments/` fixture).
      Live evidence on the Odoo 19 stack in `~/odooctl-demo`: clone
      production→staging set `database.is_neutralized=true`, supplement SQL
      froze/rewrote `web.base.url`, 0 active crons/mail servers in staging,
      production untouched; manifest records `["odoo-neutralize", "odooctl-sql"]`.

### §3 — `sync` + `auto_deploy` wiring ✓ DONE (2026-07-21)

Commits: `2fa72bf` (feature), `543550d` (docs).

- [x] `odooctl/services/sync.py`: `check_sync`/`run_sync` — git fetch, compare
      last deployed commit (deployment metadata) vs remote tip
      (`<branch>@{upstream}` with `origin/<branch>` fallback); statuses
      up_to_date / behind / disabled / never_deployed / diverged / no_remote /
      fetch_failed / unknown. Deploys via the existing `run_deploy` pipeline
      only when behind and `auto_deploy: true` (or `--force`).
- [x] `odooctl sync <env>` CLI (`--force`, `--json`); deploys wrapped in the
      operation engine with `actor="sync"`, `trigger: sync` params; attention
      states (diverged/no_remote/fetch_failed/unknown) exit 1 so systemd
      timers surface them. No operation record for no-op polls.
- [x] `odooctl schedule sync --env <env>`: sync allowed alongside
      backup/doctor; per-command default interval (sync → `*:0/5` systemd,
      `*/5 * * * *` cron; others stay daily).
- [x] Docs: `docs/git-sync.md` (pull-based sync as PRIMARY CI/CD model:
      read-only deploy key, no inbound secrets, NAT-friendly) in mkdocs nav;
      deployment.md + configuration.md cross-links; generated GitHub Actions
      workflow switched to `runs-on: [self-hosted]` with a header comment
      (GH-hosted runners can't reach a VPS Docker daemon) and marked
      secondary. CHANGELOG updated.
- [x] Verification: 15 new tests in `tests/test_sync.py`; full pytest 1062
      passed (same lone pre-existing `test_import_detect` failure), ruff
      clean. Live evidence on `~/odooctl-demo` (now a git repo with a local
      bare remote `~/odooctl-demo-remote.git`): full ladder exercised —
      never_deployed → baseline deploy → up_to_date → simulated developer
      push → disabled (auto_deploy false) → enabled `auto_deploy: true` →
      `sync staging` auto-deployed 2 commits (3ed8218) → up_to_date;
      operation record shows kind=deploy, actor=sync, status=succeeded.
- Deferred per plan: webhook trigger on `odooctl serve` is post-1.0 (polling
  stays source of truth). Note: rendered systemd unit needs the operator to
  add `EnvironmentFile=` for `ODOO_DB_PASSWORD` etc. (documented; same as
  scheduled backups — candidate improvement pre-1.0).

Addendum (2026-07-21, commit `e7e1185` after merging origin/master `d9f5522`):
live edge-case sweep on `~/odooctl-demo` found and fixed two silent-failure
modes: (a) failed deploy + unmoved remote read as `up_to_date`/exit 0 forever
→ new `deploy_failed` attention status (new push auto-heals via behind);
(b) dirty worktree crashed into the deploy pipeline per poll, minting failed
operation records every 5 min → new `dirty_worktree` attention status,
checked pre-pipeline, zero op records. Also live-verified: diverged (rewound
and rewritten remote), fetch_failed (missing remote), concurrency race (two
simultaneous syncs → exactly one deploy; loser fails at fetch, next poll
recovers). Known limitations, documented: deploy reads `odooctl.yml`
pre-pull so config commits lag one deploy (doc note in git-sync.md; real fix
is a §4-adjacent design question); `--json` output shares stdout with deploy
logs when a deploy runs (parse last JSON block). Suite: 1067 passed, 0
failed (master's vendored fixture cured the old `test_import_detect`
failure), ruff clean. 19 sync tests.

### §4 — machine-local config overlay ✓ DONE (2026-07-21)

- [x] `config.py`: `local_overlay_path()` (`odooctl.yml` → `odooctl.local.yml`;
      `custom.yml` → `custom.local.yml`; a `*.local.yml` config has no overlay,
      preventing recursion) + `deep_merge()` (mappings merge key-by-key;
      scalars/lists/null replace wholesale). `load_config` merges an existing
      overlay before validation; validation errors name both files. Precedence:
      env vars (`*_env` runtime indirections) > overlay > main.
- [x] `ProjectContext.overlay_path` exposes the merged overlay (None when
      absent); all call sites get the merge for free via `load_config`.
      Config-writing commands (`env add`, domain attach, setup) operate on the
      raw main file, so overlay values never leak into `odooctl.yml`.
- [x] `odooctl validate` prints "Machine-local overlay merged: …" and warns
      when the overlay is not gitignored (`git check-ignore` best-effort;
      silent outside a repo). `odooctl init` and `odooctl setup` append
      `odooctl.local.yml` to `.gitignore` (create if missing, idempotent).
- [x] Docs: configuration.md "Machine-local overlay" section (precedence,
      merge semantics, gitignore rule); git-sync.md note (overlay survives
      pulls; unignored overlay blocks sync with dirty_worktree). CHANGELOG.
- [x] Verification: 23 new tests in `tests/test_config_overlay.py`; full suite
      1090 passed, ruff clean. Live on `~/odooctl-demo`: overlay with
      `project.name`+port overrides merged (validate showed `demo-local` +
      overlay line + not-gitignored warning); with staging behind, sync exited
      1 `dirty_worktree` on the unignored overlay (no deploy, pre-pipeline);
      after gitignoring, worktree clean → `sync staging` auto-deployed 2
      commits (e4e5c26) with the overlay in place → up_to_date; overlay file
      untouched by the pull.
- Scope note: the git-sync "config changes lag one deploy" limitation is
  unchanged — the overlay is machine-local and never pulled, so it is
  orthogonal to that; no generic `ODOOCTL_*` config-override env mechanism
  was added (the plan's "env vars" precedence refers to the existing `*_env`
  runtime indirections, which always win by construction).

### §6 — identity (user accounts, sessions, ownership, attribution) + RBAC-on-all-mutations ✓ DONE (2026-07-21)

Sequencing items 4 and 5. Commits: identity-core commit + docs/UI commit.

- [x] `security/users.py`: server-level user store next to the registry
      (`~/.config/odooctl/users.json`, fcntl-locked, atomic 0600 writes);
      salted scrypt password hashing with scheme-prefixed format (argon2 can
      be added without invalidating hashes); `provider`/`provider_subject`
      fields reserved for post-1.0 OIDC; login-oracle hardening (dummy-verify
      on unknown email, identical 401s).
- [x] `security/sessions.py`: revocable browser sessions — 256-bit sid in an
      HttpOnly SameSite=Lax cookie, server stores SHA-256 digests only,
      12 h TTL, create-time pruning, revoke/revoke_user(keep_sid).
- [x] API: `POST /auth/login` (per-email backoff 5/15 min) / `logout` /
      `me` / `password` (self-service, revokes other sessions); `/users`
      CRUD (admin+, role ceiling, outrank guards, no self-disable/delete,
      disable revokes live sessions). Session principals resolve roles from
      the user store per request (role changes/disable apply immediately);
      bearer tokens unchanged for CLI/CI. `create_app(auth_dir=...)` for
      tests; default is the registry directory.
- [x] RBAC: new `Action.USERS` (admin+). Phase-5 gate: introspection test
      walks every mutating API route and asserts an authentication
      dependency; role×mutation checks for users/ownership routes.
- [x] Ownership: `owner` on registry projects (`project add --owner`,
      `project owner`, `PATCH /projects/{p}/owner`) + optional `owner` on
      environment config; both in API responses and UI (project header,
      env cards).
- [x] Attribution: CLI `actor="cli"` literals (11 sites) → `local:<os-user>`
      via `principals.local_actor()` (`ODOOCTL_ACTOR` override); API path
      records the authenticated principal (user email for sessions).
- [x] CLI: `odooctl user add/list/role/passwd/disable/enable/remove`
      (passwords via prompt/--stdin/--password-env, never argv; passwd/
      disable/remove revoke sessions).
- [x] SPA: email/password login form (token paste kept as fallback),
      session probe on boot, logout via `/auth/logout`, Access page user
      management (admin), owner display.
- [x] Docs: new `users-and-access.md` (nav under Security); api.md auth
      section + auth/user routes; web-ui.md login flow; rbac.md stale
      "not yet wired" note replaced with the enforced-everywhere statement;
      configuration.md env `owner`; CHANGELOG.
- [x] Verification: 48 new tests (`test_users.py`, `test_api_identity.py`);
      full suite 1142 passed, ruff clean. Live on `~/.config/odooctl` +
      `~/odooctl-demo` via `odooctl serve` on :8799 with curl: user add →
      3×bad-password 401s → login sets HttpOnly cookie → `/auth/me` 200
      (401 without cookie) → `/users` listing → `PATCH /projects/demo/owner`
      persisted to registry → enqueued backup op record shows
      `actor: testadmin@example.com` → logout revoked (me → 401) → SPA
      serves. Test user/owner cleaned up afterwards.
- Notes: role set stays owner/admin/operator/viewer (plan's "developer"
  read as loose wording; matrix can grow a tier without breakage). CSRF
  posture: SameSite=Lax HttpOnly cookie + JSON-only bodies + no CORS.
  Approval gates, team UI views, and OIDC remain post-1.0 per plan.

### m17 multiversion verification — Odoo 19 / 18 / 17 ✓ PASS (2026-07-21)

Full local experiments before moving on, at commit `4df205f`. Fixtures and
full evidence table: `experiments/2026-07-21-m17-multiversion-suite/NOTES.md`
(odoo18 + odoo17 compose stacks built for this run, one at a time due to RAM;
Odoo 19 = existing `~/odooctl-demo`).

- §1 neutralize-first sanitize PASS on **all three versions**: clone manifest
  records `["odoo-neutralize", "odooctl-sql"]`, staging gets
  `database.is_neutralized=true`, 0 active crons/mail servers, seeded secret
  param scrubbed, `web.base.url` rewritten; production markers untouched.
- §3 sync ladder PASS on 17 (never_deployed → baseline deploy → up_to_date →
  dev push → auto-deployed → up_to_date; dirty_worktree exits 1 only when a
  deploy would run, then deploys once clean) — matching the 19 runs.
- §4 overlay merge line PASS on 18 (and prior 19 evidence).
- §6 identity e2e PASS on 18 with real serve+runner: viewer session 403 on
  enqueue, admin session enqueue → runner executed backup against the live
  Odoo 18 stack, op + audit records `actor=qa@example.com`, project owner
  set via `--owner` and queryable via API; CLI ops on all three versions
  attributed `local:dev`.

Findings for pre-1.0 (detailed in suite NOTES.md):
1. Post-clone healthcheck failure loses the clone manifest (sanitize ran but
   its mechanism record is never written; op marked failed). Write manifest
   before the final healthcheck, or persist mechanisms on the op record.
2. `deploy` pre-backup demands the host filestore dir even when
   `filestore_volume` is set (`FileNotFoundError` on first staging deploy);
   auto-create or skip when the volume backs the filestore.
3. Note: a second runner with a different `ODOOCTL_API_KEY` fails claimed ops
   with `signature mismatch` — correct behavior, but a doctor check for
   key mismatch across serve/runner could save operator confusion.

Next: sequencing item 6 — §7.1 managed Postgres seam + config shape (design
at 1.0, implementation post-1.0); remaining pre-1.0 hardening per m16, plus
the two multiversion findings above.
