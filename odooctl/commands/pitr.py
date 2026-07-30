"""PostgreSQL WAL archiving and point-in-time recovery commands."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from odooctl.cli_selector import resolve_config_path

app = typer.Typer(
    help=(
        "Archive PostgreSQL WAL and recover a database to a verified "
        "point in time."
    )
)
wal_app = typer.Typer(help="Internal PostgreSQL archive-command helpers.")
base_app = typer.Typer(help="Create, list, and verify physical base backups.")
restore_app = typer.Typer(
    help="Plan, execute, and explicitly cut over point-in-time recovery."
)
retention_app = typer.Typer(help="Reconcile the recoverable base/WAL graph.")
lease_app = typer.Typer(help="Inspect or recover cross-host PITR coordination.")
app.add_typer(wal_app, name="wal")
app.add_typer(base_app, name="base")
app.add_typer(restore_app, name="restore")
app.add_typer(retention_app, name="retention")
app.add_typer(lease_app, name="lease")


def _config_path(ctx: typer.Context, config: str) -> str:
    return str(resolve_config_path(ctx, config, normalize=False))


def _service_context(ctx: typer.Context, config: str):
    from odooctl.services.context import ServiceContext

    return ServiceContext.from_config_path(_config_path(ctx, config))


def _operation_stores(service_ctx):
    from odooctl.operations.audit import AuditStore
    from odooctl.operations.store import OperationStore

    state_dir = service_ctx.project.state_dir
    return OperationStore(state_dir), AuditStore(state_dir)


@app.command("check")
def check(
    ctx: typer.Context,
    environment: str = typer.Argument(...),
    json_output: bool = typer.Option(
        False,
        "--json",
        "--json-output",
    ),
    config: str = "odooctl.yml",
) -> None:
    """Validate PostgreSQL prerequisites and the independent archive."""

    from dataclasses import asdict

    from odooctl.services.pitr import check_pitr

    result = check_pitr(
        _service_context(ctx, config),
        environment,
    )
    if json_output:
        typer.echo(json.dumps(asdict(result), indent=2))
        return
    typer.echo(
        f"{environment}: PostgreSQL {result.postgres_major}, "
        f"system {result.system_identifier}, timeline {result.timeline}"
    )
    typer.echo(
        f"Archive mode {result.archive_mode}; "
        f"{result.remote_wal_objects} remote WAL object(s)"
    )
    typer.echo(f"Recovery image: {result.recovery_image}")


@app.command("archive-config")
def archive_config(
    ctx: typer.Context,
    environment: str = typer.Argument(...),
    odooctl_bin: str = typer.Option(
        "odooctl",
        "--odooctl-bin",
    ),
    config: str = "odooctl.yml",
) -> None:
    """Print a secret-free ``archive_command`` for PostgreSQL."""

    from odooctl.services.pitr import archive_command

    command = archive_command(
        _service_context(ctx, config),
        environment,
        odooctl_bin=odooctl_bin,
    )
    escaped_command = command.replace("'", "''")
    typer.echo(f"archive_command = '{escaped_command}'")


@wal_app.command("push", hidden=True)
def wal_push(
    ctx: typer.Context,
    environment: str = typer.Argument(...),
    path: Path = typer.Option(..., "--path"),
    name: str = typer.Option(..., "--name"),
    config: str = "odooctl.yml",
) -> None:
    """Archive one immutable WAL/history file; nonzero means retry."""

    from odooctl.services.pitr import archive_wal

    receipt = archive_wal(
        _service_context(ctx, config),
        environment,
        path,
        name,
    )
    typer.echo(
        f"{receipt.filename}\t{receipt.size}\t{receipt.sha256}"
    )


@base_app.command("create")
def base_create(
    ctx: typer.Context,
    environment: str = typer.Argument(...),
    config: str = "odooctl.yml",
) -> None:
    """Create and remotely verify a physical PostgreSQL base backup."""

    from odooctl.operations.engine import run_operation
    from odooctl.operations.models import OperationKind
    from odooctl.services.pitr import create_base_backup

    service_ctx = _service_context(ctx, config)
    store, audit = _operation_stores(service_ctx)
    with run_operation(
        store,
        audit,
        kind=OperationKind.PITR_BASE_CREATE,
        project=service_ctx.project.config.project.name,
        environment=environment,
        actor="cli",
        params_redacted={
            "environment": environment,
            "filestore_consistency": "not_included",
        },
        state_dir=service_ctx.project.state_dir,
    ) as op_ctx:
        op_ctx.emit(
            "creating verified physical base backup",
            phase="pitr_base",
        )
        manifest = create_base_backup(service_ctx, environment)
        op_ctx.emit(
            f"PITR base complete: {manifest.base_backup_id}",
            phase="pitr_base",
            data={
                "base_backup_id": manifest.base_backup_id,
                "system_identifier": manifest.system_identifier,
                "end_wal": manifest.end_wal,
                "remote_uri": manifest.remote_uri,
            },
        )
    typer.echo(manifest.base_backup_id)


@base_app.command("list")
def base_list(
    ctx: typer.Context,
    environment: str = typer.Argument(...),
    json_output: bool = typer.Option(
        False,
        "--json",
        "--json-output",
    ),
    config: str = "odooctl.yml",
) -> None:
    """List completed base backups from the PITR archive."""

    from odooctl.services.pitr import list_base_backups

    manifests = list_base_backups(
        _service_context(ctx, config),
        environment,
    )
    if json_output:
        typer.echo(
            json.dumps(
                [
                    manifest.model_dump(mode="json")
                    for manifest in manifests
                ],
                indent=2,
            )
        )
        return
    if not manifests:
        typer.echo("No complete PITR base backups found.")
        return
    for manifest in manifests:
        typer.echo(
            f"{manifest.base_backup_id}\t{manifest.completed_at}\t"
            f"{manifest.end_wal}\t{manifest.postgres_image}"
        )


@base_app.command("verify")
def base_verify(
    ctx: typer.Context,
    environment: str = typer.Argument(...),
    base_backup_id: str = typer.Option(..., "--base"),
    config: str = "odooctl.yml",
) -> None:
    """Byte-verify a completed physical base backup."""

    from odooctl.services.pitr import verify_base_backup

    manifest = verify_base_backup(
        _service_context(ctx, config),
        environment,
        base_backup_id,
    )
    typer.echo(
        f"{manifest.base_backup_id}: verified at "
        f"{manifest.verified_at}"
    )


@restore_app.command("plan")
def restore_plan(
    ctx: typer.Context,
    environment: str = typer.Argument(...),
    target_time: str = typer.Option(..., "--target-time"),
    timeline: int | None = typer.Option(None, "--timeline"),
    base_backup_id: str | None = typer.Option(None, "--base"),
    json_output: bool = typer.Option(
        False,
        "--json",
        "--json-output",
    ),
    config: str = "odooctl.yml",
) -> None:
    """Create a verified, non-destructive recovery plan."""

    from odooctl.services.pitr import plan_restore

    plan = plan_restore(
        _service_context(ctx, config),
        environment,
        target_time,
        timeline=timeline,
        base_backup_id=base_backup_id,
    )
    if json_output:
        typer.echo(plan.model_dump_json(indent=2))
        return
    typer.echo(plan.plan_id)
    typer.echo(
        f"Base {plan.base_backup_id}; WAL {plan.first_wal}.."
        f"{plan.last_wal} ({plan.wal_count} segment(s))"
    )
    typer.echo(
        f"Recovery will create {plan.new_database!r}; "
        "the live database and filestore remain unchanged."
    )


@restore_app.command("execute")
def restore_execute(
    ctx: typer.Context,
    environment: str = typer.Argument(...),
    plan_id: str = typer.Option(..., "--plan"),
    config: str = "odooctl.yml",
) -> None:
    """Recover in isolation and restore the verified dump to a new DB."""

    from odooctl.operations.engine import run_operation
    from odooctl.operations.models import OperationKind
    from odooctl.services.pitr import execute_restore

    service_ctx = _service_context(ctx, config)
    store, audit = _operation_stores(service_ctx)
    with run_operation(
        store,
        audit,
        kind=OperationKind.PITR_RESTORE,
        project=service_ctx.project.config.project.name,
        environment=environment,
        actor="cli",
        params_redacted={
            "plan_id": plan_id,
            "database_only": True,
        },
        state_dir=service_ctx.project.state_dir,
    ) as op_ctx:
        op_ctx.emit(
            f"executing isolated PITR plan {plan_id}",
            phase="pitr_restore",
        )
        metadata = execute_restore(
            service_ctx,
            environment,
            plan_id,
        )
        op_ctx.emit(
            f"PITR restore verified: {metadata.restore_id}",
            phase="pitr_restore",
            data={
                "restore_id": metadata.restore_id,
                "new_database": metadata.new_database,
                "target_time": metadata.target_time,
                "recovered_lsn": metadata.recovered_lsn,
            },
        )
    typer.echo(metadata.restore_id)
    typer.echo(
        f"Verified new database: {metadata.new_database}. "
        "No cutover was performed."
    )


@restore_app.command("cutover")
def restore_cutover(
    ctx: typer.Context,
    environment: str = typer.Argument(...),
    restore_id: str = typer.Option(..., "--restore"),
    confirm_environment: str = typer.Option(
        ...,
        "--confirm-environment",
    ),
    confirm_database: str = typer.Option(
        ...,
        "--confirm-database",
    ),
    accept_database_only: bool = typer.Option(
        False,
        "--accept-database-only",
        help=(
            "Acknowledge that WAL recovery does not roll the Odoo "
            "filestore back."
        ),
    ),
    config: str = "odooctl.yml",
) -> None:
    """Atomically swap an already verified new database into place."""

    from odooctl.operations.engine import run_operation
    from odooctl.operations.models import OperationKind
    from odooctl.services.pitr import cutover_restore

    service_ctx = _service_context(ctx, config)
    store, audit = _operation_stores(service_ctx)
    with run_operation(
        store,
        audit,
        kind=OperationKind.PITR_CUTOVER,
        project=service_ctx.project.config.project.name,
        environment=environment,
        actor="cli",
        params_redacted={
            "restore_id": restore_id,
            "confirm_environment": confirm_environment,
            "confirm_database": confirm_database,
            "accept_database_only": accept_database_only,
        },
        state_dir=service_ctx.project.state_dir,
    ) as op_ctx:
        op_ctx.emit(
            f"cutting over verified PITR restore {restore_id}",
            phase="pitr_cutover",
        )
        metadata = cutover_restore(
            service_ctx,
            environment,
            restore_id,
            confirm_environment=confirm_environment,
            confirm_database=confirm_database,
            accept_database_only=accept_database_only,
        )
        op_ctx.emit(
            f"PITR cutover complete: {metadata.restore_id}",
            phase="pitr_cutover",
            data={
                "restore_id": metadata.restore_id,
                "database": metadata.database,
                "filestore_consistency": (
                    metadata.filestore_consistency
                ),
            },
        )
    typer.echo(
        f"{metadata.restore_id}: database cutover complete; "
        "filestore was not changed"
    )


@retention_app.command("reconcile")
def retention_reconcile(
    ctx: typer.Context,
    environment: str = typer.Argument(...),
    config: str = "odooctl.yml",
) -> None:
    """Prune only bases/WAL outside the retained recovery graph."""

    from odooctl.operations.engine import run_operation
    from odooctl.operations.models import OperationKind
    from odooctl.services.pitr import reconcile_retention

    service_ctx = _service_context(ctx, config)
    store, audit = _operation_stores(service_ctx)
    with run_operation(
        store,
        audit,
        kind=OperationKind.PITR_RECONCILE,
        project=service_ctx.project.config.project.name,
        environment=environment,
        actor="cli",
        params_redacted={"environment": environment},
        state_dir=service_ctx.project.state_dir,
    ) as op_ctx:
        result = reconcile_retention(service_ctx, environment)
        op_ctx.emit(
            "PITR retention reconciliation complete",
            phase="pitr_retention",
            data={
                "retained_base_backup_ids": list(
                    result.retained_base_backup_ids
                ),
                "deleted_base_backup_ids": list(
                    result.deleted_base_backup_ids
                ),
                "deleted_wal_filenames": list(
                    result.deleted_wal_filenames
                ),
            },
        )
    typer.echo(
        f"Retained {len(result.retained_base_backup_ids)} base(s); "
        f"deleted {len(result.deleted_base_backup_ids)} base(s) and "
        f"{len(result.deleted_wal_filenames)} WAL object(s)"
    )


@lease_app.command("inspect")
def lease_inspect(
    ctx: typer.Context,
    environment: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", "--json-output"),
    config: str = "odooctl.yml",
) -> None:
    """Inspect the remote PITR lease without changing it."""

    from dataclasses import asdict

    from odooctl.services.pitr import inspect_coordination_lease

    lease = inspect_coordination_lease(
        _service_context(ctx, config),
        environment,
    )
    if lease is None:
        typer.echo("No PITR coordination lease.")
        return
    if json_output:
        typer.echo(json.dumps(asdict(lease), indent=2, default=str))
        return
    state = "expired" if lease.expired else "active"
    typer.echo(
        f"{lease.lease_id}\t{state}\t{lease.owner}\t"
        f"{lease.purpose}\t{lease.expires_at.isoformat()}"
    )


@lease_app.command("recover-expired")
def lease_recover_expired(
    ctx: typer.Context,
    environment: str = typer.Argument(...),
    confirm_lease_id: str = typer.Option(..., "--confirm-lease-id"),
    confirm_owner: str = typer.Option(..., "--confirm-owner"),
    confirm_purpose: str = typer.Option(..., "--confirm-purpose"),
    confirm_owner_stopped: str = typer.Option(
        ...,
        "--confirm-owner-stopped",
        help="Exact attestation OWNER_STOPPED:<lease-id>.",
    ),
    config: str = "odooctl.yml",
) -> None:
    """CAS-remove one expired lease after proving its owner stopped."""

    from odooctl.services.pitr import recover_expired_coordination_lease

    lease = recover_expired_coordination_lease(
        _service_context(ctx, config),
        environment,
        confirm_lease_id=confirm_lease_id,
        confirm_owner=confirm_owner,
        confirm_purpose=confirm_purpose,
        confirm_owner_stopped=confirm_owner_stopped,
    )
    typer.echo(f"Recovered expired PITR lease {lease.lease_id}.")
