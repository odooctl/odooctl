"""Sync command — pull-based auto-deploy (drift check, deploy when behind)."""
from __future__ import annotations

import json
from dataclasses import asdict

import typer

from odooctl.operations.audit import AuditStore
from odooctl.operations.engine import run_operation
from odooctl.operations.models import OperationKind
from odooctl.operations.store import OperationStore
from odooctl.services.context import ServiceContext
from odooctl.services.deploy import run_deploy
from odooctl.services.sync import ATTENTION_STATUSES, run_sync


def execute(
    environment: str,
    config_path: str = "odooctl.yml",
    *,
    force: bool = False,
    json_output: bool = False,
) -> None:
    ctx = ServiceContext.from_config_path(config_path)

    def deploy_with_operation(svc_ctx: ServiceContext, env_name: str):
        store = OperationStore(ctx.project.state_dir)
        audit = AuditStore(ctx.project.state_dir)
        with run_operation(
            store,
            audit,
            kind=OperationKind.DEPLOY,
            project=ctx.project.config.project.name,
            environment=env_name,
            actor="sync",
            params_redacted={"environment": env_name, "trigger": "sync"},
            state_dir=ctx.project.state_dir,
        ) as op_ctx:
            op_ctx.emit(f"sync deploying {env_name}", phase="deploy")
            result = run_deploy(svc_ctx, env_name)
            op_ctx.emit(f"deploy complete: {result.status}", phase="deploy")
            return result

    outcome = run_sync(ctx, environment, force=force, deploy=deploy_with_operation)

    if json_output:
        typer.echo(json.dumps(asdict(outcome), indent=2))
    else:
        typer.echo(f"[sync] {environment}: {outcome.status} — {outcome.message}")
    if outcome.status in ATTENTION_STATUSES:
        raise typer.Exit(code=1)
