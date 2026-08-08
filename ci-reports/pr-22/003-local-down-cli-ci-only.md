# Error 003: Local cluster teardown confirmation assertion failed in CI

## Observed failure

- Checks: `test (3.11)`, `test (3.12)`, and `test (3.13)`
- Test: `tests/test_local_cluster.py::test_local_down_cli_requires_explicit_yes`
- Assertion: expected `local down requires --yes`, but CI returned Typer usage text and `SystemExit(2)`.

The same PR commit now passes this test in fresh Python 3.11, 3.12, and 3.13 containers and in the frozen-lock unit matrix. As with Error 002, the evidence points to CI dependency drift or a transient dependency combination.

## Where to fix

- Reproducibility fix: `.github/workflows/ci.yml`, changing the test install to consume `uv.lock` with frozen resolution.
- Constraint definitions: `pyproject.toml` and `uv.lock`.
- If reproducible after pinning: inspect `odooctl/main.py` local command registration and `odooctl/commands/local.py` option declarations.
- Assertion location: `tests/test_local_cluster.py:431`.

The destructive-operation contract should remain explicit: omitting `--yes` must reach application validation and emit the expected safety message.

## Reproduce

```bash
uv run --isolated --python 3.12 --extra dev --extra api \
  pytest -q tests/test_local_cluster.py::test_local_down_cli_requires_explicit_yes
```
