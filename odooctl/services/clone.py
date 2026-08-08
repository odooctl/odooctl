"""Clone service — copy production data to a target environment with sanitization."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from odooctl.adapters.docker_compose import DockerComposeAdapter
from odooctl.adapters.db import make_db_adapter as make_context_db_adapter
from odooctl.adapters.filestore import FilestoreAdapter, make_filestore_adapter
from odooctl.adapters.postgres import PostgresAdapter
from odooctl.adapters.reverse_proxy import public_url
from odooctl.adapters.runtime import make_runtime_adapter, validate_runtime_definition
from odooctl.odoo.db_swap import swap_temp_database
from odooctl.odoo.healthcheck import check_url, with_db_selector
from odooctl.odoo.module_update import update_modules_compose
from odooctl.odoo.neutralize import neutralize_database, probe_native_neutralization
from odooctl.metadata.models import SanitizationMetadata
from odooctl.metadata.store import MetadataStore
from odooctl.services.models import CloneResult

if TYPE_CHECKING:
    from odooctl.services.context import ServiceContext


def run_clone(
    ctx: ServiceContext,
    source: str,
    target: str,
    sanitize: bool | None = True,
    sanitization_profile: str = "normal",
    preview: bool = False,
) -> CloneResult:
    cfg = ctx.project.config
    src = cfg.env(source)
    dst = cfg.env(target)
    if not dst.clone_from:
        raise RuntimeError(
            f"Environment '{target}' is not configured as a clone target; set clone_from before cloning into it"
        )
    if dst.clone_from != source:
        raise RuntimeError(f"Environment '{target}' must be cloned from '{dst.clone_from}', not '{source}'")
    should_sanitize = dst.sanitize if sanitize is None else sanitize
    if cfg.is_protected(source) and not should_sanitize:
        raise RuntimeError("Refusing to clone protected environment data without sanitization enabled")

    validate_runtime_definition(ctx.project)

    scheme = cfg.healthcheck.scheme or dst.scheme
    base_url = public_url(dst.domain, scheme=scheme, port=dst.port)

    if preview:
        print("[clone] preview")
        print(
            f"source={source} target={target} profile={sanitization_profile} "
            f"base_url={base_url} sanitize={'yes' if should_sanitize else 'no'}"
        )
        print(f"source_branch={src.branch} target_branch={dst.branch} clone_from={dst.clone_from}")
        print(f"affected_integrations={','.join(dst.update_modules) or 'none'}")
        print(f"production_source={'yes' if cfg.is_protected(source) else 'no'}")
        return CloneResult(url=base_url)

    missing_env_vars = cfg.missing_env_vars()
    if missing_env_vars:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing_env_vars)}")

    sanitization_sql_files = ctx.project.sanitization_sql_files()
    if should_sanitize:
        for sql_file in sanitization_sql_files:
            if not sql_file.exists():
                raise FileNotFoundError(
                    f"Configured sanitization SQL file does not exist: {sql_file}"
                )
    compose = make_runtime_adapter(
        ctx.project,
        environment=target,
        compose_adapter_cls=DockerComposeAdapter,
    )
    native_capability = (
        probe_native_neutralization(cfg, compose=compose) if should_sanitize else None
    )
    pg = make_context_db_adapter(ctx.project) if cfg.runtime.execution_mode == "docker" else PostgresAdapter(cfg.postgres)
    fs = make_filestore_adapter(ctx.project, dst) if dst.filestore_volume else FilestoreAdapter()
    temp_db = f"{dst.db_name}{cfg.sanitization.temp_db_suffix}"
    if temp_db == dst.db_name:
        raise RuntimeError(
            "Configured sanitization.temp_db_suffix must produce a temporary database distinct from the target"
        )
    if hasattr(pg, "clone_db_in_container"):
        pg.clone_db_in_container(src.db_name, temp_db)  # type: ignore[attr-defined]
    else:
        with tempfile.NamedTemporaryFile(prefix="odooctl-clone-", suffix=".dump", delete=False) as tmp:
            tmp_dump = Path(tmp.name)
        try:
            pg.dump(src.db_name, tmp_dump)
            pg.restore(temp_db, tmp_dump)
        finally:
            tmp_dump.unlink(missing_ok=True)
    src_filestore = src.filestore_path if src.filestore_volume else str(ctx.project.resolve_path(src.filestore_path))
    dst_filestore = dst.filestore_path if dst.filestore_volume else str(ctx.project.resolve_path(dst.filestore_path))
    neutralization = None
    if should_sanitize:
        neutralization = neutralize_database(
            pg=pg,
            db_name=temp_db,
            env=dst,
            cfg=cfg,
            profile=sanitization_profile,
            sql_files=sanitization_sql_files,
            compose=compose,
            native_capability=native_capability,
        )
    # Do not replace the live target filestore until neutralization and all
    # extension verification checks have passed.
    fs.copy(src_filestore, dst_filestore)
    swap_temp_database(
        pg,
        temp_db=temp_db,
        target_db=dst.db_name,
        target_env_name=target,
        is_protected_fn=cfg.is_protected,
    )
    if neutralization:
        MetadataStore(ctx.project.state_dir).save_sanitization(
            SanitizationMetadata(
                project=cfg.project.name,
                source_environment=source,
                target_environment=target,
                database=dst.db_name,
                policy=neutralization.policy,
                native_status=neutralization.native_status,
                profile=neutralization.profile,
                extension_statements=neutralization.extension_statements,
                custom_sql_files=neutralization.custom_sql_files,
                verification_checks=list(neutralization.verification_checks),
            )
        )
    update_modules_compose(
        compose,
        cfg.odoo.service,
        dst.db_name,
        dst.update_modules,
        cli_command=cfg.odoo.cli_command,
        db_host=cfg.odoo.db_host,
        db_user=cfg.odoo.db_user,
        db_password_env=cfg.odoo.db_password_env,
        config_path=cfg.odoo.config_path,
    )
    compose.restart(cfg.odoo.service)
    running_services = compose.ps()
    if cfg.odoo.service not in running_services:
        raise RuntimeError(f"Target service is not running after clone: {cfg.odoo.service}")
    url = with_db_selector(base_url + cfg.healthcheck.path, dst.db_name if dst.db_selector else None)
    check_url(
        url,
        timeout=cfg.healthcheck.timeout_seconds,
        retries=cfg.healthcheck.retries,
        interval=cfg.healthcheck.interval_seconds,
    )
    return CloneResult(
        url=base_url,
        native_neutralization=neutralization.native_status if neutralization else None,
    )
