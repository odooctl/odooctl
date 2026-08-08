# Summary

<!-- What does this change and why? Link the issue if one exists. -->

## Checklist

- [ ] `uv run --frozen pytest -q --cov=odooctl --cov-fail-under=80` passes locally
- [ ] `uv run --frozen ruff check odooctl tests` passes
- [ ] `uv.lock` committed if dependencies changed (CI installs `--frozen`)
- [ ] New behavior is covered by tests
- [ ] Destructive-operation safety unchanged or strengthened (backups, confirmations, protected-env guards)
- [ ] No secrets in code, tests, or fixtures
- [ ] Docs updated if user-facing behavior changed
