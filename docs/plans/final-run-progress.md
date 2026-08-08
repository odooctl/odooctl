# Final production-readiness run — live progress tracker

Purpose: session-resumable state for the roadmap execution
(`docs/plans/m16-roadmap-2026-07-production-readiness.md`). If a new session
starts, read this file first, then `git log --oneline -15`.

Last updated: 2026-07-19 ~18:05 UTC (update this line on every edit)

## Ground rules in effect

- User authorization: commit AND push to `origin/master` continuously (gh CLI authenticated as Rami-0). Full use of this machine (4 cores / 7.2 GB) for Docker integration testing.
- Never touch pre-existing containers (`community-sh-*`, `odoo19-community-staging-*`, `atabe-*`, `freellmapi*`, `homepage`, `dockge*`). The integration harness namespaces everything under compose project `odooctl-it-*`.
- Multi-agent execution: parallel sub-agents on disjoint file sets; cross-model review planned via Opus subagents + GPT through `codex` CLI (installed, v0.144.4).
- Outward-facing steps that still require explicit user confirmation: making the GitHub repo public, publishing to PyPI.

## Phase status

### Phase 0 — CI + honest signals: DONE (commit 65e3e82)
- `.github/workflows/ci.yml`: ruff + pytest 3.11/3.12/3.13 (cov floor 80) + build + wheel smoke. CI green on master.
- `runner --once` exits non-zero on failed op; `--fail-fast` for loop mode (C2 part 1). `RunnerWorker.last_run_ok`, `run_loop -> bool`.
- `tests/test_project_selector_matrix.py`: full command × selector regression matrix (C1 closed structurally via new `odooctl/cli_selector.py`; `typer.Context` threaded explicitly through main.py/env.py/branch.py).
- Test isolation: `ODOOCTL_*` env vars stripped + `XDG_CONFIG_HOME` registry isolation in tests/conftest.py.
- Learned: audit's "14 failed" did NOT reproduce; caused by operator machine state, now impossible.

### Phase 1 — Security hardening: DONE (commits 65e3e82, then Phase-1 commit, then L1/F3/F4 commit)
- C3/F1: all three `sh -lc` sinks removed (filestore list-argv execs; DB clone via new no-shell `run_pipe()` in utils/shell.py). Guard test blocks reappearance.
- Config-boundary validators (identifier + hostname rules) in config.py; Traefik Host()/route filenames re-validated point-of-use (F8).
- C4/F2: all literal `== "production"` guards → `cfg.is_protected()` (db_swap takes `is_protected_fn`); guard test scans the tree.
- F5/L2: PGPASSWORD via `docker compose exec -e` name-only injection (no argv); CommandError message redacted; operation-store/runner error persistence redacted.
- F15: healthchecks require 2xx, redirects rejected (`_NoRedirectHandler`).
- F11: `restore` and `rollback --mode full` require `--yes` or interactive confirm (`_confirm_destructive` in main.py).
- L1/F3/F4: `run_restore` = temp-DB restore → swap (verify-before-destroy); failed protected deploy auto-restores its pre-deploy backup when DB may have been mutated (`db_mutation_possible` flag), honest recovery notes in metadata.
- Trust model documented in docs/security.md ("odooctl.yml is operator-trusted" + enforced boundaries).
- click declared as direct dep (typer 0.27 dropped it); tests robust to click ≥8.2 stdout/stderr split + unrendered ClickExceptions (`_all_output` helper in test_env_cmd.py). Suite verified on BOTH pinned venv (.venv) and latest-deps venv (scratchpad fresh-venv).

### Phase 2 — Safety & correctness polish: DONE (pushed, CI green)
All four agents merged + integrated. Also: token mint/verify CLI now defaults
to signing with ODOOCTL_API_KEY (was ODOOCTL_RUNNER_KEY — a real key-mismatch
bug the docs-drift agent surfaced); tests/test_runner.py TEST_KEY lengthened to
32+ chars so the C2 regression test exercises op failure, not the key floor.
Dependabot immediately opened 5 GitHub-Actions bump PRs (#1-#5) — LEFT OPEN for
the user to review/merge (merge-without-review is outside my authorization).

### Phase 2 original agent scope (for reference)
- DONE by me: healthcheck strictness (2.6), restore/rollback confirmations (2.1), shell completion enabled in main.py.
- Agent "sanitization completeness" (2.2): DONE — payment_acquirer legacy table, web.base.url.freeze, OAuth secrets cleared, iap_account tokens, Odoo-19 `auth_passkey_key` DELETE (guarded), crons disabled in ALL profiles incl. minimal. docs/staging-clone.md updated. 896 passed.
- Agent "RBAC + token/audit hardening" (2.3+2.5): RUNNING — Action.CANCEL as write, project scoping on reads/cancel, TTL 300s default, nonce purge w/ timestamps, HMAC-keyed audit chain (ODOOCTL_AUDIT_KEY), API key ≥32 chars floor, events max_polls clamp.
- Agent "path containment" (2.4): RUNNING — resolve_backup_dir containment landed already; registry config-path containment, project-name identifier validation, migration report paths, import --output --allow-outside.
- Agent "docs drift + API polish" (2.7): RUNNING — token-mint key envvar truth, serve host guidance, runner-supported kinds table, README Status paragraph, index.html caching, DomainService.attach defaults fix.
- STILL TODO after agents: de-overload `-C` in `project add` (waits on containment agent; change `"--path", "-C"` → `"--path"` in commands/project.py:19), run full suite both venvs, commit+push, verify CI.

### Phase 3 — Integration harness: IN PROGRESS
- `tests/integration/` written: conftest.py (disposable compose stacks, unique `odooctl-it-*` project names, free-port binding, XDG-isolated registry, serial per-version param via ODOOCTL_IT_VERSIONS, teardown `down -v`), test_lifecycle.py (validate/doctor/status/backup --verify/clone-sanitize with real SQL assertions/restore --to/API-enqueue→runner-parity regression for C2/foreign-container guard).
- Images: odoo:19.0 local; odoo:18.0 + 17.0 pulling in background (task).
- Smoke run 1 findings (both fixed): (a) harness must NOT pass an explicit `-p` — odooctl derives the compose project from the directory name, so the harness now names the temp dir uniquely and derives the same project name; (b) Odoo 19 302-redirects `/web/login?db=...`, which the new strict healthcheck correctly rejects → switched the DEFAULT healthcheck path to `/web/health` (200, no-db route, Odoo 15+) in config.py, dr.py, examples, docs, harness. validate/doctor/status/backup --verify all PASSED against real Odoo 19.
- Smoke run 2 (with fixes, still `-k "not api"`) running in background.
- `import --allow-outside` flag wired in main.py (containment agent left it as a kwarg).
- TODO: api parity test after Phase 2 agents merge, full 17/18/19 matrix, docs/operations/integration-testing.md, optional nightly cron.

### Phase 4 — Product & UX: IN PROGRESS
- DONE: web UI papercuts agent (Migrate disabled for operator on protected env w/ tooltip, empty states w/ CLI hints, running-op pulse indicator, title+inline-SVG favicon; 48 web tests green). mkdocs.yml + .github/workflows/docs.yml (GitHub Pages, README→index.md at build) scaffolded. docs/operations/integration-testing.md written.
- RUNNING: README full-rewrite agent (verifies every command against --help before documenting).
- TODO: screenshots (needs a running UI; placeholder comments in README), first-run measurement, error-message polish (deferred — good state already).

### Phase 5 — Launch prep + cross-model review: DONE (two review rounds fixed)
- Round 1 (Opus): 13 remediations verified + 9 findings. ALL FIXED (commit "post-rescan security hardening"): H1 API project-scope, H2 audit HWM truncation-detection + runner unkeyed warning, H3 nonce atomic flock+expiry, M1 temp-db collision, M2 filestore_path validation, M4 unconditional key floor, L1 testclient host. M3 accepted low → later fixed in round 2. L2 mitigated.
- Round 2 (GPT via codex CLI — stdin-fix made it work): confirmed round-1 fixes hold, 7 more findings. Fixed with tests (commit "second-round re-scan findings"): #3 crash/failure-safe DB swap (rename-aside via new database_exists on real adapters; both models flagged it), #6 registry containment in API+runner (registry.context_from_registered), #4 CommandError merged-env redaction, #1 filestore tar member validation. #2/#5/#7 deferred with rationale under operator-trust model → experiments/2026-07-19-post-hardening-rescan/codex-findings-triage.md.
- 983 unit tests green on both venvs; CI green all Python versions. Integration re-run on Odoo 19 in progress to exercise real-Postgres swap path.
- Findings + resolution recorded in experiments/2026-07-19-post-hardening-rescan/opus-rescan-findings.md.
- Launch metadata done earlier: release.yml (PyPI trusted publishing), dependabot, templates, CODEOWNERS, 0.2.0, mkdocs.
- STILL REQUIRES USER (outward-facing): make repo public; PyPI trusted-publisher + tag v0.2.0 to publish. Dependabot PRs #1-#5 for user to merge.
- NOTE: earlier this session hit a ~2h window (session limit + repo write-gate) that blocked the fixes; they were applied once write access returned.

### Phase 5 original launch-prep detail
- DONE: .github/workflows/release.yml (tag → test → build → PyPI trusted publishing → GH release), dependabot.yml, ISSUE_TEMPLATE/{bug,feature}.yml, PULL_REQUEST_TEMPLATE.md, CODEOWNERS. Version bumped to 0.2.0 (pyproject + __init__ + changelog heading). SECURITY.md placeholder email removed → GitHub private advisory is the sole channel.
- RUNNING: cross-model adversarial re-scan — GPT via `codex exec -s read-only` (scratchpad/codex-review.md) AND an Opus subagent — both verifying the 13 remediations hold + hunting new bugs.
- TODO after reviews return: triage findings, fix any real HIGH/MED, re-run suite, commit. THEN (needs user confirmation, do not do autonomously): flip repo public, set up PyPI trusted-publisher + tag v0.2.0 to publish.
- PyPI name `odooctl` verified free 2026-07-19; repo currently private.
- NOTE: dependabot opened PRs #1-#5 (actions bumps) — left for user to merge (merge-without-review blocked for me).

## Key facts for a fresh session

- Suite: 896+ passed, `-m 'not integration'` default; integration tests via `pytest -m integration tests/integration`.
- Two venvs matter: `.venv` (pinned, older typer/click) and `/tmp/claude-1000/-home-dev-odooctl/253d1d92-8629-47d1-9e24-53c2b246c80b/scratchpad/fresh-venv` (latest deps — recreate with `uv venv` + `uv pip install -e '.[dev,api]'` if gone). CI runs latest deps, so always check both.
- CI: `.github/workflows/ci.yml`, watch with `gh run watch $(gh run list --workflow=CI --limit 1 --json databaseId -q '.[0].databaseId')`.
- Memory file exists: ~/.claude/projects/-home-dev-odooctl/memory/odooctl-prod-roadmap.md.
