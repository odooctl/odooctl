"""k3d + Tilt local production simulation commands."""
from __future__ import annotations

import typer

from odooctl.services.context import ServiceContext
from odooctl.services.local_cluster import (
    create_local_cluster,
    delete_local_cluster,
    render_local_simulation,
    run_local_smoke,
)


app = typer.Typer(help="Run the owned k3d + Tilt production simulation.")


@app.command("render")
def render(config: str = "odooctl.yml") -> None:
    """Render k3d, Kubernetes, and Tilt definitions without mutation."""
    result = render_local_simulation(ServiceContext.from_config_path(config))
    typer.echo(result.directory)


@app.command("up")
def up(config: str = "odooctl.yml") -> None:
    """Create the deterministic project-owned k3d cluster."""
    result = create_local_cluster(ServiceContext.from_config_path(config))
    typer.echo(f"cluster: {result.cluster_name}")
    typer.echo(f"tilt: tilt up -f {result.tiltfile}")


@app.command("smoke")
def smoke(config: str = "odooctl.yml") -> None:
    """Exercise deploy, native neutralize, backup/restore, and rollback."""
    result = run_local_smoke(ServiceContext.from_config_path(config))
    typer.echo(
        f"smoke passed: cluster={result.cluster_name} "
        f"database={result.database} restore={result.restored_database}"
    )


@app.command("down")
def down(
    config: str = "odooctl.yml",
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Delete only the k3d cluster recorded as owned by this project."""
    if not yes:
        raise typer.BadParameter("local down requires --yes")
    cluster = delete_local_cluster(ServiceContext.from_config_path(config))
    typer.echo(f"deleted: {cluster}")
