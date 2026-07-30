"""Project status service — reads compose/metadata and returns StatusReport."""
from __future__ import annotations

from typing import TYPE_CHECKING

from odooctl.adapters.docker_compose import DockerComposeAdapter
from odooctl.adapters.reverse_proxy import public_url
from odooctl.adapters.runtime import make_runtime_adapter
from odooctl.metadata.store import MetadataStore
from odooctl.odoo.healthcheck import with_db_selector
from odooctl.services.backup import git_commit
from odooctl.services.models import EnvironmentSummary, StatusReport

if TYPE_CHECKING:
    from odooctl.services.context import ServiceContext


def _service_status(ps_output: str, service: str) -> str:
    service_name = service.lower()
    for line in ps_output.splitlines():
        tokens = line.strip().split()
        if not tokens:
            continue
        if tokens[0].lower() == service_name:
            lowered = line.lower()
            if "running" in lowered:
                return "running"
            if "exit" in lowered or "stopped" in lowered:
                return "stopped"
            return "unknown"
    return "unknown"


def _build_environment_summary(
    name: str,
    env,
    cfg,
    store: MetadataStore,
    ps: str,
) -> EnvironmentSummary:
    dep = store.latest_deployment(name) or {}
    backup = store.latest_backup(name) or {}
    scheme = cfg.healthcheck.scheme or env.scheme
    env_url = public_url(env.domain, scheme=scheme, port=env.port)
    health_url = dep.get(
        "health_check_url",
        with_db_selector(env_url + cfg.healthcheck.path, env.db_name if env.db_selector else None),
    )
    return EnvironmentSummary(
        name=name,
        url=env_url,
        branch=env.branch,
        commit=dep.get("commit", backup.get("git_commit", "unknown")),
        image=dep.get("docker_image", backup.get("docker_image", cfg.odoo.image)),
        odoo_status=_service_status(ps, cfg.odoo.service),
        postgres_status=_service_status(ps, cfg.postgres.service),
        latest_backup=backup.get("timestamp", "unknown"),
        last_deployment=dep.get("status", "unknown"),
        last_deployment_backup=dep.get("backup", "unknown"),
        last_deployment_message=dep.get("message"),
        health_check=(
            "passing"
            if dep.get("status") == "success"
            else "failing"
            if dep.get("health_check_url")
            else dep.get("status", "unknown")
        ),
        health_check_url=health_url,
    )


def get_status(ctx: ServiceContext, environment: str | None = None) -> StatusReport:
    cfg = ctx.project.config
    store = MetadataStore(ctx.project.state_dir)
    compose = make_runtime_adapter(
        ctx.project,
        compose_adapter_cls=DockerComposeAdapter,
    )
    ps = compose.ps()
    env_items = [(environment, cfg.env(environment))] if environment else list(cfg.environments.items())
    environments = [_build_environment_summary(name, env, cfg, store, ps) for name, env in env_items]
    return StatusReport(
        project=cfg.project.name,
        git_commit=git_commit(ctx.project.root) or "unknown",
        environments=environments,
        raw_compose_output=ps,
    )
