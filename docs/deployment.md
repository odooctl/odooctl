# Deployment

`odooctl deploy production --branch main` performs a pre-deploy backup, checks out/pulls the branch, pulls and starts Docker Compose services, runs module updates, performs health checks, and stores deployment metadata.

When `snapshots.pre_deploy` is `preferred` or `required`, deployment of the
protected environment bound by `snapshots.environment` is ordered as follows:

1. create the portable database + filestore backup;
2. request and verify completion of the configured provider snapshot;
3. begin the code/image rollout.

A required snapshot failure or pending status stops before rollout. With
`preferred`, either condition is recorded in deployment metadata and the
rollout continues; use `odooctl dr snapshot reconcile` to refresh pending
provider state. The provider binding represents one infrastructure source, so
another protected environment still gets its portable pre-deploy backup but
does not reuse the bound source's provider snapshot.

The automatic snapshot is tracked by its manifest and the enclosing deploy
operation/audit record; it does not create a separate `snapshot_create`
operation. Provider snapshots are coarse disaster-recovery artifacts, can
remain billable until removed at the provider, and never replace the portable
backup. See [Disaster recovery](disaster-recovery.md).

Deploy refuses to run when the git worktree has uncommitted changes. Commit or stash local edits before deploying so the recorded metadata and checkout/pull steps describe an intentional, reproducible code state.

If a production deploy fails, `odooctl deploy` may restart the service as a recovery attempt, but it does not automatically roll back code or data; use `odooctl rollback production --mode code` or `odooctl rollback production --mode full` for an explicit rollback.

`odooctl deploy staging --branch staging` follows the same flow without mandatory production backup.

`odooctl restore staging --backup latest` restores the selected backup, verifies checksums, runs the health check, and prints the restored backup id.

For CI/CD, `odooctl github-actions` generates a starter GitHub Actions workflow that exposes staging/production deploys as a manual dispatch job.

See `docs/operations/deploy-staging-production.md` for the operator workflow and branch/environment rules.
