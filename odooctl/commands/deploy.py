"""Deploy command — thin wrapper around the deploy service."""
from __future__ import annotations

from odooctl.services.context import ServiceContext
from odooctl.services.deploy import run_deploy
from odooctl.operations.audit import AuditStore
from odooctl.operations.engine import run_operation
from odooctl.operations.models import OperationKind
from odooctl.operations.store import OperationStore
from odooctl.security.principals import local_actor


def execute(environment: str, branch: str | None = None, config_path: str = "odooctl.yml") -> None:
    ctx = ServiceContext.from_config_path(config_path)
    store = OperationStore(ctx.project.state_dir)
    audit = AuditStore(ctx.project.state_dir)
    with run_operation(
        store,
        audit,
        kind=OperationKind.DEPLOY,
        project=ctx.project.config.project.name,
        environment=environment,
        actor=local_actor(),
        params_redacted={"environment": environment, "branch": branch},
        state_dir=ctx.project.state_dir,
    ) as op_ctx:
        op_ctx.emit(f"deploying {environment}", phase="deploy")
        result = run_deploy(ctx, environment, branch)
        op_ctx.emit(f"deploy complete: {result.status}", phase="deploy")
