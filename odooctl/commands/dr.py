"""Disaster recovery CLI commands."""
from __future__ import annotations

import json
import shlex

import typer

from odooctl.cli_selector import resolve_config_path

app = typer.Typer(help="Disaster recovery drills.")
snapshot_app = typer.Typer(
    help="Create, list, plan, and execute provider-native DR snapshots."
)
app.add_typer(snapshot_app, name="snapshot")


def _config_path(ctx: typer.Context, config: str) -> str:
    return str(resolve_config_path(ctx, config, normalize=False))


@app.command()
def drill(
    ctx: typer.Context,
    environment: str = typer.Argument(..., help="Source environment whose latest backup to drill."),
    config: str = "odooctl.yml",
) -> None:
    """Run a DR drill: restore the latest backup into a throwaway DB, healthcheck, then clean up."""
    from odooctl.services.context import ServiceContext
    from odooctl.services.dr import run_dr_drill
    from odooctl.adapters.db import make_db_adapter as make_context_db_adapter
    from odooctl.adapters.filestore import FilestoreAdapter

    service_ctx = ServiceContext.from_config_path(_config_path(ctx, config))
    cfg = service_ctx.project.config

    db_adapter = make_context_db_adapter(service_ctx.project)
    fs_adapter = FilestoreAdapter()

    def healthcheck_fn(url: str) -> bool:
        try:
            from odooctl.odoo.healthcheck import check_url
            check_url(url, timeout=cfg.healthcheck.timeout_seconds, retries=1, interval=1)
            return True
        except Exception:
            return False

    result = run_dr_drill(
        environment=environment,
        backups_root=service_ctx.project.backups_dir,
        db_adapter=db_adapter,
        fs_adapter=fs_adapter,
        healthcheck_fn=healthcheck_fn,
        is_protected_fn=cfg.is_protected,
    )

    typer.echo(f"DR drill for {environment!r}: {result.status}")
    if result.backup_id:
        typer.echo(f"Backup used: {result.backup_id}")
    if result.message:
        typer.echo(f"Note: {result.message}")

    if result.status != "success":
        raise typer.Exit(code=1)


@snapshot_app.command("create")
def snapshot_create(
    ctx: typer.Context,
    environment: str = typer.Argument(..., help="Environment represented by the snapshot."),
    config: str = "odooctl.yml",
) -> None:
    """Request and durably track a provider-native DR snapshot."""
    from odooctl.operations.audit import AuditStore
    from odooctl.operations.engine import run_operation
    from odooctl.operations.models import OperationKind
    from odooctl.operations.store import OperationStore
    from odooctl.services.context import ServiceContext
    from odooctl.services.snapshots import run_snapshot_create

    service_ctx = ServiceContext.from_config_path(_config_path(ctx, config))
    store = OperationStore(service_ctx.project.state_dir)
    audit = AuditStore(service_ctx.project.state_dir)
    with run_operation(
        store,
        audit,
        kind=OperationKind.SNAPSHOT_CREATE,
        project=service_ctx.project.config.project.name,
        environment=environment,
        actor="cli",
        params_redacted={"environment": environment, "trigger": "explicit"},
        state_dir=service_ctx.project.state_dir,
    ) as op_ctx:
        op_ctx.emit("creating provider DR snapshot", phase="snapshot")
        manifest = run_snapshot_create(service_ctx, environment)
        op_ctx.emit(
            f"snapshot {manifest.status}: {manifest.snapshot_id}",
            phase="snapshot",
            data={
                "snapshot_id": manifest.snapshot_id,
                "provider": manifest.provider,
                "status": manifest.status,
                "source_resource_id": manifest.source_resource_id,
            },
        )

    typer.echo(manifest.snapshot_id)
    typer.echo(
        f"{manifest.provider}: {len(manifest.resources)} resource(s), "
        f"{manifest.consistency.replace('_', ' ')}, {manifest.status}"
    )


@snapshot_app.command("list")
def snapshot_list(
    ctx: typer.Context,
    environment: str | None = typer.Option(
        None,
        "--environment",
        "--env",
        help="Only list snapshots for one environment.",
    ),
    json_output: bool = typer.Option(False, "--json", "--json-output"),
    config: str = "odooctl.yml",
) -> None:
    """List odooctl snapshot manifests (separate from portable backups)."""
    from odooctl.services.context import ServiceContext
    from odooctl.services.snapshots import list_snapshots

    service_ctx = ServiceContext.from_config_path(_config_path(ctx, config))
    manifests = list_snapshots(service_ctx, environment)
    if json_output:
        typer.echo(
            json.dumps(
                [manifest.model_dump(mode="json") for manifest in manifests],
                indent=2,
            )
        )
        return
    if not manifests:
        typer.echo("No snapshot manifests found.")
        return
    for manifest in manifests:
        typer.echo(
            f"{manifest.snapshot_id}\t{manifest.environment}\t"
            f"{manifest.provider}\t{manifest.status}\t"
            f"{manifest.consistency}\t{manifest.timestamp}"
        )


@snapshot_app.command("reconcile")
def snapshot_reconcile(
    ctx: typer.Context,
    snapshot_id: str = typer.Argument(
        ...,
        help="Exact odooctl snapshot manifest id.",
    ),
    config: str = "odooctl.yml",
) -> None:
    """Refresh provider state for a requested, pending, or completed snapshot."""
    from odooctl.operations.audit import AuditStore
    from odooctl.operations.engine import run_operation
    from odooctl.operations.models import OperationKind
    from odooctl.operations.store import OperationStore
    from odooctl.services.context import ServiceContext
    from odooctl.services.snapshots import get_snapshot, run_snapshot_reconcile

    service_ctx = ServiceContext.from_config_path(_config_path(ctx, config))
    existing = get_snapshot(service_ctx, snapshot_id)
    store = OperationStore(service_ctx.project.state_dir)
    audit = AuditStore(service_ctx.project.state_dir)
    with run_operation(
        store,
        audit,
        kind=OperationKind.SNAPSHOT_RECONCILE,
        project=service_ctx.project.config.project.name,
        environment=existing.environment,
        actor="cli",
        params_redacted={"snapshot_id": snapshot_id},
        state_dir=service_ctx.project.state_dir,
    ) as op_ctx:
        op_ctx.emit("reconciling provider snapshot state", phase="snapshot")
        manifest = run_snapshot_reconcile(service_ctx, snapshot_id)
        op_ctx.emit(
            f"snapshot {manifest.status}: {manifest.snapshot_id}",
            phase="snapshot",
            data={
                "snapshot_id": manifest.snapshot_id,
                "status": manifest.status,
                "source_resource_id": manifest.source_resource_id,
            },
        )
    typer.echo(
        f"{manifest.snapshot_id}: {manifest.status} "
        f"({len(manifest.resources)} resource(s))"
    )


def _print_restore_plan(plan) -> None:
    typer.echo(
        f"Snapshot {plan.snapshot_id} ({plan.provider}) targets source resource "
        f"{plan.source_resource_id}."
    )
    typer.echo("Provider recovery commands:")
    for command in plan.commands:
        typer.echo(f"  {shlex.join(command)}")
    for note in plan.notes:
        typer.echo(f"  Note: {note}")
    if plan.destructive:
        typer.secho(
            "This provider plan is destructive.",
            fg=typer.colors.RED,
            bold=True,
        )


@snapshot_app.command("restore")
def snapshot_restore(
    ctx: typer.Context,
    snapshot_id: str = typer.Argument(..., help="Exact odooctl snapshot manifest id."),
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Execute the provider recovery commands; otherwise only print the plan.",
    ),
    confirm_snapshot: str | None = typer.Option(
        None,
        "--confirm-snapshot",
        help="Exact snapshot id; prompted when omitted with --execute.",
    ),
    confirm_resource: str | None = typer.Option(
        None,
        "--confirm-resource",
        help="Exact source resource id; prompted when omitted with --execute.",
    ),
    config: str = "odooctl.yml",
) -> None:
    """Plan or explicitly execute provider recovery.

    ``--execute`` has no ``--yes`` bypass: both the snapshot id and source
    resource id must be typed exactly, interactively or through the two
    confirmation options.
    """
    from odooctl.operations.audit import AuditStore
    from odooctl.operations.engine import run_operation
    from odooctl.operations.models import OperationKind
    from odooctl.operations.store import OperationStore
    from odooctl.services.context import ServiceContext
    from odooctl.services.snapshots import (
        get_snapshot,
        plan_snapshot_restore,
        run_snapshot_restore,
    )

    service_ctx = ServiceContext.from_config_path(_config_path(ctx, config))
    initial = get_snapshot(service_ctx, snapshot_id)
    store = OperationStore(service_ctx.project.state_dir)
    audit = AuditStore(service_ctx.project.state_dir)
    with run_operation(
        store,
        audit,
        kind=OperationKind.SNAPSHOT_RESTORE,
        project=service_ctx.project.config.project.name,
        environment=initial.environment,
        actor="cli",
        params_redacted={
            "snapshot_id": snapshot_id,
            "source_resource_id": initial.source_resource_id,
            "execute": execute,
        },
        state_dir=service_ctx.project.state_dir,
    ) as op_ctx:
        manifest, plan = plan_snapshot_restore(service_ctx, snapshot_id)
        _print_restore_plan(plan)
        if execute:
            if confirm_snapshot is None:
                confirm_snapshot = typer.prompt("Type the exact snapshot id")
            if confirm_resource is None:
                confirm_resource = typer.prompt(
                    "Type the exact source resource id"
                )
        op_ctx.emit(
            "executing snapshot recovery" if execute else "planning snapshot recovery",
            phase="snapshot_restore",
            data={
                "snapshot_id": manifest.snapshot_id,
                "source_resource_id": manifest.source_resource_id,
            },
        )
        outcome = run_snapshot_restore(
            service_ctx,
            snapshot_id,
            execute=execute,
            confirm_snapshot_id=confirm_snapshot,
            confirm_resource_id=confirm_resource,
            prepared=(manifest, plan),
        )
        op_ctx.emit(
            (
                f"snapshot recovery {outcome.status}"
                if outcome.executed
                else "snapshot recovery planned"
            ),
            phase="snapshot_restore",
            data={
                "source_resource_id": manifest.source_resource_id,
                "status": outcome.status,
                "message": outcome.message,
                "restored_resource_ids": list(outcome.restored_resource_ids),
            },
        )

    typer.echo(outcome.message or "Snapshot recovery operation complete.")
    typer.echo(f"Restore status: {outcome.status}")
    if outcome.restored_resource_ids:
        typer.echo(
            "Restored provider resources: "
            + ", ".join(outcome.restored_resource_ids)
        )
