from __future__ import annotations

import typer

from odooctl.context import ProjectContext
from odooctl.utils.logging import success, warn


def execute(config_path: str = "odooctl.yml") -> None:
    cfg = ProjectContext.from_config_path(config_path).config
    env_names = ", ".join(sorted(cfg.environments))
    success(f"Config valid: {cfg.project.name} ({env_names})")

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
