"""Promote command — safe staging→production flow with backup and rollback."""
from __future__ import annotations

import typer

from odooctl.operations.audit import AuditStore
from odooctl.operations.engine import run_operation
from odooctl.operations.models import OperationKind
from odooctl.operations.store import OperationStore
from odooctl.services.context import ServiceContext
from odooctl.services.promote import promote_preview, run_promote
from odooctl.security.principals import local_actor

app = typer.Typer(help="Promote an environment to its configured target.", add_completion=False)


def execute(
    source: str,
    target: str,
    config: str = "odooctl.yml",
    preview: bool = False,
    yes: bool = False,
) -> None:
    ctx = ServiceContext.from_config_path(config)
    cfg = ctx.project.config

    if preview:
        result = promote_preview(ctx, source, target)
        typer.echo(f"Promote preview: {result.source} → {result.target} (no side effects)")
        return

    op_store = OperationStore(ctx.project.state_dir)
    audit = AuditStore(ctx.project.state_dir)
    with run_operation(
        op_store,
        audit,
        kind=OperationKind.PROMOTE,
        project=cfg.project.name,
        environment=target,
        actor=local_actor(),
        params_redacted={"source": source, "target": target},
        state_dir=ctx.project.state_dir,
    ) as op_ctx:
        op_ctx.emit(f"promote {source} → {target}", phase="promote")
        result = run_promote(ctx, source, target, confirm=yes)
        op_ctx.emit(
            f"promote complete: {source} → {target} backup={result.backup_id}",
            phase="promote",
        )

    typer.echo(f"Promoted {source} → {target} (backup: {result.backup_id})")
