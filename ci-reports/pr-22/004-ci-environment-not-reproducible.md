# Error 004: CI does not reproduce the repository's validated environment

## Impact

This is the common infrastructure cause behind the lint mismatch and the two CI-only CLI failures.

- Lint installs unconstrained latest Ruff.
- Tests run unconstrained pip resolution from broad lower bounds.
- The committed `uv.lock` is not used by CI.
- The PR validation statement uses `uv run`, so local/PR evidence and Actions exercise different dependency graphs.

On 2026-08-08, the frozen environment passed 1,460 unit tests on every supported Python version with 84.23% coverage, while fresh container installs also passed the two assertions that failed in the 2026-08-07 Actions run. This demonstrates that the workflow can change outcome without a source change.

## Where to fix

Primary location: `.github/workflows/ci.yml`.

Suggested focused plan:

1. Install `uv` in lint and test jobs.
2. Use `uv sync --frozen` with the required extras for each matrix Python.
3. Run tools via `uv run --frozen`.
4. Keep the Python 3.11/3.12/3.13 matrix.
5. Optionally add a dependency-update workflow so lock refreshes are reviewed rather than occurring implicitly during every CI run.

Also align the development instructions in `README.md` and the PR validation template with the exact CI commands.

## Verification after the fix

```bash
uv run --isolated --python 3.11 --extra dev --extra api --with pytest-cov pytest -q --cov=odooctl --cov-fail-under=80
uv run --isolated --python 3.12 --extra dev --extra api --with pytest-cov pytest -q --cov=odooctl --cov-fail-under=80
uv run --isolated --python 3.13 --extra dev --extra api --with pytest-cov pytest -q --cov=odooctl --cov-fail-under=80
uv run --python 3.12 --with ruff ruff check odooctl tests
```
