"""Object-storage filestore inspection, migration, verification, and cutover."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import typer

from odooctl.cli_selector import resolve_config_path


app = typer.Typer(
    help="Manage local, volume, mirrored, POSIX-mounted, and module filestores."
)
migrate_app = typer.Typer(
    help="Plan, sync, verify, and explicitly cut over filestore migrations."
)
app.add_typer(migrate_app, name="migrate")


def _service_context(ctx: typer.Context, config: str):
    from odooctl.services.context import ServiceContext

    path = resolve_config_path(ctx, config, normalize=False)
    return ServiceContext.from_config_path(str(path))


def _migration_operation(service_ctx, environment: str, params: dict):
    from odooctl.operations.audit import AuditStore
    from odooctl.operations.engine import run_operation
    from odooctl.operations.models import OperationKind
    from odooctl.operations.store import OperationStore

    return run_operation(
        OperationStore(service_ctx.project.state_dir),
        AuditStore(service_ctx.project.state_dir),
        kind=OperationKind.FILESTORE_MIGRATE,
        project=service_ctx.project.config.project.name,
        environment=environment,
        actor="cli",
        params_redacted=params,
        state_dir=service_ctx.project.state_dir,
    )


@app.command("status")
def status(
    ctx: typer.Context,
    environment: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", "--json-output"),
    config: str = "odooctl.yml",
) -> None:
    """Show the effective backend and latest migration state."""

    from odooctl.services.filestore_storage import backend_status

    result = backend_status(
        _service_context(ctx, config),
        environment,
    )
    if json_output:
        typer.echo(json.dumps(asdict(result), indent=2))
        return
    typer.echo(f"{result.environment}: {result.backend}")
    typer.echo(f"Source: {result.source_location}")
    if result.target_location:
        typer.echo(f"Target: {result.target_location}")
    if result.latest_migration_id:
        typer.echo(
            f"Latest: {result.latest_migration_id} "
            f"({result.latest_status})"
        )


@migrate_app.command("plan")
def migrate_plan(
    ctx: typer.Context,
    environment: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", "--json-output"),
    config: str = "odooctl.yml",
) -> None:
    """Inventory the source and record an immutable migration plan."""

    from odooctl.services.filestore_storage import plan_migration

    service_ctx = _service_context(ctx, config)
    with _migration_operation(
        service_ctx,
        environment,
        {"action": "plan", "environment": environment},
    ) as op_ctx:
        manifest = plan_migration(service_ctx, environment)
        op_ctx.emit(
            f"filestore migration planned: {manifest.migration_id}",
            phase="filestore_plan",
            data={
                "migration_id": manifest.migration_id,
                "inventory_sha256": manifest.inventory_sha256,
                "object_count": len(manifest.entries),
                "total_size": manifest.total_size,
            },
        )
    if json_output:
        typer.echo(manifest.model_dump_json(indent=2))
        return
    typer.echo(manifest.migration_id)
    typer.echo(
        f"{len(manifest.entries)} file(s), {manifest.total_size} byte(s), "
        f"inventory {manifest.inventory_sha256}"
    )


@migrate_app.command("sync")
def migrate_sync(
    ctx: typer.Context,
    environment: str = typer.Argument(...),
    migration_id: str = typer.Option(..., "--migration"),
    config: str = "odooctl.yml",
) -> None:
    """Upload/copy the exact planned inventory without deleting its source."""

    from odooctl.services.filestore_storage import sync_migration

    service_ctx = _service_context(ctx, config)
    with _migration_operation(
        service_ctx,
        environment,
        {
            "action": "sync",
            "environment": environment,
            "migration_id": migration_id,
        },
    ) as op_ctx:
        manifest = sync_migration(
            service_ctx,
            environment,
            migration_id,
        )
        op_ctx.emit(
            f"filestore migration synced: {manifest.migration_id}",
            phase="filestore_sync",
            data={
                "migration_id": manifest.migration_id,
                "inventory_sha256": manifest.inventory_sha256,
            },
        )
    typer.echo(
        f"{manifest.migration_id}: synced; source retained at "
        f"{manifest.source_location}"
    )


@migrate_app.command("verify")
def migrate_verify(
    ctx: typer.Context,
    environment: str = typer.Argument(...),
    migration_id: str = typer.Option(..., "--migration"),
    json_output: bool = typer.Option(False, "--json", "--json-output"),
    config: str = "odooctl.yml",
) -> None:
    """Read and checksum every target object/file against the plan."""

    from odooctl.services.filestore_storage import verify_migration

    service_ctx = _service_context(ctx, config)
    with _migration_operation(
        service_ctx,
        environment,
        {
            "action": "verify",
            "environment": environment,
            "migration_id": migration_id,
        },
    ) as op_ctx:
        result = verify_migration(
            service_ctx,
            environment,
            migration_id,
        )
        op_ctx.emit(
            f"filestore migration verified: {result.migration_id}",
            phase="filestore_verify",
            data={
                "migration_id": result.migration_id,
                "inventory_sha256": result.inventory_sha256,
                "object_count": result.object_count,
                "total_size": result.total_size,
            },
        )
    if json_output:
        typer.echo(json.dumps(asdict(result), indent=2))
        return
    typer.echo(
        f"{result.migration_id}: verified {result.object_count} file(s), "
        f"{result.total_size} byte(s)"
    )


@migrate_app.command("cutover")
def migrate_cutover(
    ctx: typer.Context,
    environment: str = typer.Argument(...),
    migration_id: str = typer.Option(..., "--migration"),
    confirm_environment: str = typer.Option(
        ...,
        "--confirm-environment",
    ),
    confirm_source_retained: bool = typer.Option(
        False,
        "--confirm-source-retained",
        help="Acknowledge that cutover does not delete the source.",
    ),
    config: str = "odooctl.yml",
) -> None:
    """Publish the verified target as active while retaining the source."""

    from odooctl.services.filestore_storage import cutover_migration

    service_ctx = _service_context(ctx, config)
    params = {
        "action": "cutover",
        "environment": environment,
        "migration_id": migration_id,
        "confirm_environment": confirm_environment,
        "confirm_source_retained": confirm_source_retained,
    }
    with _migration_operation(
        service_ctx,
        environment,
        params,
    ) as op_ctx:
        manifest = cutover_migration(
            service_ctx,
            environment,
            migration_id,
            confirm_environment=confirm_environment,
            confirm_source_retained=confirm_source_retained,
        )
        op_ctx.emit(
            f"filestore cutover complete: {manifest.migration_id}",
            phase="filestore_cutover",
            data={
                "migration_id": manifest.migration_id,
                "source_retained": not manifest.source_deleted,
            },
        )
    typer.echo(
        f"{manifest.migration_id}: cutover recorded; source retained at "
        f"{manifest.source_location}"
    )


@migrate_app.command("download")
def migrate_download(
    ctx: typer.Context,
    environment: str = typer.Argument(...),
    migration_id: str = typer.Option(..., "--migration"),
    destination: Path = typer.Option(..., "--destination"),
    config: str = "odooctl.yml",
) -> None:
    """Download/copy a verified generation into a new local directory."""

    from odooctl.services.filestore_storage import download_migration

    service_ctx = _service_context(ctx, config)
    with _migration_operation(
        service_ctx,
        environment,
        {
            "action": "download",
            "environment": environment,
            "migration_id": migration_id,
            "destination": str(destination),
        },
    ) as op_ctx:
        target = download_migration(
            service_ctx,
            environment,
            migration_id,
            destination,
        )
        op_ctx.emit(
            f"filestore migration downloaded: {migration_id}",
            phase="filestore_download",
            data={
                "migration_id": migration_id,
                "destination": str(target),
            },
        )
    typer.echo(str(target))


@migrate_app.command("delete-source")
def migrate_delete_source(
    ctx: typer.Context,
    environment: str = typer.Argument(...),
    migration_id: str = typer.Option(..., "--migration"),
    confirm_environment: str = typer.Option(
        ...,
        "--confirm-environment",
    ),
    confirm_migration_id: str = typer.Option(
        ...,
        "--confirm-migration",
    ),
    delete_source: bool = typer.Option(False, "--delete-source"),
    config: str = "odooctl.yml",
) -> None:
    """Delete the unchanged source only after explicit verified cutover."""

    from odooctl.services.filestore_storage import (
        delete_source_after_cutover,
    )

    service_ctx = _service_context(ctx, config)
    params = {
        "action": "delete_source",
        "environment": environment,
        "migration_id": migration_id,
        "confirm_environment": confirm_environment,
        "confirm_migration_id": confirm_migration_id,
        "delete_source": delete_source,
    }
    with _migration_operation(
        service_ctx,
        environment,
        params,
    ) as op_ctx:
        manifest = delete_source_after_cutover(
            service_ctx,
            environment,
            migration_id,
            confirm_environment=confirm_environment,
            confirm_migration_id=confirm_migration_id,
            delete_source=delete_source,
        )
        op_ctx.emit(
            f"filestore source deleted: {manifest.migration_id}",
            phase="filestore_delete_source",
            data={"migration_id": manifest.migration_id},
        )
    typer.echo(
        f"{manifest.migration_id}: source deleted at "
        f"{manifest.source_deleted_at}"
    )


@migrate_app.command("delete-remote-marker")
def migrate_delete_remote_marker(
    ctx: typer.Context,
    environment: str = typer.Argument(...),
    migration_id: str = typer.Option(..., "--migration"),
    confirm_migration_id: str = typer.Option(
        ...,
        "--confirm-migration",
    ),
    config: str = "odooctl.yml",
) -> None:
    """Delete one inactive migration marker; content objects remain."""

    from odooctl.services.filestore_storage import (
        delete_inactive_remote_marker,
    )

    service_ctx = _service_context(ctx, config)
    with _migration_operation(
        service_ctx,
        environment,
        {
            "action": "delete_remote_marker",
            "environment": environment,
            "migration_id": migration_id,
            "confirm_migration_id": confirm_migration_id,
        },
    ) as op_ctx:
        key = delete_inactive_remote_marker(
            service_ctx,
            environment,
            migration_id,
            confirm_migration_id=confirm_migration_id,
        )
        op_ctx.emit(
            f"filestore remote marker deleted: {migration_id}",
            phase="filestore_delete_remote_marker",
            data={
                "migration_id": migration_id,
                "key": key,
            },
        )
    typer.echo(f"Deleted inactive remote marker: {key}")
