from __future__ import annotations

import subprocess

import typer

from odooctl.context import ProjectContext
from odooctl.utils.logging import info, success, warn


def _overlay_gitignored(ctx: ProjectContext) -> bool | None:
    """Return whether Git ignores the machine-local overlay, when detectable."""
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(ctx.overlay_path)],
            cwd=ctx.root,
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True
        if result.returncode != 1:
            return None
        probe = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=ctx.root,
            capture_output=True,
            timeout=10,
        )
        return False if probe.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def execute(config_path: str = "odooctl.yml") -> None:
    ctx = ProjectContext.from_config_path(config_path)
    cfg = ctx.config
    env_names = ", ".join(sorted(cfg.environments))
    success(f"Config valid: {cfg.project.name} ({env_names})")

    if ctx.overlay_path is not None:
        info(f"Machine-local overlay merged: {ctx.overlay_path.name}")
        if _overlay_gitignored(ctx) is False:
            warn(
                f"{ctx.overlay_path.name} is not gitignored; add it to .gitignore "
                "(an untracked overlay blocks `odooctl sync` with dirty_worktree, "
                "and a committed one is no longer machine-local)"
            )

    missing = cfg.missing_env_vars(include_snapshot=True)
    if missing:
        warn("Missing referenced environment variables: " + ", ".join(missing))
    else:
        success("All referenced environment variables are set")


def run(config_path: str = "odooctl.yml") -> None:
    try:
        execute(config_path)
    except Exception as exc:
        raise typer.BadParameter(str(exc)) from exc
