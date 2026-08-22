# PR 29 readiness fixes

Date: 2026-08-23
Status: COMPLETE
Branch: docs/versioned-documentation

## Why this work exists

The end-to-end review of PR 29 found failing CI, a shallow release checkout,
historical documentation that still contradicted the published packages, and
an accidental replacement of the operator security guide. These issues must be
fixed before the versioned-documentation publisher can merge.

Existing project behavior and destructive-operation guards remain unchanged.
Historical corrections are applied only to staged documentation sources and
must fail closed when their expected tagged text is absent.

## R1 — Make versioned documentation merge-ready

Restore the operator security guide, distinguish the released beta from the
unreleased staging-login fix, correct invalid API-key examples, and apply
minimal, exact-match installation corrections to retained tagged sources.
Install the locked project environment before running the documentation
checker, fetch all retained tags in release jobs, publish development docs from
`master`, preserve the original `/docs/` entry point, and make the
pre-Ruff-0.16 lint rules explicit.

Acceptance criteria:

- `docs/security.md` retains the established trust and safety boundaries.
- No published-version page claims the tagged beta contains an unreleased fix.
- Backports preserve unrelated historical documentation and fail when their
  expected source text changes.
- Focused documentation/backport tests pass.
- The frozen Ruff command passes without narrowing the allowed Ruff version.
- The full versioned documentation tree builds from retained tags.
- Release and docs workflows have the dependencies and history their commands
  require.
- The landing page, root stable alias, immutable versions, channel aliases,
  and page-preserving selector have no broken internal links.

## R2 — Validate and merge

Run the frozen unit/coverage, lint, and build checks plus versioned-doc and link
validation. Record the fixes and exact checks on PR 29, mark it ready, wait for
required checks, and merge only when all are green.

Acceptance criteria:

- All required local and GitHub checks pass.
- The PR comment records every review fix and any intentionally skipped check.
- PR 29 is merged without bypassing a failing required check.
