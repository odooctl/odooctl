"""Restore command — thin wrapper around the restore service."""
from __future__ import annotations

# Re-export service utilities so callers that import from here continue to work.
from odooctl.services.restore import (  # noqa: F401
    resolve_backup_dir,
    sha256_file,
    validate_backup_dir,
    run_restore,
)
from odooctl.services.context import ServiceContext
from odooctl.operations.audit import AuditStore
from odooctl.operations.engine import run_operation
from odooctl.operations.models import OperationKind
from odooctl.operations.store import OperationStore
from odooctl.security.principals import local_actor


def execute_to(source_environment: str, target_environment: str, backup: str = "latest", config_path: str = "odooctl.yml") -> str:
    from odooctl.services.restore import restore_to_env
    ctx = ServiceContext.from_config_path(config_path)
    store = OperationStore(ctx.project.state_dir)
    audit = AuditStore(ctx.project.state_dir)
    result = None
    with run_operation(
        store,
        audit,
        kind=OperationKind.RESTORE,
        project=ctx.project.config.project.name,
        environment=target_environment,
        actor=local_actor(),
        params_redacted={"source": source_environment, "target": target_environment, "backup": backup},
        state_dir=ctx.project.state_dir,
    ) as op_ctx:
        op_ctx.emit(f"restoring {source_environment} backup {backup!r} into {target_environment}", phase="restore")
        result = restore_to_env(
            source_environment=source_environment,
            target_environment=target_environment,
            backup=backup,
            ctx=ctx,
        )
        op_ctx.emit(f"restore complete: {result.backup_id}", phase="restore")
    return result.backup_id  # type: ignore[union-attr]


def execute(environment: str, backup: str = "latest", config_path: str = "odooctl.yml") -> str:
    ctx = ServiceContext.from_config_path(config_path)
    store = OperationStore(ctx.project.state_dir)
    audit = AuditStore(ctx.project.state_dir)
    result = None
    with run_operation(
        store,
        audit,
        kind=OperationKind.RESTORE,
        project=ctx.project.config.project.name,
        environment=environment,
        actor=local_actor(),
        params_redacted={"environment": environment, "backup": backup},
        state_dir=ctx.project.state_dir,
    ) as op_ctx:
        op_ctx.emit(f"restoring {environment} from {backup}", phase="restore")
        result = run_restore(ctx, environment, backup)
        op_ctx.emit(f"restore complete: {result.backup_id}", phase="restore")
    return result.backup_id  # type: ignore[union-attr]
