from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from odooctl.config import validate_identifier
from odooctl.registry import (
    add_project,
    load_registry,
    remove_project,
    set_project_owner,
    use_project,
)

app = typer.Typer(help="Manage globally registered odooctl projects.", add_completion=False)
console = Console()


@app.command("add")
def add(
    name: str,
    path: Path = typer.Option(..., "--path", "-C", help="Path to the project repository."),
    config: str = typer.Option("odooctl.yml", "--config", help="Config path relative to the project."),
    no_use: bool = typer.Option(False, "--no-use", help="Register without making it active."),
    owner: str = typer.Option("", "--owner", help="Owning user/team (an email or team label)."),
):
    # Project names flow into registry keys and state paths; enforce the same
    # identifier rule as config env names so a name cannot inject path
    # components (audit finding F10).
    try:
        validate_identifier(name, "project name")
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    project = add_project(name, path, config, make_active=not no_use, owner=owner)
    suffix = f", owner {project.owner}" if project.owner else ""
    typer.echo(f"Registered project {project.name}: {project.path} ({project.config}{suffix})")


@app.command("list")
def list_projects(json_output: bool = typer.Option(False, "--json", "--json-output")):
    registry = load_registry()
    if json_output:
        import json

        typer.echo(
            json.dumps(
                {
                    "active": registry.active,
                    "projects": {
                        name: {
                            "path": str(project.path),
                            "config": project.config,
                            "owner": project.owner,
                        }
                        for name, project in sorted(registry.projects.items())
                    },
                },
                indent=2,
            )
        )
        return

    table = Table(title="odooctl projects")
    table.add_column("Active")
    table.add_column("Name")
    table.add_column("Path")
    table.add_column("Config")
    table.add_column("Owner")
    for name, project in sorted(registry.projects.items()):
        table.add_row(
            "*" if name == registry.active else "",
            name,
            str(project.path),
            project.config,
            project.owner or "-",
        )
    console.print(table)


@app.command("use")
def use(name: str):
    project = use_project(name)
    typer.echo(f"Active project: {project.name}")


@app.command("owner")
def owner(
    name: str,
    owner: str = typer.Argument(..., help="Owning user/team (an email or team label); '' to clear."),
):
    """Record who owns a registered project (shown in listings, API, and UI)."""
    project = set_project_owner(name, owner)
    if project.owner:
        typer.echo(f"Owner of {project.name}: {project.owner}")
    else:
        typer.echo(f"Cleared owner of {project.name}")


@app.command("remove")
def remove(name: str):
    remove_project(name)
    typer.echo(f"Removed project: {name}")


@app.command("current")
def current():
    registry = load_registry()
    if registry.active is None:
        typer.echo("No active project")
        raise typer.Exit(code=1)
    project = registry.projects.get(registry.active)
    if project is None:
        typer.echo(f"Active project missing from registry: {registry.active}")
        raise typer.Exit(code=1)
    typer.echo(f"{project.name}\t{project.path}\t{project.config}")
