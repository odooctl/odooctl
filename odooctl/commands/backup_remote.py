"""Remote portable-backup inspection, verification, and retrieval commands."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from odooctl.cli_selector import resolve_config_path

app = typer.Typer(help="List, byte-verify, and download S3-compatible portable backups.")


def _config_path(ctx: typer.Context, config: str) -> str:
    return str(resolve_config_path(ctx, config, normalize=False))


@app.command("list")
def list_backups(
    ctx: typer.Context,
    environment: str = typer.Argument(
        ...,
        help="Environment whose completed remote backups should be listed.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        "--json-output",
    ),
    config: str = "odooctl.yml",
) -> None:
    from odooctl.services.context import ServiceContext
    from odooctl.services.remote_backup import list_remote_backups

    service_ctx = ServiceContext.from_config_path(_config_path(ctx, config))
    backup_ids = list_remote_backups(service_ctx, environment)
    if json_output:
        typer.echo(json.dumps(backup_ids, indent=2))
        return
    if not backup_ids:
        typer.echo("No complete remote backups found.")
        return
    for backup_id in backup_ids:
        typer.echo(backup_id)


@app.command("verify")
def verify(
    ctx: typer.Context,
    environment: str = typer.Argument(
        ...,
        help="Environment whose remote backup should be byte-verified.",
    ),
    backup: str = typer.Option(
        "latest",
        "--backup",
        help="Exact backup id, or latest.",
    ),
    config: str = "odooctl.yml",
) -> None:
    from odooctl.operations.audit import AuditStore
    from odooctl.operations.engine import run_operation
    from odooctl.operations.models import OperationKind
    from odooctl.operations.store import OperationStore
    from odooctl.services.context import ServiceContext
    from odooctl.services.remote_backup import (
        prune_remote_backups,
        record_remote_retention_error,
        verify_remote_backup,
    )

    service_ctx = ServiceContext.from_config_path(_config_path(ctx, config))
    store = OperationStore(service_ctx.project.state_dir)
    audit = AuditStore(service_ctx.project.state_dir)
    with run_operation(
        store,
        audit,
        kind=OperationKind.BACKUP,
        project=service_ctx.project.config.project.name,
        environment=environment,
        actor="cli",
        params_redacted={
            "action": "remote_verify",
            "backup_id": backup,
        },
        state_dir=service_ctx.project.state_dir,
    ) as op_ctx:
        op_ctx.emit(
            f"verifying remote backup: {backup}",
            phase="remote_verify",
        )
        result = verify_remote_backup(
            service_ctx,
            environment,
            backup,
        )
        prune_result = prune_remote_backups(
            service_ctx,
            environment=environment,
            protected_backup_ids=(result.backup_id,),
        )
        if prune_result.error:
            backup_dir = service_ctx.project.backups_dir / result.backup_id
            if backup_dir.is_dir() and not backup_dir.is_symlink():
                record_remote_retention_error(
                    service_ctx,
                    backup_dir,
                    result.manifest,
                    prune_result.error,
                )
            raise RuntimeError(
                "Remote backup verified, but retention reconciliation failed: "
                f"{prune_result.error}"
            )
        op_ctx.emit(
            f"remote backup verified: {result.backup_id}",
            phase="remote_verify",
            data={
                "backup_id": result.backup_id,
                "remote_uri": result.uri,
                "object_count": result.object_count,
                "verified_at": result.verified_at,
                "pruned_backup_ids": list(
                    prune_result.deleted_backup_ids
                ),
            },
        )
    typer.echo(
        f"{result.backup_id}: verified {result.object_count} objects at {result.verified_at}"
    )


@app.command("download")
def download(
    ctx: typer.Context,
    environment: str = typer.Argument(
        ...,
        help="Environment whose remote backup should be downloaded.",
    ),
    backup: str = typer.Option(
        "latest",
        "--backup",
        help="Exact backup id, or latest.",
    ),
    destination: Path | None = typer.Option(
        None,
        "--destination",
        "-d",
        help="Destination root; defaults to the configured local backup root.",
    ),
    config: str = "odooctl.yml",
) -> None:
    from odooctl.operations.audit import AuditStore
    from odooctl.operations.engine import run_operation
    from odooctl.operations.models import OperationKind
    from odooctl.operations.store import OperationStore
    from odooctl.services.context import ServiceContext
    from odooctl.services.remote_backup import download_remote_backup

    service_ctx = ServiceContext.from_config_path(_config_path(ctx, config))
    destination_root = (
        service_ctx.project.resolve_path(destination) if destination is not None else None
    )
    store = OperationStore(service_ctx.project.state_dir)
    audit = AuditStore(service_ctx.project.state_dir)
    with run_operation(
        store,
        audit,
        kind=OperationKind.BACKUP,
        project=service_ctx.project.config.project.name,
        environment=environment,
        actor="cli",
        params_redacted={
            "action": "remote_download",
            "backup_id": backup,
            "destination": str(destination_root) if destination_root else None,
        },
        state_dir=service_ctx.project.state_dir,
    ) as op_ctx:
        op_ctx.emit(
            f"downloading remote backup: {backup}",
            phase="remote_download",
        )
        path = download_remote_backup(
            service_ctx,
            environment,
            backup,
            destination_root=destination_root,
        )
        op_ctx.emit(
            f"remote backup downloaded: {path.name}",
            phase="remote_download",
            data={
                "backup_id": path.name,
                "destination": str(path),
            },
        )
    typer.echo(str(path))
