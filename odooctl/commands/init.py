from __future__ import annotations
from pathlib import Path
import typer
from odooctl.config import example_config, local_overlay_path
from odooctl.utils.logging import info, success, warn


def ensure_overlay_gitignored(config_path: Path) -> None:
    """Add the machine-local overlay to .gitignore next to the config file.

    The overlay must stay untracked: committing it defeats machine-local
    settings, and an untracked-but-not-ignored overlay blocks `odooctl sync`
    with dirty_worktree.
    """
    overlay = local_overlay_path(config_path)
    if overlay is None:
        return
    gitignore = config_path.resolve().parent / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    if overlay.name in existing.splitlines():
        return
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    gitignore.write_text(existing + prefix + overlay.name + "\n")
    info(f"Added {overlay.name} to {gitignore.name} (machine-local overlay stays untracked)")


def run(output: str = "odooctl.yml", dry_run: bool = False, force: bool = False) -> None:
    content = example_config()
    path = Path(output)
    if dry_run:
        typer.echo(content)
        return
    if path.exists() and not force:
        raise typer.BadParameter(f"{output} already exists; pass --force to overwrite")
    path.write_text(content)
    success(f"Created {output}")
    ensure_overlay_gitignored(path)
    warn("Review domains, db names, paths, and environment variable names before deployment.")
