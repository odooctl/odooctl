# M9 — Odoo.sh-Style Environment and Branch Model

## Goal

Add the Odoo.sh mental model: production, staging, dev/QA environments bound to Git branches, with safe promote and rollback.

## Config additions

Extend environment config with:

- `tier`: production, staging, development, qa
- `branch`
- `protected`
- `promotes_to`
- `auto_deploy`
- `last_deployed_commit`

Add promotion policy:

- require clean worktree
- require fresh backup
- require healthcheck
- optional test command
- required approvers later when RBAC/UI exists

## Files to create

- `odooctl/services/branch.py`
- `odooctl/services/promote.py`
- `odooctl/commands/branch.py`
- `odooctl/commands/promote.py`
- `tests/test_branch_status.py`
- `tests/test_promote.py`

## Flows

### Branch status

Show for each environment:

- environment name
- tier
- branch
- current commit
- last deployed commit
- ahead/behind
- drift status

### Promote staging → production

1. Lock production.
2. Check staging health.
3. Check branch state.
4. Take production backup.
5. Merge/fast-forward branch according to policy.
6. Deploy production.
7. Healthcheck.
8. On failure, rollback.
9. Record operation/audit.

### Ephemeral dev environment

`odooctl env open feature-x --from feature/x`

Creates a dev env by cloning/sanitizing from production and binding to a feature branch.

## Acceptance criteria

- Production is protected by default.
- Promote preview shows plan without side effects.
- Promote takes backup before deploy.
- Failed healthcheck rolls back.
- Branch status is available to CLI/API/UI.
