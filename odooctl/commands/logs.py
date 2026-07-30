from __future__ import annotations

from odooctl.adapters.docker_compose import DockerComposeAdapter
from odooctl.adapters.runtime import make_runtime_adapter
from odooctl.context import ProjectContext


def _runtime_adapter(context: ProjectContext):
    try:
        return make_runtime_adapter(
            context,
            compose_adapter_cls=DockerComposeAdapter,
        )
    except TypeError:
        return DockerComposeAdapter(context.config.runtime.compose_file)


def execute(
    environment: str,
    service: str | None = None,
    config_path: str = "odooctl.yml",
    *,
    follow: bool = True,
    tail: int | None = None,
) -> None:
    context = ProjectContext.from_config_path(config_path)
    cfg = context.config
    cfg.env(environment)
    _runtime_adapter(context).logs(
        service or cfg.odoo.service,
        follow=follow,
        tail=tail,
    )
