# Contributing to odooctl

Thanks for your interest in `odooctl`. This document covers the developer
setup, the checks we run, and the conventions we follow for code, commits,
and docs.

## Ground rules

- `odooctl` operates real Odoo databases and filestores. Default to safe,
  reversible behavior; protect production with explicit confirmation and
  backups.
- Discuss non-trivial changes in an issue before opening a large pull
  request.
- Contributions are accepted under the terms in
  [License and Developer Certificate of Origin](#license-and-developer-certificate-of-origin)
  below.

## License and Developer Certificate of Origin

odooctl is dual-licensed (see [LICENSING.md](LICENSING.md)): AGPL-3.0-or-later
for everyone, with a commercial license offered by the maintainer for
proprietary embedding and resale.

By submitting a contribution you agree that:

1. You certify the [Developer Certificate of Origin 1.1](https://developercertificate.org/)
   — you wrote the contribution or otherwise have the right to submit it.
   Signal this by adding a `Signed-off-by: Your Name <email>` line to your
   commits (`git commit -s`).
2. Your contribution is licensed under **AGPL-3.0-or-later**, and you
   additionally grant the project maintainer a perpetual, worldwide,
   royalty-free right to distribute your contribution under the project's
   commercial license.

This keeps the open-source project and its commercial licensing viable
without a separate CLA signature step. If your employer owns your work,
make sure you have permission to contribute under these terms.

## Development setup

`odooctl` uses [`uv`](https://docs.astral.sh/uv/) for dependency
management.

```bash
git clone https://github.com/odooctl/odooctl.git
cd odooctl
uv sync --frozen --extra dev --extra api
```

`--frozen` installs exactly what `uv.lock` pins, which is the same
environment CI builds. Do not use `uv pip install -e '.[dev]'` — it
re-resolves from the lower bounds in `pyproject.toml` and can give you a
dependency set that CI never tests.

Optional extras:

- Add `--extra s3` to work on the real S3 adapter.

Python 3.11 or newer is required.

## Tests, lint, and build

Run the same checks CI expects before opening a pull request. These are
the exact commands in `.github/workflows/ci.yml`:

```bash
uv run --frozen pytest -q --cov=odooctl --cov-fail-under=80   # unit tests
ODOO_DB_PASSWORD=odoo uv run --frozen pytest -q               # same suite with a secret env var set
uv run --frozen ruff check odooctl tests                      # lint
uv run --frozen python -m build                               # sdist + wheel build
```

Keep the `--frozen` flag. Without it `uv run` may silently update
`uv.lock` before running, so a green local check would no longer prove the
locked environment is green.

### Changing dependencies

`uv.lock` is authoritative. When you add or bump a dependency, edit
`pyproject.toml`, run `uv lock`, and commit the resulting `uv.lock` in the
same pull request. Routine refreshes arrive as Dependabot pull requests so
the lock changes are reviewed rather than applied implicitly during a CI
run.

Ruff is deliberately constrained to the `0.15.x` series. A new Ruff minor
release enables new rules and can fail an otherwise unchanged commit;
moving to `0.16` is a separate, intentional pull request that also fixes
the findings it introduces.

Notes:

- The default `pytest` configuration excludes the `integration` marker.
- Run integration tests explicitly with `uv run --frozen pytest -q -m integration`.
  They require external services (Docker, a running Odoo stack) and are not
  part of the default PR gate.
- The Docker integration fixture lives under
  `experiments/odoo19-community-staging/`. Use it to validate
  backup/restore/clone/update-modules end-to-end before shipping
  changes that touch those code paths. See its `README.md` for usage.

## Branching and commits

- Work from `master`. Create a feature branch named for the change, for
  example `feat/docker-volume-filestore` or `fix/clone-swap-guard`.
- Keep commits focused. Prefer multiple small, well-described commits over
  one large catch-all commit.
- Write commit messages in the imperative mood:
  `Add Docker PostgreSQL adapter`, not `Added` or `Adds`.
- The first line should be a short summary (under ~70 characters). Add a
  body when the change needs justification, links to issues, or
  upgrade/operator notes.
- Reference issues in the body when applicable
  (`Closes #123`, `Refs #456`).

## Pull requests

Each pull request should:

- Stay focused on one logical change.
- Include tests for new behavior or regressions you are fixing.
- Update docs under `docs/` and `examples/` when user-visible behavior,
  configuration, or commands change.
- Pass `uv run --frozen pytest -q`, `uv run --frozen ruff check odooctl tests`,
  and `uv run --frozen python -m build` locally.
- Note any deliberate skips (for example, integration runs that require a
  live Docker/Odoo fixture you could not reproduce locally).

## Code conventions

- Target Python 3.11+; respect the typing already used in the codebase.
- Keep adapters (`odooctl/adapters/`) free of CLI/UX concerns; route
  user-facing output through `odooctl/commands/`.
- Treat process working directory as untrusted: anchor file paths through
  `ProjectContext` instead of `os.getcwd()`.
- Do not log raw secrets. Use the redaction helpers and respect
  `redaction.min_secret_length` / `redaction.ignore_values`.
- Sanitization SQL must remain idempotent and guarded so it can run against
  any clone without raising on missing tables.

## Tests

- New unit tests belong under `tests/` and should run without Docker, a
  network, or environment variables that are not provided by the test.
- Use the `integration` marker for tests that require Docker or a real
  Odoo stack. They will be skipped by default and run explicitly with
  `-m integration`.
- The `docker` marker is reserved for tests that require Docker
  specifically.

## Documentation

- Update the relevant page in `docs/` for any user-visible change
  (commands, flags, config fields, defaults, safety behavior).
- Keep `README.md` examples in sync with actual CLI behavior.
- If you add or change a command, also update `examples/odooctl.yml` and
  the matching page under `docs/`.

## Reporting bugs and proposing features

- File a GitHub issue with a clear reproduction, your `odooctl` version,
  Python version, host OS, and execution mode (`host` or `docker`).
- Usage questions belong in
  [GitHub Discussions](https://github.com/odooctl/odooctl/discussions), not
  issues.
- For security issues, follow `SECURITY.md` instead of opening a public
  issue.

## Issue labels and triage

Labels are defined in
[`.github/labels.yml`](https://github.com/odooctl/odooctl/blob/master/.github/labels.yml)
and synced automatically; edit that file to change them. The taxonomy:

- **Type** — `bug`, `enhancement`, `documentation`, `question`, `security`,
  `breaking-change`.
- **`area/*`** — the affected subsystem (`area/cli`, `area/backup-restore`,
  `area/clone-sanitize`, …). PRs get these automatically from changed paths
  via [`.github/labeler.yml`](https://github.com/odooctl/odooctl/blob/master/.github/labeler.yml).
- **`priority/*`** — `critical` (data loss / production breakage) through
  `low`.
- **`status/*`** — triage flow: new issues start at `status/needs-triage`,
  then move to `status/confirmed`, `status/needs-info`, or `status/blocked`.
- **`odoo/*`** — which Odoo version(s) the issue affects.

Looking for something to work on? Start with
[`good first issue`](https://github.com/odooctl/odooctl/labels/good%20first%20issue)
or [`help wanted`](https://github.com/odooctl/odooctl/labels/help%20wanted),
and comment on the issue before starting so it can be assigned to you.

## Code of Conduct

Participation in this project is governed by the
[Code of Conduct](CODE_OF_CONDUCT.md). By contributing, you agree to abide
by its terms.
