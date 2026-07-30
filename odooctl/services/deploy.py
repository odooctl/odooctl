"""Deploy service — orchestrate git pull, compose update, module update, and health check."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from odooctl.adapters.docker_compose import DockerComposeAdapter
from odooctl.adapters.db import make_db_adapter as make_context_db_adapter
from odooctl.adapters.postgres import PostgresAdapter
from odooctl.adapters.reverse_proxy import public_url
from odooctl.metadata.models import DeploymentMetadata
from odooctl.metadata.store import MetadataStore
from odooctl.odoo.healthcheck import check_url, with_db_selector
from odooctl.odoo.module_update import update_modules_compose
from odooctl.services.backup import git_commit, run_backup as backup_execute
from odooctl.services.models import DeployResult
from odooctl.utils.shell import run

if TYPE_CHECKING:
    from odooctl.services.context import ServiceContext


def _assert_clean_worktree(operation: str = "deploy", *, cwd: str | Path | None = None) -> None:
    result = run(["git", "status", "--porcelain"], check=False, cwd=str(cwd) if cwd is not None else None)
    dirty_paths = result.stdout.strip()
    if dirty_paths:
        raise RuntimeError(
            f"Git worktree is dirty; commit or stash changes before {operation}:\n{dirty_paths}"
        )


def run_deploy(ctx: ServiceContext, environment: str, branch: str | None = None) -> DeployResult:
    print("[deploy] preflight")
    cfg = ctx.project.config
    missing_env_vars = cfg.missing_env_vars()
    if missing_env_vars:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing_env_vars)}")
    env = cfg.env(environment)
    selected_branch = branch or env.branch
    if branch and branch != env.branch:
        raise RuntimeError(f"Branch '{branch}' is not allowed for environment '{environment}'")
    compose_path = ctx.project.compose_file
    if not compose_path.exists():
        raise FileNotFoundError(f"Compose file not found: {compose_path}")
    filestore_path = ctx.project.resolve_path(env.filestore_path)
    if not filestore_path.exists():
        raise FileNotFoundError(f"Target filestore path not found: {filestore_path}")
    try:
        pg = (
            make_context_db_adapter(ctx.project)
            if cfg.runtime.execution_mode == "docker"
            else PostgresAdapter(cfg.postgres)
        )
        pg.ping(env.db_name)
    except Exception as exc:
        raise RuntimeError(
            f"Postgres connectivity check failed for database '{env.db_name}' "
            f"on {cfg.postgres.host}:{cfg.postgres.port}: {exc}"
        ) from exc
    _assert_clean_worktree(cwd=ctx.project.root)

    scheme = cfg.healthcheck.scheme or env.scheme
    url = with_db_selector(
        public_url(env.domain, scheme=scheme, port=env.port) + cfg.healthcheck.path,
        env.db_name if env.db_selector else None,
    )
    compose = DockerComposeAdapter(cfg.runtime.compose_file, project_dir=str(ctx.project.root))
    backup_id = None
    status = "failed"
    message = None
    db_mutation_possible = False
    try:
        if cfg.is_protected(environment):
            print("[deploy] backup")
            backup_result = backup_execute(ctx, environment)
            backup_id = backup_result.backup_id
        print("[deploy] rollout")
        run(["git", "fetch", "--all"], stream=True, cwd=str(ctx.project.root))
        run(["git", "checkout", selected_branch], stream=True, cwd=str(ctx.project.root))
        run(["git", "pull", "--ff-only"], stream=True, cwd=str(ctx.project.root))
        compose.pull(cfg.odoo.service)
        compose.up(cfg.odoo.service)
        db_mutation_possible = True
        update_modules_compose(
            compose,
            cfg.odoo.service,
            env.db_name,
            env.update_modules,
            cli_command=cfg.odoo.cli_command,
            db_host=cfg.odoo.db_host,
            db_user=cfg.odoo.db_user,
            db_password_env=cfg.odoo.db_password_env,
            config_path=cfg.odoo.config_path,
        )
        print("[deploy] verify")
        check_url(
            url,
            timeout=cfg.healthcheck.timeout_seconds,
            retries=cfg.healthcheck.retries,
            interval=cfg.healthcheck.interval_seconds,
        )
        status = "success"
        print("[deploy] done")
    except Exception as exc:
        message = str(exc)
        if cfg.is_protected(environment):
            recovery_notes = []
            if backup_id is not None and db_mutation_possible:
                try:
                    from odooctl.services.restore import run_restore

                    run_restore(ctx, environment, backup=backup_id)
                    recovery_notes.append(f"database restored from pre-deploy backup {backup_id}")
                except Exception as restore_exc:
                    recovery_notes.append(
                        f"pre-deploy backup restore FAILED ({restore_exc}); "
                        f"restore manually from backup {backup_id}"
                    )
            try:
                compose.restart(cfg.odoo.service)
            except Exception as recovery_exc:
                recovery_notes.append(f"recovery restart failed: {recovery_exc}")
            if recovery_notes:
                message = f"{message}; " + "; ".join(recovery_notes)
        raise
    finally:
        MetadataStore(ctx.project.state_dir).save_deployment(
            DeploymentMetadata(
                project=cfg.project.name,
                environment=environment,
                branch=selected_branch,
                commit=git_commit(ctx.project.root),
                docker_image=cfg.odoo.image,
                backup=backup_id,
                modules_updated=env.update_modules,
                status=status,
                health_check_url=url,
                message=message,
            )
        )
    return DeployResult(environment=environment, backup_id=backup_id, status=status)
