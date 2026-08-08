from __future__ import annotations

from importlib.metadata import version
from pathlib import Path

import typer
from odooctl.cli_selector import resolve_config_path
from odooctl.commands import (
    backup as backup_cmd,
    backup_remote as backup_remote_cmd,
    branch as branch_cmd,
    catalog as catalog_cmd,
    clone as clone_cmd,
    deploy as deploy_cmd,
    doctor as doctor_cmd,
    domain as domain_cmd,
    dr as dr_cmd,
    env as env_cmd,
    filestore_storage as filestore_cmd,
    github_actions as gha_cmd,
    gitops as gitops_cmd,
    import_cmd,
    init as init_cmd,
    logs as logs_cmd,
    local as local_cmd,
    migrate as migrate_cmd,
    ops as ops_cmd,
    pitr as pitr_cmd,
    project as project_cmd,
    promote as promote_cmd,
    restore as restore_cmd,
    rollback as rollback_cmd,
    runner as runner_cmd,
    schedule as schedule_cmd,
    security as security_cmd,
    serve as serve_cmd,
    setup as setup_cmd,
    status as status_cmd,
    update_modules as update_cmd,
    validate as validate_cmd,
)

app = typer.Typer(
    help="Odoo-aware deployment CLI for self-hosted Compose and Kubernetes projects.",
)
app.add_typer(project_cmd.app, name="project")
app.add_typer(env_cmd.app, name="env")
app.add_typer(ops_cmd.app, name="ops")
app.add_typer(branch_cmd.app, name="branch")
app.add_typer(catalog_cmd.app, name="catalog")
app.add_typer(security_cmd.app, name="security")
app.add_typer(domain_cmd.app, name="domain")
app.add_typer(dr_cmd.app, name="dr")
app.add_typer(pitr_cmd.app, name="pitr")
app.add_typer(filestore_cmd.app, name="filestore")
app.add_typer(migrate_cmd.app, name="migrate")
app.add_typer(backup_remote_cmd.app, name="backup-remote")
app.add_typer(gitops_cmd.app, name="gitops")
app.add_typer(local_cmd.app, name="local")


def _context_config(ctx: typer.Context, config: str) -> str:
    return str(resolve_config_path(ctx, config, normalize=False))


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version_option: bool = typer.Option(False, "--version", help="Show the installed odooctl version and exit."),
    project: str | None = typer.Option(None, "--project", "-p", help="Registered project name to operate on."),
    project_dir: Path | None = typer.Option(None, "--project-dir", "-C", help="Project directory for ad-hoc operation."),
):
    ctx.obj = {"project": project, "project_dir": project_dir}
    if version_option:
        typer.echo(f"odooctl {version('odooctl')}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


@app.command()
def init(output: str = "odooctl.yml", dry_run: bool = False, force: bool = False):
    init_cmd.run(output, dry_run, force)

@app.command()
def deploy(ctx: typer.Context, environment: str, branch: str | None = None, config: str = "odooctl.yml"):
    deploy_cmd.execute(environment, branch, _context_config(ctx, config))

@app.command()
def backup(
    ctx: typer.Context,
    environment: str,
    config: str = "odooctl.yml",
    verify: bool = typer.Option(False, "--verify", help="Verify the backup after creation."),
):
    backup_id = backup_cmd.execute(environment, _context_config(ctx, config), verify=verify)
    typer.echo(backup_id)

def _confirm_destructive(action: str, target: str, *, yes: bool) -> None:
    """Interactive gate for operations that overwrite a database.

    ``--yes`` skips the prompt for non-interactive callers (CI, runner scripts).
    """
    if yes:
        return
    typer.echo(f"About to {action} — this OVERWRITES the current database of '{target}'.")
    if not typer.confirm(f"Type y to continue with {action}"):
        typer.echo("Aborted.")
        raise typer.Exit(code=1)


@app.command()
def restore(
    ctx: typer.Context,
    environment: str,
    backup: str = "latest",
    config: str = "odooctl.yml",
    to: str | None = typer.Option(None, "--to", help="Restore source environment backup into this target environment (e.g. --to staging)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the interactive confirmation prompt."),
):
    if to is not None:
        _confirm_destructive(f"restore {environment} backup '{backup}' into {to}", to, yes=yes)
        backup_id = restore_cmd.execute_to(environment, to, backup, _context_config(ctx, config))
        typer.echo(f"Restored {environment} backup {backup_id} into {to}")
    else:
        _confirm_destructive(f"restore {environment} from backup '{backup}'", environment, yes=yes)
        backup_id = restore_cmd.execute(environment, backup, _context_config(ctx, config))
        typer.echo(f"Restored {environment} from backup {backup_id}")

@app.command(name="clone")
def clone_env(
    ctx: typer.Context,
    source: str,
    target: str,
    sanitize: bool = True,
    config: str = "odooctl.yml",
    sanitization_profile: str = typer.Option("normal", "--sanitization-profile", "--profile"),
    preview: bool = typer.Option(False, "--preview", "--dry-run"),
):
    url = clone_cmd.execute(source, target, sanitize, _context_config(ctx, config), sanitization_profile, preview)
    typer.echo(f"Staging URL: {url}")

@app.command("update-modules")
def update_modules(ctx: typer.Context, environment: str, modules: str | None = None, config: str = "odooctl.yml"):
    parsed = [m.strip() for m in modules.split(",")] if modules else None
    update_cmd.execute(environment, parsed, _context_config(ctx, config))

@app.command()
def rollback(
    ctx: typer.Context,
    environment: str,
    mode: str = "code",
    backup: str | None = None,
    config: str = "odooctl.yml",
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the interactive confirmation prompt (full mode only)."),
):
    if mode == "full":
        _confirm_destructive(
            f"rollback {environment} to backup '{backup or 'latest'}' (code + database)", environment, yes=yes
        )
    rollback_cmd.execute(environment, mode, backup, _context_config(ctx, config))


@app.command()
def promote(
    ctx: typer.Context,
    source: str,
    target: str,
    preview: bool = typer.Option(False, "--preview", "--dry-run", help="Show promote plan without side effects."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm promote to a protected target."),
    config: str = "odooctl.yml",
):
    promote_cmd.execute(source, target, _context_config(ctx, config), preview=preview, yes=yes)

@app.command()
def logs(ctx: typer.Context, environment: str, service: str | None = None, config: str = "odooctl.yml", follow: bool = True, tail: int | None = None):
    logs_cmd.execute(environment, service, _context_config(ctx, config), follow=follow, tail=tail)

@app.command()
def status(
    ctx: typer.Context,
    config: str = "odooctl.yml",
    environment: str | None = None,
    json_output: bool = typer.Option(False, "--json", "--json-output"),
):
    status_cmd.execute(_context_config(ctx, config), environment, json_output=json_output)


@app.command()
def validate(ctx: typer.Context, config: str = "odooctl.yml"):
    validate_cmd.run(_context_config(ctx, config))


@app.command()
def doctor(
    ctx: typer.Context,
    config: str = "odooctl.yml",
    json_output: bool = typer.Option(False, "--json", "--json-output"),
):
    doctor_cmd.execute(_context_config(ctx, config), json_output=json_output)


@app.command()
def schedule(
    ctx: typer.Context,
    command: str = typer.Argument(
        ...,
        help=(
            "Schedule: backup, backup-remote-verify, dr-drill, doctor, "
            "pitr-base, or pitr-reconcile."
        ),
    ),
    environment: str = typer.Option(..., "--env", "--environment", help="Environment to target."),
    format: str = typer.Option("systemd", "--format", "-f", help="Output format: systemd or cron."),
    interval: str = typer.Option(
        "daily",
        "--interval",
        "-i",
        "--cron",
        help="systemd OnCalendar value or cron alias/expression.",
    ),
    cron_line: bool = typer.Option(False, "--cron-line", help="Shortcut for --format cron."),
    user: str | None = typer.Option(None, "--user", "-u", help="User for systemd service or /etc/cron.d entry."),
    odooctl_bin: str = typer.Option("odooctl", "--odooctl-bin", help="Path/name of the odooctl executable."),
    environment_file: Path | None = typer.Option(
        None,
        "--environment-file",
        help="Load schedule secrets from this systemd/POSIX environment file.",
    ),
    config: str = "odooctl.yml",
):
    try:
        content = schedule_cmd.render(
            command,
            environment,
            _context_config(ctx, config),
            format="cron" if cron_line else format,
            interval=interval,
            user=user,
            odooctl_bin=odooctl_bin,
            environment_file=environment_file,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(content)


@app.command(name="github-actions")
def github_actions(ctx: typer.Context, config: str = "odooctl.yml", output: str = ".github/workflows/odooctl-deploy.yml", dry_run: bool = False, force: bool = False):
    content = gha_cmd.run(_context_config(ctx, config), output, dry_run, force)
    if dry_run:
        typer.echo(content)


@app.command(name="import")
def import_project(
    path: Path | None = typer.Argument(None, help="Path to docker-compose.yml or its directory."),
    preview: bool = typer.Option(False, "--preview", help="Show preview only (default behaviour)."),
    name: str | None = typer.Option(None, "--name", "-n", help="Project name for the generated config."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Write odooctl.yml after preview (adoption)."),
    force: bool = typer.Option(False, "--force", help="Overwrite existing odooctl.yml."),
    output: Path = typer.Option(Path("odooctl.yml"), "--output", "-o", help="Output path for generated config."),
    skip_doctor: bool = typer.Option(False, "--skip-doctor", help="Skip preflight doctor checks after adoption."),
    skip_backup: bool = typer.Option(False, "--skip-backup", help="Skip safety backup after adoption."),
    allow_outside: bool = typer.Option(False, "--allow-outside", help="Permit --output paths outside the current and imported project directories."),
) -> None:
    """Import an existing Docker Compose Odoo deployment (read-only detection, then optional adoption).

    Detection is strictly read-only: no containers are touched, no DBs mutated,
    no volumes written, and no secret values are printed or stored.

    Run without --yes to preview. Add --yes to write the generated odooctl.yml.
    Use --force to overwrite an existing file.

    After adoption, the project is registered, config is validated, doctor
    preflight checks run, and a safety backup is created. Use --skip-doctor
    or --skip-backup to suppress those post-adoption steps.
    """
    import_cmd.run(
        path,
        preview=preview,
        name=name,
        yes=yes,
        force=force,
        output=output,
        skip_doctor=skip_doctor,
        skip_backup=skip_backup,
        allow_outside=allow_outside,
    )


@app.command()
def setup(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip interactive prompts and use defaults."),
    stack: str | None = typer.Option(None, "--stack", help="Stack name (e.g. odoo-19-community)."),
    name: str | None = typer.Option(None, "--name", "-n", help="Project name."),
    output: Path = typer.Option(Path("odooctl.yml"), "--output", "-o", help="Output path."),
    force: bool = typer.Option(False, "--force", help="Overwrite existing odooctl.yml."),
    catalog: Path | None = typer.Option(
        None,
        "--catalog",
        help="Path to a YAML catalog manifest to extend bundled entries for this invocation.",
    ),
) -> None:
    """Scaffold a greenfield Odoo project (new deployment, no existing stack).

    For taking over an existing running deployment, use 'odooctl import' instead.
    Secrets are referenced by env-var name only — never inlined in the generated config.
    Use --catalog to extend the bundled catalog with custom StackTemplate entries.
    """
    setup_cmd.run(yes=yes, stack=stack, name=name, output=output, force=force, catalog=catalog)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host (default: localhost only)."),
    port: int = typer.Option(8787, "--port", "-p", help="Bind port."),
    api_key: str | None = typer.Option(None, "--api-key", envvar="ODOOCTL_API_KEY", help="HMAC key for bearer tokens."),
    static_dir: Path | None = typer.Option(None, "--static-dir", help="Directory of pre-built SPA assets to serve at /."),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes (dev only)."),
) -> None:
    """Start the local API server (requires odooctl[api] extras).

    Binds to 127.0.0.1 by default. Pass --api-key or set ODOOCTL_API_KEY.
    Optionally serve a static SPA from --static-dir at /.
    """
    serve_cmd.run(host=host, port=port, api_key=api_key, static_dir=static_dir, reload=reload)


@app.command()
def runner(
    once: bool = typer.Option(False, "--once", help="Process one operation and exit."),
    fail_fast: bool = typer.Option(False, "--fail-fast", help="In loop mode, exit non-zero as soon as an operation fails."),
    api_key: str | None = typer.Option(None, "--api-key", envvar="ODOOCTL_API_KEY", help="HMAC key for capability-token verification."),
) -> None:
    """Run the privileged operation runner.

    Claims and executes queued operations from all registered projects.
    Requires Docker, Postgres, and filestore access.
    Use --once to process a single operation and exit (useful for testing);
    it exits non-zero if the processed operation failed.
    """
    runner_cmd.run(once=once, fail_fast=fail_fast, api_key=api_key)


if __name__ == "__main__":
    app()
