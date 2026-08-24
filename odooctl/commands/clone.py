"""Clone command — thin wrapper around the clone service."""
from __future__ import annotations

from odooctl.services.clone import run_clone
from odooctl.services.context import ServiceContext
from odooctl.operations.audit import AuditStore
from odooctl.operations.engine import run_operation
from odooctl.operations.models import OperationKind
from odooctl.operations.store import OperationStore
from odooctl.security.principals import local_actor


def execute(
    source: str,
    target: str,
    sanitize: bool | None = True,
    config_path: str = "odooctl.yml",
    sanitization_profile: str = "normal",
    preview: bool = False,
) -> str:
    ctx = ServiceContext.from_config_path(config_path)
    if preview:
        result = run_clone(
            ctx, source, target,
            sanitize=sanitize, sanitization_profile=sanitization_profile, preview=True,
        )
        return result.url
    store = OperationStore(ctx.project.state_dir)
    audit = AuditStore(ctx.project.state_dir)
    result = None
    with run_operation(
        store,
        audit,
        kind=OperationKind.CLONE,
        project=ctx.project.config.project.name,
        environment=target,
        actor=local_actor(),
        params_redacted={"source": source, "target": target, "profile": sanitization_profile},
        state_dir=ctx.project.state_dir,
    ) as op_ctx:
        op_ctx.emit(f"cloning {source} → {target}", phase="clone")
        result = run_clone(
            ctx, source, target,
            sanitize=sanitize, sanitization_profile=sanitization_profile, preview=False,
        )
        mechanisms = ", ".join(result.sanitization_mechanisms) or "none"
        op_ctx.emit(f"clone complete: {result.url} (sanitization: {mechanisms})", phase="clone")
    return result.url  # type: ignore[union-attr]
