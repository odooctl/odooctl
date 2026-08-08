# Production Readiness Roadmap — from feature-complete alpha to 1.0 GA

Date: 2026-07-19
Status: PROPOSED
Supersedes: `experiments/2026-05-31-kanban-scan-suite/report-4-opus-next-steps-synthesis.md` (as the working backlog; the report remains the audit record)

## 0. Ground truth as of 2026-07-19 (verified on this machine, HEAD `ee52ff4`)

Before planning, the audit record was re-verified against the current tree:

| Claim from the May 31 audits | Verified status today |
| --- | --- |
| C1: `--project`/`--project-dir` not propagated; 14 tests failing | **Does not reproduce.** Full suite: **723 passed, 0 failed**. Functional check: `--project-dir ../proj validate` resolves the sibling config correctly under `CliRunner`. Likely environment-dependent in the audit run (exported `ODOOCTL_*` vars / stale registry). The regression matrix is still worth adding (P0.3 below) because `main.py:55` still uses `click.get_current_context()` implicitly. |
| C2: `runner --once` exits 0 on failed operations | **Confirmed still present.** `RunnerWorker.run_loop(once=True)` (`odooctl/runner/worker.py:221-233`) returns `None` regardless of outcome; `commands/runner.py` never inspects it. |
| C3/F1: `sh -lc` command-injection sinks | **Confirmed still present**: `odooctl/adapters/filestore.py:99`, `filestore.py:113`, `odooctl/adapters/db.py:124`. |
| C4/F2: literal `== "production"` guards instead of `is_protected()` | **Confirmed still present** at `odooctl/odoo/db_swap.py:52`, `commands/env.py:124`, `commands/env.py:286`, `services/clone.py:41`, `services/deploy.py:72`, `services/deploy.py:103` (config.py:173/237 and branch.py:58 are definitional, not guards). |
| Suite size / maturity | 723 tests (~11.7k LOC of tests vs ~9.9k LOC of source), no stubs/TODOs anywhere. Feature-complete alpha. |

Launch-infrastructure gaps found during verification:

- **No CI**: `.github/workflows/` contains only the example deploy workflow — nothing runs pytest/ruff/build on push or PR.
- **Repo is private** (`github.com/Rami-0/odooctl` returns 404 unauthenticated) — the "open source" part has not happened yet.
- **PyPI name `odooctl` is unclaimed** (404 on pypi.org) — claim it early.
- **`SECURITY.md` contact is still the `<MAINTAINER_SECURITY_EMAIL>` placeholder.**
- **README is stale**: says "Status: M5" while the tree is at M15 (API, runner, web UI, domains, DR, migration all exist but are not mentioned).

## 1. Definition of "100% production ready"

1.0 GA is declared when all of the following hold:

1. **Green, enforced CI** — lint + full unit suite on Python 3.11/3.12/3.13, on every PR, with a coverage floor.
2. **Zero open HIGH/MEDIUM audit findings** — every P1/P2 item from reports 1–4 fixed or explicitly risk-accepted in `SECURITY.md`.
3. **Proven against real Odoo** — an opt-in Docker integration suite passes the full operator lifecycle on Odoo 17, 18, and 19, on this machine and in CI (nightly).
4. **CLI/API/runner parity** — every operation the UI can enqueue succeeds through the runner exactly as it does through the CLI, and the runner reports failure honestly.
5. **A 10-minute first-run experience** — a newcomer or an existing self-hoster gets from `pipx install odooctl` to a working, sanitized staging clone in under 10 minutes following only the docs.
6. **Published and governed** — public repo, PyPI releases via trusted publishing, semver + changelog discipline, issue templates, filled-in security policy.

## 2. Phases

Ordering rationale: make quality *visible* first (CI), then make the tool *safe* (security), then make claims *provable* (integration), then make it *lovable* (UX), then *launch*. Phases 3 and 4 can run in parallel.

---

### Phase 0 — CI + honest signals (small, do first)

Everything later depends on a trustworthy red/green signal.

- **0.1 CI workflow** (`.github/workflows/ci.yml`): ruff check, pytest (3.11/3.12/3.13 matrix), `python -m build`, wheel smoke-install (`odooctl --version`). Add `pytest-cov` with an initial floor (~85%) and ratchet up.
- **0.2 Fix C2 — runner exit code**: `run_loop(once=True)` returns whether the processed op succeeded; `odooctl runner --once` exits non-zero on a failed op. Add a `--fail-fast` option for loop mode. Unit tests for both.
- **0.3 `--project`/`--project-dir` regression matrix**: parametrized test crossing every destructive command × selector flags × (cwd has config / cwd empty / registry set). Refactor `_context_config` (`main.py:55`) to thread `typer.Context` explicitly instead of `click.get_current_context()` — this closes C1 structurally, whatever caused the audit-run failures.
- **0.4 Root-cause the API/runner enqueued-backup failure** from report 3 (failed on both Odoo 18 and 19 while the CLI path passed). Reproduce with a disposable stack on this machine; fix; add to the integration suite (Phase 3).
- **0.5 Environment hygiene for tests**: fixtures that isolate `HOME`, `ODOOCTL_*` env vars, and the global registry, so the suite result cannot depend on the operator's shell (the most likely cause of the audit-run RED).

Exit criteria: CI required on PRs; runner failure = non-zero exit; regression matrix green.

---

### Phase 1 — Security hardening (P1 findings; all confirmed live)

- **1.1 Remove the `sh -lc` sinks** (C3/F1). Config-boundary validators first (charset/length for env names, db names, volume names, hostnames — Pydantic validators in `config.py`), then replace the three shell-string sinks with list-argv sequences or Python-side archive handling:
  - `adapters/filestore.py:99` (`rm -rf` composition), `filestore.py:113` (`cp -a` composition), `adapters/db.py:124` (dump/restore script).
- **1.2 `is_protected()` everywhere** (C4/F2). Replace the six literal `"production"` guards listed in §0; add a test asserting no `== "production"` guard exists outside `config.py` (grep-based guard test, same pattern as `runner_contract`).
- **1.3 Verify-before-destroy + rollback** (L1/F3/F4): `run_restore`/`swap_temp_database` restore into a temp DB, verify, then swap (mirror `restore_to_env`); failed protected-env deploy triggers restore of its own pre-deploy backup; `ALLOW_CONNECTIONS false` on retired DBs.
- **1.4 Secrets off argv and out of errors** (L2/F5): DB password via env/`.pgpass` only; redact `CommandError` args before persist/stream; single shared redactor (also resolves F17).
- **1.5 Traefik `Host()` rule built from validated hostname** (F8) — falls out of 1.1's validators.
- **1.6 Decide and document the `odooctl.yml` trust model** — the audits' biggest severity multiplier. Proposed stance for v1: *the config file is trusted; anyone who can write it is an operator.* Document in `docs/security.md` + threat model section; validators from 1.1 still apply as defense in depth.

Exit criteria: reports 1 & 2 HIGH findings all closed with tests; a re-scan (fresh adversarial review) finds no new HIGHs.

---

### Phase 2 — Safety & correctness polish (P2/P3 findings)

- **2.1 Typed confirmation / `--yes` on `restore` and `rollback --mode full`** (F11); `--dry-run` on all destructive commands (F23).
- **2.2 Sanitization completeness** (F7): freeze `web.base.url`, cover `payment_acquirer` legacy table, OAuth providers, SMS gateways, and **Odoo 19 `auth_passkey` credentials**; keep crons disabled under `minimal`.
- **2.3 RBAC tightening** (C5/F6): `Action.CANCEL` as a write action; project scoping on reads/cancels (org_id enforcement stays post-v1, documented).
- **2.4 Path containment** (F10/F19/F20): registry paths, backup ids, migration report paths, `import --output` resolved and contained under expected roots.
- **2.5 Token/audit hardening** (F12/F13/F24): capability TTL → 300s + nonce purge; HMAC-keyed audit chain head; key-strength floor for `ODOOCTL_API_KEY`/`ODOOCTL_RUNNER_KEY`.
- **2.6 Healthcheck strictness** (F15): require 2xx, don't follow redirects.
- **2.7 Docs drift sweep** (F21/F22): `token mint` env-var docs, `serve` host guidance, runner-supported operation kinds table, plus the maintainer follow-ups already logged in `docs/plans/progress.md` (events `max_polls` clamp, `index.html` caching, `DomainService.attach` defaults, `temp_db != target_db` guard).

Exit criteria: zero open MEDIUMs; progress.md follow-up list empty or explicitly deferred with rationale.

---

### Phase 3 — Real-Odoo integration harness (uses this machine's resources)

The single biggest credibility gap: all 723 tests are unit tests with fakes. Report 3 proved the value of dynamic testing (it found C2).

- **3.1 Integration suite** under `tests/integration/` (marker `integration`+`docker`, excluded by default, run with `pytest -m integration`): spins up disposable Compose stacks from the official `odoo:17`/`odoo:18`/`odoo:19` + `postgres:16` images into throwaway dirs/volumes; tears down completely.
- **3.2 Lifecycle scenarios per version**: `init → validate → doctor → deploy → backup --verify → clone --sanitize (assert sanitization SQL effects) → update-modules → restore --to → promote → rollback (code & full) → env destroy`; plus `import` against a pre-built "foreign" compose project; plus **API/runner parity**: enqueue each supported kind via the API, run `runner --once`, assert success *and* exit codes (locks in 0.2/0.4).
- **3.3 Resource budget for this host** (4 cores / 7.2 GB RAM / 334 GB free): one Odoo stack at a time, sequential version matrix (~15–25 min total); prune volumes/images between versions. Existing stopped containers on the box (`community-sh-*`, `odoo19-community-staging-*`) must not be touched — the harness uses unique compose project names and asserts it only ever removes resources it created.
- **3.4 Nightly job on this machine** (cron or CI self-hosted fallback): full integration matrix + result file committed to `docs/operations/integration-status.md` style log, so "works on 17/18/19" is a continuously renewed claim, not a one-time experiment.
- **3.5 Version-support policy doc**: which Odoo versions are supported, how new majors get added (rerun matrix + sanitization review — e.g. the `auth_passkey` lesson from Odoo 19).

Exit criteria: matrix green on 17/18/19 twice in a row; enqueued-backup bug (0.4) covered by a regression scenario.

---

### Phase 4 — Product & UX polish (parallel with Phase 3)

"An amazing environment to manage Odoo" — the code is there; the experience needs finishing.

- **4.1 README rewrite**: pitch the real product (Odoo.sh-style control plane for self-hosters), feature overview incl. web UI/API/import/migration, screenshots of the SPA, quickstart for both personas (newcomer via `setup`, existing self-hoster via `import`), comparison table (odoo.sh / doodba / plain compose). The current "Status: M5" text undersells the tree by ten milestones.
- **4.2 Docs site**: MkDocs Material from the existing `docs/` (they're extensive already), deployed via GitHub Pages workflow. Task-oriented landing: "Adopt an existing deployment in 10 minutes", "Create staging you can trust", "Upgrade rehearsal".
- **4.3 CLI polish**: shell completion enabled (`add_completion=False` today), `--json` on remaining read commands, consistent Rich tables/error rendering, de-overload `-p` (F23), actionable error messages for the top 10 failure modes (missing docker, missing pg_dump → suggest docker execution mode, bad compose path…).
- **4.4 Web UI pass**: keep the no-build vanilla SPA (it's a feature for auditability); polish: empty states, operation progress UX, the M15 follow-up (hide Migrate action for operators on protected envs), mobile-usable layout, favicon/name. Screenshot set for README/docs.
- **4.5 First-run measurement**: script the two personas' paths end-to-end on a clean VM/container; fix every papercut until each is < 10 min. This becomes a doc *and* an integration test.

Exit criteria: a stranger can succeed from README alone; screenshots/docs published.

---

### Phase 5 — Open-source launch & release engineering

- **5.1 Governance files**: fill `SECURITY.md` contact, add issue/PR templates, `CODEOWNERS`, enable Dependabot (uv lock + Actions), branch protection requiring CI.
- **5.2 Release pipeline**: tag-driven GitHub Actions release — build, integration smoke, publish to PyPI via **trusted publishing** (no long-lived token), GitHub Release with changelog section. Claim the `odooctl` PyPI name with the first alpha immediately (it is free today; names get squatted).
- **5.3 Versioning policy**: 0.2.0 = post-hardening alpha (end of Phase 2), 0.9.x = public beta at launch (end of Phase 4), 1.0.0 = GA when §1 criteria all hold. Semver + `CHANGELOG.md` discipline; config-schema compatibility policy documented.
- **5.4 Go public**: make the repo public after Phase 1 (security fixes) lands — publishing known command-injection sinks before fixing them is the wrong order. Prepare launch posts (Odoo community forum, r/odoo, Hacker News "Show HN") for the beta.
  - Immediately after going public, enable branch protection on `master` (blocked on private repos without GitHub Pro — attempted 2026-07-19, HTTP 403): require PRs + CI checks (`lint`, `build`, `test (3.11/3.12/3.13)`), `enforce_admins`, no force pushes/deletions, required conversation resolution.
- **5.5 Adoption funnel**: "good first issue" seeding, roadmap.md public (post-v1: SSH/multi-host runners, Nginx/Caddy adapters, PITR via WAL archiving, org-scoped multi-tenancy — currently modeled but unenforced).

Exit criteria: v0.9.0 on PyPI from a public repo through the automated pipeline.

---

### Phase 6 — GA gate

Run the §1 checklist as a formal review: fresh adversarial security re-scan of the final tree, integration matrix green, first-run timing re-measured, docs audit. Fix or explicitly waive every finding, then tag 1.0.0.

## 3. Sequencing & effort snapshot

| Phase | Depends on | Rough size |
| --- | --- | --- |
| 0 CI + honest signals | — | 1–2 days |
| 1 Security hardening | 0 | 3–5 days |
| 2 Safety polish | 1 | 3–4 days |
| 3 Integration harness | 0 (0.4 feeds it) | 4–6 days, mostly machine time |
| 4 Product & UX | 0 | 4–6 days, parallel with 3 |
| 5 Launch | 1 (public), 2–4 (beta) | 2–3 days |
| 6 GA gate | all | 1–2 days |

Critical path: 0 → 1 → 2 → 5-beta → 6; Phases 3/4 overlap. Order of first three work items when execution starts: **0.1 CI**, **0.2 runner exit code**, **1.1 shell-sink removal**.
