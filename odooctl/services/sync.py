"""Sync service — pull-based auto-deploy: detect remote drift, deploy when behind.

`run_sync` is the polling half of the pull-based CI/CD model: fetch the remote,
compare the last deployed commit against the remote tip of the environment's
branch, and run the existing deploy pipeline when the environment is behind and
`auto_deploy: true` (or `force`). Every other state is a no-op with an explicit
status so schedulers can run it tightly without side effects.
"""
from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Callable

from odooctl.metadata.store import MetadataStore
from odooctl.services.deploy import run_deploy
from odooctl.services.models import SyncOutcome
from odooctl.utils.shell import run

if TYPE_CHECKING:
    from odooctl.services.context import ServiceContext

# Statuses that mean the environment needs human attention (surfaced as a
# non-zero exit by the CLI so systemd timers mark the unit failed).
ATTENTION_STATUSES = frozenset(
    {"deploy_failed", "dirty_worktree", "diverged", "no_remote", "fetch_failed", "unknown"}
)


def _git_rev(ref: str, cwd: str | None = None) -> str | None:
    result = run(["git", "rev-parse", "--short", ref], check=False, cwd=cwd)
    if result.returncode != 0:
        return None
    stdout = result.stdout.strip()
    return stdout if stdout else None


def _git_count(a: str, b: str, cwd: str | None = None) -> int | None:
    result = run(["git", "rev-list", "--count", f"{a}..{b}"], check=False, cwd=cwd)
    try:
        return int(result.stdout.strip())
    except (ValueError, TypeError):
        return None


def check_sync(ctx: ServiceContext, environment: str, *, force: bool = False) -> SyncOutcome:
    """Fetch and classify drift between the last deploy and the remote branch tip.

    Read-only apart from ``git fetch``; never mutates the worktree or containers.
    """
    cfg = ctx.project.config
    env = cfg.env(environment)
    cwd = str(ctx.project.root)

    def outcome(status: str, message: str, **kwargs) -> SyncOutcome:
        return SyncOutcome(
            environment=environment, branch=env.branch, status=status, message=message, **kwargs
        )

    fetch = run(["git", "fetch", "--all", "--quiet"], check=False, cwd=cwd)
    if fetch.returncode != 0:
        detail = fetch.stderr.strip() or fetch.stdout.strip()
        return outcome("fetch_failed", f"git fetch failed: {detail}")

    # Prefer the branch's configured upstream (any remote name); fall back to
    # origin/<branch> when the local branch or its upstream is not set up yet.
    remote_commit = _git_rev(f"{env.branch}@{{upstream}}", cwd=cwd) or _git_rev(
        f"origin/{env.branch}", cwd=cwd
    )
    if remote_commit is None:
        return outcome(
            "no_remote",
            f"no remote tracking ref found for branch '{env.branch}' "
            f"(checked {env.branch}@{{upstream}} and origin/{env.branch})",
        )

    last = MetadataStore(ctx.project.state_dir).latest_deployment(environment)
    deployed_commit = last.get("commit") if last else None
    if deployed_commit is None:
        return outcome(
            "never_deployed",
            f"no deployment recorded for '{environment}'; "
            f"run 'odooctl deploy {environment}' once to establish a baseline",
            remote_commit=remote_commit,
        )

    details = {"remote_commit": remote_commit, "deployed_commit": deployed_commit}
    if deployed_commit == remote_commit:
        # The remote hasn't moved since the last deploy attempt. If that
        # attempt failed, reporting up_to_date would silently mask a broken
        # environment forever; surface it instead. Retrying the same commit is
        # deliberate-human territory — a new push flips this back to behind.
        if last.get("status") != "success":
            return outcome(
                "deploy_failed",
                f"last deploy of {deployed_commit} failed"
                f"{': ' + last['message'] if last.get('message') else ''}; "
                f"push a fix or run 'odooctl deploy {environment}' manually",
                ahead=0,
                behind=0,
                **details,
            )
        return outcome(
            "up_to_date", f"deployed commit {deployed_commit} matches remote", ahead=0, behind=0, **details
        )

    behind = _git_count(deployed_commit, remote_commit, cwd=cwd)
    ahead = _git_count(remote_commit, deployed_commit, cwd=cwd)
    if behind is None or ahead is None:
        return outcome(
            "unknown",
            f"could not compare deployed commit {deployed_commit} with remote {remote_commit}",
            **details,
        )
    details.update({"ahead": ahead, "behind": behind})

    if behind > 0 and ahead == 0:
        if env.auto_deploy or force:
            # The deploy pipeline refuses dirty worktrees; catching it here
            # turns a per-poll failed operation record (and, on protected
            # envs, a pre-deploy backup attempt) into a clean no-op status.
            dirty = run(["git", "status", "--porcelain"], check=False, cwd=cwd).stdout.strip()
            if dirty:
                return outcome(
                    "dirty_worktree",
                    f"{behind} new commit(s) on remote {env.branch}, but the worktree at "
                    f"{cwd} has uncommitted changes; commit or stash them:\n{dirty}",
                    **details,
                )
            return outcome(
                "behind", f"{behind} new commit(s) on remote {env.branch}; deploy needed", **details
            )
        return outcome(
            "disabled",
            f"{behind} new commit(s) on remote {env.branch}, but auto_deploy is false "
            f"for '{environment}'; run 'odooctl deploy {environment}' or set auto_deploy: true",
            **details,
        )
    if ahead > 0:
        return outcome(
            "diverged",
            f"deployed commit {deployed_commit} and remote {remote_commit} have diverged "
            f"(ahead {ahead}, behind {behind}); resolve the branch history manually",
            **details,
        )
    return outcome("up_to_date", f"deployed commit {deployed_commit} matches remote", **details)


def run_sync(
    ctx: ServiceContext,
    environment: str,
    *,
    force: bool = False,
    deploy: Callable[[ServiceContext, str], object] | None = None,
) -> SyncOutcome:
    """Check drift and deploy when behind; every other state is a no-op.

    ``deploy`` lets the CLI wrap the deploy in the operation engine; it defaults
    to the plain deploy service.
    """
    outcome = check_sync(ctx, environment, force=force)
    if outcome.status != "behind":
        return outcome
    deploy_fn = deploy or run_deploy
    result = deploy_fn(ctx, environment)
    return replace(
        outcome,
        status="deployed",
        backup_id=getattr(result, "backup_id", None),
        message=f"deployed {environment} at {outcome.remote_commit} "
        f"({outcome.behind} new commit(s))",
    )
