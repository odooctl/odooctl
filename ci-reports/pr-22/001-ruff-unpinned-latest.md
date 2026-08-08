# Error 001: CI lint uses an unpinned Ruff release

## Observed failure

- Check: `lint`
- Run: https://github.com/odooctl/odooctl/actions/runs/31188248235/job/92898192208
- CI command: `pip install ruff && ruff check odooctl tests`
- Current clean-environment result with Ruff 0.16.2: **567 violations**, of which 334 are automatically fixable.
- Locked repository result with Ruff 0.15.14: **passes**.

The branch validation cited in the PR and the CI runner do not install the same Ruff version. The CI job always resolves the newest release, so new lint rules can make an unchanged commit fail.

## Where to fix

Primary fix location: `.github/workflows/ci.yml`, in the lint job's `pip install ruff` step.

Choose one policy and make it explicit:

1. Reproducible policy: install the locked/dev Ruff version (preferably through `uv sync --frozen --extra dev`) and keep `uv.lock` authoritative.
2. Latest-Ruff policy: keep latest Ruff, then fix all 567 findings throughout `odooctl/` and `tests/`, and record the minimum/enforced version in `pyproject.toml`.

Do not bulk-apply `ruff --fix` without reviewing safety-sensitive exception handling. Many findings are `BLE001` on deliberate provider-boundary exception translation.

## Reproduce

```bash
uvx --from ruff ruff check odooctl tests
uv run --python 3.12 --with ruff ruff check odooctl tests
```

The first command resolves current Ruff; the second uses the repository lock.
