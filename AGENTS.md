# AGENTS.md

Operating guide for AI coding agents. Humans: read [CONTRIBUTING.md](CONTRIBUTING.md).

## Context

`odooctl` is a CLI-first control plane for self-hosted Odoo. It performs **real,
destructive operations on live databases and filestores**. A change that weakens
a guard, confirmation, or redaction path can destroy a user's production data.
Default to safe, reversible, fail-closed behavior; if a request reduces safety,
say so in a sentence, then do what the user decided.

**Never run mutating `odooctl` subcommands** (`deploy`, `restore`, `promote`,
`clone`, `rollback`, `pitr`, `dr`, `migrate`, `update-modules`, `ops *`) against
real infrastructure — not even to verify. Verify through tests. Read-only
(`--help`, `--version`, `status`, `validate`, `doctor`) is fine.

## Checks

```bash
uv sync --frozen --extra dev --extra api
uv run --frozen pytest -q --cov=odooctl --cov-fail-under=80
uv run --frozen ruff check odooctl tests
uv run --frozen python -m build
```

- **Always keep `--frozen`** — without it `uv run` may rewrite `uv.lock`, so a
  green local run stops proving the locked environment is green.
- Never `uv pip install -e '.[dev]'` (re-resolves to lower bounds CI never tests).
- Dependency change = edit `pyproject.toml` → `uv lock` → commit `uv.lock` together.
- Ruff is pinned `>=0.15.14,<0.17` on purpose. Don't widen it as a side effect.

## Layout

| Path | Responsibility |
| --- | --- |
| `odooctl/main.py` | Typer app, registers command groups |
| `odooctl/commands/` | CLI only — args, confirmations, output. Keep thin |
| `odooctl/services/` | Orchestration; the actual workflows |
| `odooctl/adapters/` | Infra drivers (compose, k8s, postgres, s3, traefik) |
| `odooctl/odoo/` | Odoo logic (sanitize, neutralize, module update, db swap) |
| `odooctl/operations/` | Operation engine, locks, audit, events |
| `odooctl/security/` | RBAC, principals, tokens, secrets, redaction |
| `odooctl/api/`, `web/` | FastAPI service and bundled UI |
| `docs/`, `docs/plans/` | MkDocs site; design plans |

Generated, do not edit: `site/`, `dist/`, `odooctl/web/dist/`, `docs/index.md`
(built from `README.md`; landing page is `landing/index.html`).

## Rules

- Commands are thin — see `commands/clone.py`: build `ServiceContext`, wrap in
  `run_operation(...)`, return. Logic lives in `services/`.
- Adapters carry no CLI/UX concerns (no `typer.echo`, prompts, or Rich).
- Every mutating operation runs inside `run_operation(...)` for its operation
  record, environment lock, audit entries, and events.
- **Never `os.getcwd()`** — the working directory is untrusted. Resolve through
  `ProjectContext.resolve_path()`.
- **Never log raw secrets** — use the redaction helpers; respect
  `redaction.min_secret_length` / `redaction.ignore_values`.
- Sanitization SQL stays idempotent and guarded (must survive missing tables).
- Protected-environment guards (`config.is_protected`) may be strengthened,
  never loosened without an explicit request.
- Python 3.11+, `from __future__ import annotations`, 100-char lines.

## Tests

- Unit tests run without Docker, network, or ambient env vars — `conftest.py`
  deliberately scrubs operator credentials.
- `integration` / `docker` markers are excluded by default and from the PR gate.
- **Assertions on CLI text must use `strip_ansi` from `tests/conftest.py`.**
  Typer force-enables Rich colour under `GITHUB_ACTIONS`, and Rich splits option
  tokens while styling them, so `--yes` isn't a literal substring of the output.
  Skipping this passes locally and fails only on CI. After CLI-output changes:
  `GITHUB_ACTIONS=true uv run --frozen pytest -q`.
- Coverage gate is 80%. Don't lower it to make a change pass.

## Git

**Branches** — never commit to `master`. Name `<type>/<kebab-slug>`, ≤4 words:
`feat/`, `fix/`, `docs/`, `ci/`, `refactor/`, `test/`, `chore/`, `release/`,
`agent/` (agent-authored work with no obvious owner type). One branch = one PR.

**Commit cadence — commit as you go, don't batch:**

- Finish a feat or fix → commit it.
- Work split into tasks → commit after **each** task, not at the end.
- A commit must be self-contained and green on its own. If it isn't, the task
  boundary was wrong — adjust the split, not the rule.
- Never `git push`, open a PR, merge, or `--force` without being asked.
- keep the remote up to date, when ever commiting, push to the branch.

**Messages** — Conventional Commits, imperative, summary <70 chars
(`feat: add PostgreSQL WAL archiving and PITR`). Types: `feat`, `fix`, `docs`,
`test`, `ci`, `build`, `chore`, `refactor`, `release`, `changelog`. Body for
justification, operator notes, or issue links (`Closes #123`). Sign off with
`git commit -s` (DCO required). User-visible changes get a `CHANGELOG.md` entry
under `## [Unreleased]`. Never commit secrets, real hostnames, or customer data.

**PRs** — `gh pr create --base master`. Title = lead commit summary. Body follows
`.github/PULL_REQUEST_TEMPLATE.md`; tick a checklist box only if you ran it, and
state what you couldn't run. `--draft` while checks are unverified. `area/*`
labels come from `.github/labeler.yml` — don't hand-apply. One logical change.
always create draft PR's for new branches.

**Releases** — the release workflow fails if the tag ≠ `pyproject.toml` version.
Bump version → move `[Unreleased]` into a dated section → `release: prepare X.Y.Z`
→ tag `vX.Y.Z`. Tagging is a human action.

## Plans and reviews

Non-trivial multi-step work gets a document in `docs/plans/` before implementation.

- **Plans:** `NNN-<kebab-slug>.md`, zero-padded and sequential from the highest
  existing number — `003-kubernetes-hpa-support.md`. The number is permanent;
  never renumber a landed plan.
- **Reviews and audits:** same prefix plus the date their findings were true —
  `002-documentation-versioning-audit-2026-08-08.md`. A review is a snapshot; a
  re-review is a new number, not an edit.
- **Header:** `Date:`, `Status:` (`DRAFT` / `IN PROGRESS` / `COMPLETE`), `Branch:`.
- **Body:** why the work exists → compatibility rules for existing projects →
  numbered items (`R1`, `R2`, …), each mapping to one commit with its own
  acceptance criteria.
- Update `Status:` as work lands. `COMPLETE` is a historical record — write a new
  plan rather than rewriting it.
- Plans aren't user docs. User-visible behavior goes in `docs/` + `mkdocs.yml` nav.

## Docs

Any user-visible change (command, flag, config field, default, safety behavior)
updates the matching `docs/` page, plus `examples/odooctl.yml` and `README.md`
where they show it. A new page must be added to `nav:` in `mkdocs.yml` or it
ships unreachable.

## Reporting

Match the surrounding code's conventions — read the implementation before
guessing. If a check failed or was skipped, say which and why.
