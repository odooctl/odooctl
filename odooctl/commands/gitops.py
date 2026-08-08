"""GitOps and ephemeral pull-request environment commands."""
from __future__ import annotations

from pathlib import Path

import typer

from odooctl.services.context import ServiceContext
from odooctl.services.gitops import (
    cleanup_expired_previews,
    render_environment_overlay,
    render_preview_overlay,
)


app = typer.Typer(help="Render Kubernetes GitOps overlays and PR environments.")


@app.command("render")
def render(
    environment: str = typer.Option(..., "--env", "--environment"),
    config: str = "odooctl.yml",
    output: Path | None = typer.Option(None, "--output"),
) -> None:
    """Render one declared environment without applying it."""
    ctx = ServiceContext.from_config_path(config)
    result = render_environment_overlay(ctx, environment, output=output)
    typer.echo(result.directory)


@app.command("preview")
def preview(
    pull_request: int = typer.Option(..., "--pr"),
    revision: str = typer.Option(..., "--revision", "--sha"),
    config: str = "odooctl.yml",
    source: str | None = typer.Option(None, "--source"),
    ttl_hours: int | None = typer.Option(None, "--ttl-hours"),
    output: Path | None = typer.Option(None, "--output"),
) -> None:
    """Render a deterministic, neutralized, expiring PR environment."""
    ctx = ServiceContext.from_config_path(config)
    result = render_preview_overlay(
        ctx,
        pull_request,
        revision,
        source_environment=source,
        ttl_hours=ttl_hours,
        output=output,
    )
    typer.echo(result.directory)


@app.command("cleanup")
def cleanup(
    config: str = "odooctl.yml",
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Delete expired namespaces after ownership verification.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
    output: Path | None = typer.Option(None, "--output"),
) -> None:
    """List expired previews, or remove their owned namespaces."""
    if apply and not yes:
        raise typer.BadParameter("--apply requires --yes")
    ctx = ServiceContext.from_config_path(config)
    result = cleanup_expired_previews(ctx, apply=apply, output=output)
    action = "deleted" if apply else "expired"
    for identity in result.deleted if apply else result.expired:
        typer.echo(f"{action}: {identity}")
