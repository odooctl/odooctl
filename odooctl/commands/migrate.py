"""Migration and upgrade assistant CLI commands."""
from __future__ import annotations

import typer

app = typer.Typer(help="Migration and upgrade assistant.")


@app.command()
def matrix() -> None:
    """Print supported Odoo upgrade paths."""
    from odooctl.migration.matrix import format_matrix

    typer.echo(format_matrix())


@app.command()
def scan(
    env: str = typer.Option(..., "--env", help="Source environment to scan."),
    to: str = typer.Option(..., "--to", help="Target Odoo version (e.g. 18.0)."),
    config: str = "odooctl.yml",
) -> None:
    """Scan installed modules for upgrade readiness."""
    from odooctl.migration.scan import scan_modules
    from odooctl.services.context import ServiceContext
    import os

    ctx = ServiceContext.from_config_path(config)
    cfg = ctx.project.config
    env_cfg = cfg.env(env)
    from_version = cfg.project.odoo_version

    def _list_modules() -> list[str]:
        from odooctl.utils.shell import run

        pg = cfg.postgres
        result = run(
            [
                "psql",
                "-h", pg.host,
                "-p", str(pg.port),
                "-U", pg.user,
                "-d", env_cfg.db_name,
                "-t",
                "-c",
                "SELECT name FROM ir_module_module WHERE state='installed' ORDER BY name;",
            ],
            env={"PGPASSWORD": os.getenv(pg.password_env, "")},
            check=True,
            stream=False,
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    result = scan_modules(
        from_version=from_version,
        to_version=to,
        module_list_fn=_list_modules,
    )

    typer.echo(f"Scan: {env!r}  {from_version} → {to}")
    typer.echo(f"Installed modules: {len(result.installed_modules)}")
    if result.blockers:
        typer.secho("Blockers:", fg=typer.colors.RED)
        for b in result.blockers:
            typer.echo(f"  [BLOCK] {b}")
    if result.warnings:
        typer.secho("Warnings:", fg=typer.colors.YELLOW)
        for w in result.warnings:
            typer.echo(f"  [WARN]  {w}")
    if not result.blockers and not result.warnings:
        typer.secho("No blockers or warnings found.", fg=typer.colors.GREEN)

    if result.blockers:
        raise typer.Exit(code=1)


@app.command()
def rehearse(
    env: str = typer.Option(..., "--env", help="Source environment to rehearse upgrade from."),
    to: str = typer.Option(..., "--to", help="Target Odoo version (e.g. 18.0)."),
    keep: bool = typer.Option(
        False, "--keep", help="Keep the throwaway DB after rehearsal (for debugging)."
    ),
    openupgrade: bool = typer.Option(
        False, "--openupgrade", help="Use OpenUpgrade (OCA) for the upgrade command."
    ),
    config: str = "odooctl.yml",
) -> None:
    """Run an upgrade rehearsal (never touches production DB or filestore).

    Clones the source environment DB into a throwaway DB, runs the upgrade
    command, healthchecks, writes a report, and drops the throwaway DB.
    """
    from odooctl.migration.rehearse import rehearse_upgrade, UpgradeResult
    from odooctl.migration.openupgrade import get_openupgrade_meta
    from odooctl.migration.matrix import supported_paths
    from odooctl.services.context import ServiceContext
    from odooctl.adapters.db import make_db_adapter

    ctx = ServiceContext.from_config_path(config)
    cfg = ctx.project.config
    env_cfg = cfg.env(env)
    db_adapter = make_db_adapter(ctx.project)
    from_version = cfg.project.odoo_version

    if openupgrade:
        meta = get_openupgrade_meta(to)
        if meta is None:
            raise typer.BadParameter(
                f"OpenUpgrade does not support version {to!r}; "
                "check odooctl/migration/openupgrade.py PINNED_BRANCHES"
            )

    matrix_paths = supported_paths(from_version=from_version, to_version=to)
    if not matrix_paths:
        raise typer.BadParameter(
            f"No supported migration path from {from_version!r} to {to!r}; "
            "run 'odooctl migrate matrix' to see supported paths.",
            param_hint="--to",
        )
    path_requires_ou = any(p.requires_openupgrade for p in matrix_paths)

    def _upgrade_fn(throwaway_db: str, target_version: str) -> UpgradeResult:
        from odooctl.adapters.runtime import make_runtime_adapter

        runtime = make_runtime_adapter(ctx.project)
        if openupgrade:
            from odooctl.migration.openupgrade import openupgrade_db_command

            cmd = openupgrade_db_command(throwaway_db, target_version)
            if cmd is None:
                raise ValueError(
                    f"OpenUpgrade does not support target version {target_version!r}"
                )
        else:
            cmd = [
                "odoo",
                "--database", throwaway_db,
                "--update", "all",
                "--stop-after-init",
            ]
        try:
            runtime.exec(cfg.odoo.service, cmd, stream=True)
            return UpgradeResult(ok=True)
        except Exception as exc:
            return UpgradeResult(ok=False, warnings=[str(exc)])

    def _healthcheck_fn(db_name: str) -> bool:
        # Ping the throwaway DB directly — after --stop-after-init Odoo is not running,
        # so an HTTP check against the source env URL would test the wrong target.
        try:
            db_adapter.ping(db_name)
            return True
        except Exception:
            return False

    report_dir = ctx.project.state_dir / "migration_reports"

    result = rehearse_upgrade(
        source_env=env,
        source_version=from_version,
        target_version=to,
        source_db=env_cfg.db_name,
        db_adapter=db_adapter,
        healthcheck_fn=_healthcheck_fn,
        upgrade_fn=_upgrade_fn,
        report_dir=report_dir,
        keep=keep,
        requires_openupgrade=path_requires_ou,
        use_openupgrade=openupgrade,
    )

    typer.echo(f"\nRehearsal result: {result.status.upper()}")
    typer.echo(f"Duration:         {result.duration_seconds:.1f}s")
    typer.echo(f"Source:           {env!r}  {from_version} → {to}")
    typer.echo(f"Modules after:    {len(result.installed_modules)}")
    typer.echo(f"Failed modules:   {len(result.failed_modules)}")
    typer.echo(f"Healthcheck:      {result.healthcheck_status}")
    typer.echo(f"Cleanup:          {result.cleanup_status}")
    if result.log_path:
        typer.echo(f"Report:           {result.log_path}")
    if result.message:
        typer.echo(f"Message:          {result.message}")
    if result.next_actions:
        typer.echo("\nNext actions:")
        for action in result.next_actions:
            typer.echo(f"  - {action}")

    if result.status != "success":
        raise typer.Exit(code=1)
