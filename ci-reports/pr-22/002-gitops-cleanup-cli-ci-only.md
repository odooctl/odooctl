# Error 002: GitOps cleanup confirmation assertion failed in CI

## Observed failure

- Checks: `test (3.11)`, `test (3.12)`, and `test (3.13)`
- Test: `tests/test_gitops.py::test_cleanup_cli_requires_explicit_yes`
- Assertion: expected `--apply requires --yes`, but CI returned Typer usage text and `SystemExit(2)`.

The same PR commit now passes this test in fresh `python:3.11-slim`, `python:3.12-slim`, and `python:3.13-slim` containers. It also passes in the locked unit matrix. This makes the recorded failure consistent with dependency resolution drift or a transient bad dependency combination, not a currently reproducible application defect.

## Where to fix

- Reproducibility fix: `.github/workflows/ci.yml` (`pip install -e '.[dev,api]' pytest-cov` currently ignores `uv.lock`).
- Constraint definitions: `pyproject.toml` and `uv.lock`.
- If the failure remains after pinning: inspect `odooctl/main.py` GitOps command registration and `odooctl/commands/gitops.py` option declarations.
- Assertion robustness, only if intentional CLI behavior changed: `tests/test_gitops.py:226`.

Recommended first change is to make CI install the frozen lockfile. Do not weaken the safety assertion merely to accept parser usage output; the command must reach the explicit `--yes` guard.

## Reproduce

```bash
uv run --isolated --python 3.12 --extra dev --extra api \
  pytest -q tests/test_gitops.py::test_cleanup_cli_requires_explicit_yes
```
