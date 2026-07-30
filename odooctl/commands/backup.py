"""Backup command — thin wrapper around the backup service."""

from __future__ import annotations

import click

# Re-export service utilities so callers that import from here continue to work.
from odooctl.services.backup import (  # noqa: F401
    git_commit,
    prune_backups,
    redact_config_snapshot,
    run_backup,
)
from odooctl.services.context import ServiceContext
from odooctl.operations.audit import AuditStore
from odooctl.operations.engine import run_operation
from odooctl.operations.models import OperationKind
from odooctl.operations.store import OperationStore


def execute(environment: str, config_path: str = "odooctl.yml", *, verify: bool = False) -> str:
    ctx = ServiceContext.from_config_path(config_path)
    store = OperationStore(ctx.project.state_dir)
    audit = AuditStore(ctx.project.state_dir)
    result = None
    with run_operation(
        store,
        audit,
        kind=OperationKind.BACKUP,
        project=ctx.project.config.project.name,
        environment=environment,
        actor="cli",
        params_redacted={"environment": environment},
        state_dir=ctx.project.state_dir,
    ) as op_ctx:
        op_ctx.emit("starting backup", phase="backup")
        result = run_backup(ctx, environment)
        op_ctx.emit(
            f"backup complete: {result.backup_id}",
            phase="backup",
            data={
                "backup_id": result.backup_id,
                "remote_uri": result.remote_uri,
                "remote_status": result.remote_status,
            },
        )
        if result.remote_error:
            op_ctx.emit(
                f"remote backup warning: {result.remote_error}",
                phase="remote_backup",
                level="warning",
                data={"remote_status": result.remote_status},
            )
            click.echo(
                f"Warning: remote backup is {result.remote_status}: "
                f"{result.remote_error}",
                err=True,
            )
        if verify:
            from odooctl.services.backup import verify_backup

            verify_result = verify_backup(ctx.project.backups_dir, result.backup_id)
            if verify_result.ok:
                op_ctx.emit(f"backup verified: {result.backup_id}", phase="verify")
            else:
                op_ctx.emit(f"backup verification failed: {verify_result.error}", phase="verify")
                raise RuntimeError(
                    f"Backup verification failed for {result.backup_id}: "
                    f"{verify_result.error or 'unknown verification error'}"
                )
    return result.backup_id  # type: ignore[union-attr]
