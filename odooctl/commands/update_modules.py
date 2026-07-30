from __future__ import annotations

from pathlib import Path

from odooctl.adapters.docker_compose import DockerComposeAdapter
from odooctl.context import ProjectContext
from odooctl.odoo.module_update import update_modules_compose
from odooctl.operations.audit import AuditStore
from odooctl.operations.engine import run_operation
from odooctl.operations.models import OperationKind
from odooctl.operations.store import OperationStore


def _compose_adapter(compose_file: str, project_root: Path):
    try:
        return DockerComposeAdapter(compose_file, project_dir=str(project_root))
    except TypeError:
        return DockerComposeAdapter(compose_file)


def execute(environment: str, modules: list[str] | None = None, config_path: str = "odooctl.yml") -> None:
    context = ProjectContext.from_config_path(config_path)
    cfg = context.config
    env = cfg.env(environment)
    selected = modules if modules is not None else env.update_modules
    op_store = OperationStore(context.state_dir)
    audit = AuditStore(context.state_dir)
    with run_operation(
        op_store,
        audit,
        kind=OperationKind.UPDATE_MODULES,
        project=cfg.project.name,
        environment=environment,
        actor="cli",
        params_redacted={"environment": environment, "modules": selected},
        state_dir=context.state_dir,
    ) as op_ctx:
        op_ctx.emit(f"updating modules: {selected}", phase="update")
        compose = _compose_adapter(cfg.runtime.compose_file, context.root)
        update_modules_compose(
            compose,
            cfg.odoo.service,
            env.db_name,
            selected,
            cli_command=cfg.odoo.cli_command,
            db_host=cfg.odoo.db_host,
            db_user=cfg.odoo.db_user,
            db_password_env=cfg.odoo.db_password_env,
            config_path=cfg.odoo.config_path,
        )
        op_ctx.emit("module update complete", phase="update")
