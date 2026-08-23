# Git sync (pull-based CI/CD)

Pull-based sync is odooctl's **primary CI/CD model**: the server itself polls
the git remote and deploys when its branch has new commits. This is the ArgoCD
model at Docker Compose scale —

- the server only needs a **read-only deploy key** to the repository,
- **no inbound secrets or SSH access** have to be granted to a CI system,
- it **works behind NAT** and on machines with no public inbound access,
- every deploy runs the full odooctl pipeline: pre-deploy backup (protected
  environments), fast-forward-only pull, compose pull/up, module updates,
  health check, and an explicit rollback path.

## How it works

```
git push ──► remote (GitHub/GitLab/...)
                 ▲
                 │ git fetch (poll, every 5 min)
                 │
   VPS: odooctl sync <env>  ──►  behind + auto_deploy: true?
                                   ├─ yes → odooctl deploy pipeline
                                   └─ no  → no-op with a clear status
```

`odooctl sync <env>` fetches, then compares the **last deployed commit**
(from deployment metadata) with the **remote tip** of the environment's
branch:

| Status | Meaning | Action | Exit code |
| --- | --- | --- | --- |
| `up_to_date` | Deployed commit matches remote tip | none | 0 |
| `deployed` | Was behind; deploy pipeline ran | deploy | 0 |
| `disabled` | Behind, but `auto_deploy: false` | none (message) | 0 |
| `never_deployed` | No deployment recorded yet | none (run `odooctl deploy` once) | 0 |
| `deploy_failed` | Last deploy failed and the remote hasn't moved | none — push a fix (auto-retried) or deploy manually | 1 |
| `dirty_worktree` | Behind, but the server worktree has uncommitted changes | none — commit or stash them | 1 |
| `diverged` | Deployed and remote history diverged | none — resolve manually | 1 |
| `no_remote` | No upstream/`origin/<branch>` ref | none — fix remote config | 1 |
| `fetch_failed` | `git fetch` failed | none — check network/deploy key | 1 |

Attention states exit non-zero so a systemd timer marks the unit failed and
the problem is visible in `systemctl list-timers` / `journalctl`.

## Enabling auto-deploy

Set `auto_deploy: true` on the environments that should self-update:

```yaml
environments:
  staging:
    branch: staging
    auto_deploy: true
    # ...
  production:
    branch: main
    auto_deploy: true   # protected env: sync still takes a pre-deploy backup
    # ...
```

Without `auto_deploy: true`, `odooctl sync` reports drift but never deploys
(use `odooctl sync <env> --force` for a one-off override, or keep deploying
explicitly with `odooctl deploy`).

## Scheduling the poller

Generate a systemd service + timer (default: every 5 minutes):

```console
$ odooctl schedule sync --env staging
# /etc/systemd/system/odooctl-sync-staging.service
# /etc/systemd/system/odooctl-sync-staging.timer  (OnCalendar=*:0/5)
```

The service unit runs with a minimal environment: add an `EnvironmentFile=`
line (or `Environment=` entries) to the `[Service]` section supplying the
environment variables your config references (e.g. `ODOO_DB_PASSWORD`), the
same as for scheduled backups. Install the rendered units, then:

```console
$ sudo systemctl daemon-reload
$ sudo systemctl enable --now odooctl-sync-staging.timer
```

A cron variant is available with `--format cron` (renders `*/5 * * * *` by
default), and `--interval` overrides the cadence (systemd `OnCalendar` value
or cron expression, e.g. `--interval "*:0/2"` for every 2 minutes).

Manual checks any time:

```console
$ odooctl sync staging            # human-readable status
$ odooctl sync staging --json     # machine-readable
$ odooctl branch status           # drift table across all environments
```

!!! note "Config changes lag one deploy"
    The deploy pipeline reads `odooctl.yml` **before** pulling, so a pushed
    commit that changes `odooctl.yml` itself is deployed using the previous
    config; the new config takes effect from the next deploy on. If such a
    deploy ends in `deploy_failed` (e.g. the old healthcheck settings no
    longer match), run `odooctl deploy <env>` manually — it re-reads the
    updated config from the already-pulled worktree.

!!! note "Machine-local overlay and sync"
    A server-side [`odooctl.local.yml` overlay](configuration.md#machine-local-overlay-odooctllocalyml)
    is unaffected by pulls (git never touches it), so server-specific settings
    survive every sync. It **must be gitignored**: an untracked-but-not-ignored
    overlay makes `git status` dirty and blocks sync with `dirty_worktree`.
    `odooctl validate` warns about this.

## Push-based deploys (secondary)

`odooctl github-actions` generates a manual-dispatch GitHub Actions workflow
as a **secondary** model. It requires a **self-hosted runner** on (or with
Docker access to) your server — GitHub-hosted runners (`ubuntu-latest`)
cannot reach your VPS Docker daemon. Prefer pull-based sync unless you
specifically need deploys driven from GitHub (e.g. approvals in PR flow);
a post-1.0 webhook trigger on `odooctl serve` is planned as a latency
optimization, with polling remaining the source of truth.
