from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import click
import typer
import yaml
from rich.console import Console
from rich.table import Table

from odooctl.adapters.db import make_db_adapter
from odooctl.adapters.filestore import make_filestore_adapter
from odooctl.cli_selector import resolve_config_path
from odooctl.config import load_config
from odooctl.context import ProjectContext
from odooctl.operations.audit import AuditStore
from odooctl.operations.engine import run_operation
from odooctl.operations.models import OperationKind
from odooctl.operations.store import OperationStore
from odooctl.security.principals import local_actor

app = typer.Typer(help="Manage named Odoo environments in this project.", add_completion=False)
console = Console()


def _config_path(ctx: typer.Context, config: str) -> Path:
    return Path(resolve_config_path(ctx, config))


def _load_raw(path: Path) -> dict:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise click.ClickException(f"Config file must contain a YAML mapping: {path}")
    data.setdefault("environments", {})
    if not isinstance(data["environments"], dict):
        raise click.ClickException("Config key 'environments' must be a mapping")
    return data


def _write_raw(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False))


@app.command("list")
def list_envs(
    ctx: typer.Context,
    config: str = "odooctl.yml",
    json_output: bool = typer.Option(False, "--json", "--json-output"),
):
    cfg = load_config(_config_path(ctx, config))
    if json_output:
        import json

        typer.echo(
            json.dumps(
                {
                    name: env.model_dump(mode="json", exclude_none=True)
                    for name, env in sorted(cfg.environments.items())
                },
                indent=2,
            )
        )
        return

    table = Table(title="odooctl environments")
    table.add_column("Name")
    table.add_column("Branch")
    table.add_column("URL")
    table.add_column("DB")
    table.add_column("Clone from")
    table.add_column("Sanitize")
    for name, env in sorted(cfg.environments.items()):
        port = f":{env.port}" if env.port else ""
        selector = f"?db={env.db_name}" if env.db_selector else ""
        table.add_row(
            name,
            env.branch,
            f"{env.scheme}://{env.domain}{port}{selector}",
            env.db_name,
            env.clone_from or "",
            "yes" if env.sanitize else "no",
        )
    console.print(table)


@app.command("show")
def show_env(
    ctx: typer.Context,
    name: str,
    config: str = "odooctl.yml",
    json_output: bool = typer.Option(False, "--json", "--json-output"),
):
    cfg = load_config(_config_path(ctx, config))
    env = cfg.env(name)
    if json_output:
        import json

        typer.echo(json.dumps(env.model_dump(mode="json", exclude_none=True), indent=2))
        return
    typer.echo(yaml.safe_dump({name: env.model_dump(exclude_none=True)}, sort_keys=False).rstrip())


@app.command("create")
def create_env(
    ctx: typer.Context,
    name: str,
    clone_from: str = typer.Option(..., "--clone-from", help="Source environment to clone from."),
    branch: str | None = typer.Option(None, "--branch", help="Git branch for the new environment."),
    domain: str | None = typer.Option(None, "--domain", help="Public domain/host for the new environment."),
    scheme: str = typer.Option("https", "--scheme", help="URL scheme: http or https."),
    port: int | None = typer.Option(None, "--port", help="Optional URL port."),
    db_name: str | None = typer.Option(None, "--db-name", help="Database name; defaults to <project>_<env>."),
    filestore_path: str | None = typer.Option(None, "--filestore-path", help="Filestore path/subpath; defaults from DB name."),
    stack: str | None = typer.Option(None, "--stack", help="Compose stack identity; defaults to source stack."),
    db_selector: bool = typer.Option(False, "--db-selector", help="Append ?db=<db> to probes for shared-stack multi-db."),
    sanitize: bool = typer.Option(True, "--sanitize/--no-sanitize", help="Sanitize data while provisioning."),
    provision: bool = typer.Option(True, "--provision/--no-provision", help="Run safe clone after writing config."),
    config: str = "odooctl.yml",
):
    if scheme not in {"http", "https"}:
        raise click.ClickException("--scheme must be 'http' or 'https'")

    path = _config_path(ctx, config)
    data = _load_raw(path)
    current = load_config(path)
    if name in current.environments and current.is_protected(name):
        raise click.ClickException(f"Refusing to create or replace protected environment '{name}'")
    if name in data["environments"]:
        raise click.ClickException(f"Environment already exists: {name}")
    source = current.env(clone_from)
    project_name = current.project.name.replace("-", "_")
    new_db = db_name or f"{project_name}_{name}"
    source_filestore = str(source.filestore_path)
    if filestore_path is None:
        if source.db_name in source_filestore:
            filestore_path = source_filestore.replace(source.db_name, new_db)
        else:
            filestore_path = f"/var/lib/odoo/filestore/{new_db}"

    env_data = {
        "stack": stack or source.stack,
        "branch": branch or name,
        "scheme": scheme,
        "domain": domain or f"{name}.{source.domain}",
        "db_name": new_db,
        "filestore_path": filestore_path,
        "clone_from": clone_from,
        "sanitize": sanitize,
        "db_selector": db_selector,
    }
    if port is not None:
        env_data["port"] = port
    if source.filestore_volume:
        env_data["filestore_volume"] = source.filestore_volume
    if source.update_modules:
        env_data["update_modules"] = list(source.update_modules)

    updated = deepcopy(data)
    updated["environments"][name] = env_data
    # Validate before writing so failed creations do not corrupt the operator config.
    from odooctl.config import OdooCtlConfig

    OdooCtlConfig.model_validate(updated)
    _write_raw(path, updated)
    typer.echo(f"Created environment {name} in {path}")

    if provision:
        from odooctl.services.clone import run_clone
        from odooctl.services.context import ServiceContext
        svc_ctx = ServiceContext.from_config_path(str(path))
        op_store = OperationStore(svc_ctx.project.state_dir)
        audit = AuditStore(svc_ctx.project.state_dir)
        with run_operation(
            op_store,
            audit,
            kind=OperationKind.ENV_CREATE,
            project=svc_ctx.project.config.project.name,
            environment=name,
            actor=local_actor(),
            params_redacted={"name": name, "clone_from": clone_from},
            state_dir=svc_ctx.project.state_dir,
        ) as op_ctx:
            op_ctx.emit(f"provisioning {name} from {clone_from}", phase="env_create")
            run_clone(svc_ctx, clone_from, name, sanitize=sanitize)
            op_ctx.emit(f"environment {name} provisioned", phase="env_create")
        typer.echo(f"Provisioned {name} from {clone_from}")


@app.command("open")
def open_env(
    ctx: typer.Context,
    name: str,
    from_branch: str = typer.Option(..., "--from", help="Feature branch to bind this environment to."),
    from_env: str = typer.Option("production", "--from-env", help="Source environment to clone/sanitize from."),
    domain: str | None = typer.Option(None, "--domain", help="Public domain; defaults to <name>.<source domain>."),
    port: int | None = typer.Option(None, "--port", help="Optional URL port."),
    db_name: str | None = typer.Option(None, "--db-name", help="Database name; defaults to <project>_<name>."),
    provision: bool = typer.Option(True, "--provision/--no-provision", help="Clone and sanitize from source after config write."),
    config: str = "odooctl.yml",
):
    """Create an ephemeral development environment bound to a feature branch.

    Clones and sanitizes data from the source environment (default: production),
    then binds the new environment to the specified branch.

    Example: odooctl env open feature-x --from feature/x
    """
    if name in {"production", "staging"}:
        raise click.ClickException(f"Refusing to open a reserved environment name: {name}")

    path = _config_path(ctx, config)
    data = _load_raw(path)
    if name in data["environments"]:
        raise click.ClickException(f"Environment already exists: {name}")

    current = load_config(path)
    source = current.env(from_env)
    project_name = current.project.name.replace("-", "_")
    new_db = db_name or f"{project_name}_{name}"
    source_filestore = str(source.filestore_path)
    if source.db_name in source_filestore:
        filestore_path = source_filestore.replace(source.db_name, new_db)
    else:
        filestore_path = f"/var/lib/odoo/filestore/{new_db}"

    env_data: dict = {
        "tier": "development",
        "stack": source.stack,
        "branch": from_branch,
        "scheme": source.scheme,
        "domain": domain or f"{name}.{source.domain}",
        "db_name": new_db,
        "filestore_path": filestore_path,
        "clone_from": from_env,
        "sanitize": True,
        "db_selector": source.db_selector,
    }
    if port is not None:
        env_data["port"] = port
    if source.filestore_volume:
        env_data["filestore_volume"] = source.filestore_volume
    if source.update_modules:
        env_data["update_modules"] = list(source.update_modules)

    updated = deepcopy(data)
    updated["environments"][name] = env_data
    from odooctl.config import OdooCtlConfig

    OdooCtlConfig.model_validate(updated)
    _write_raw(path, updated)
    typer.echo(f"Created development environment {name} (branch: {from_branch}) in {path}")

    if provision:
        from odooctl.services.clone import run_clone
        from odooctl.services.context import ServiceContext
        svc_ctx = ServiceContext.from_config_path(str(path))
        op_store = OperationStore(svc_ctx.project.state_dir)
        audit = AuditStore(svc_ctx.project.state_dir)
        with run_operation(
            op_store,
            audit,
            kind=OperationKind.ENV_CREATE,
            project=svc_ctx.project.config.project.name,
            environment=name,
            actor=local_actor(),
            params_redacted={"name": name, "from_env": from_env, "branch": from_branch},
            state_dir=svc_ctx.project.state_dir,
        ) as op_ctx:
            op_ctx.emit(f"opening dev environment {name} from {from_env}", phase="env_open")
            run_clone(svc_ctx, from_env, name, sanitize=True)
            op_ctx.emit(f"environment {name} ready (branch: {from_branch})", phase="env_open")
        typer.echo(f"Provisioned {name} from {from_env} (sanitized)")


@app.command("destroy")
def destroy_env(
    ctx: typer.Context,
    name: str,
    purge: bool = typer.Option(False, "--purge", help="Also purge the non-production DB and filestore before removing config."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm destructive config removal."),
    config: str = "odooctl.yml",
):
    path = _config_path(ctx, config)
    data = _load_raw(path)
    if name not in data["environments"]:
        raise click.ClickException(f"Unknown environment: {name}")
    cfg = load_config(path)
    if cfg.is_protected(name):
        raise click.ClickException(f"Refusing to destroy protected environment '{name}'")
    if not yes:
        raise click.ClickException("Pass --yes to confirm environment removal")
    env = cfg.env(name)
    if purge:
        project_ctx = ProjectContext(path.parent, path, cfg)
        op_store = OperationStore(project_ctx.state_dir)
        audit = AuditStore(project_ctx.state_dir)
        with run_operation(
            op_store,
            audit,
            kind=OperationKind.ENV_DESTROY,
            project=cfg.project.name,
            environment=name,
            actor=local_actor(),
            params_redacted={"name": name, "purge": purge},
            state_dir=project_ctx.state_dir,
        ) as op_ctx:
            op_ctx.emit(f"purging environment {name}", phase="env_destroy")
            db = make_db_adapter(project_ctx)
            fs = make_filestore_adapter(project_ctx, env)
            db.drop(env.db_name)
            fs.delete(str(env.filestore_path))
            op_ctx.emit(f"environment {name} purged", phase="env_destroy")
    updated = deepcopy(data)
    del updated["environments"][name]
    from odooctl.config import OdooCtlConfig

    OdooCtlConfig.model_validate(updated)
    _write_raw(path, updated)
    typer.echo(f"Removed environment {name} from {path}")
