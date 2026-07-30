"""Deploy service — orchestrate git pull, compose update, module update, and health check."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from odooctl.adapters.docker_compose import DockerComposeAdapter
from odooctl.adapters.db import make_db_adapter as make_context_db_adapter
from odooctl.adapters.postgres import PostgresAdapter
from odooctl.adapters.reverse_proxy import public_url
from odooctl.adapters.runtime import make_runtime_adapter, validate_runtime_definition
from odooctl.adapters.runtime import RolloutState
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
    validate_runtime_definition(ctx.project)
    filestore_path = ctx.project.resolve_path(env.filestore_path)
    if not env.filestore_volume and not filestore_path.exists():
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
    compose = make_runtime_adapter(
        ctx.project,
        environment=environment,
        compose_adapter_cls=DockerComposeAdapter,
    )
    backup_id = None
    snapshot_id = None
    snapshot_status = None
    snapshot_warning = None
    status = "failed"
    message = None
    db_mutation_possible = False
    service_mutation_possible = False
    rollout_state: RolloutState | None = None
    rollout_rollback = None
    try:
        if cfg.is_protected(environment):
            print("[deploy] backup")
            backup_result = backup_execute(ctx, environment)
            backup_id = backup_result.backup_id
            if (
                cfg.snapshots.pre_deploy != "disabled"
                and environment == cfg.snapshots.environment
            ):
                print("[deploy] snapshot")
                try:
                    from odooctl.services.snapshots import run_snapshot_create

                    snapshot = run_snapshot_create(
                        ctx,
                        environment,
                        trigger="pre_deploy",
                        portable_backup_id=backup_id,
                    )
                    snapshot_id = snapshot.snapshot_id
                    snapshot_status = snapshot.status
                    if snapshot.status != "complete":
                        snapshot_warning = (
                            f"pre-deploy snapshot {snapshot.snapshot_id} is "
                            f"{snapshot.status}; reconcile it before relying on it"
                        )
                        if cfg.snapshots.pre_deploy == "required":
                            raise RuntimeError(snapshot_warning)
                        print(f"[deploy] warning: {snapshot_warning}")
                except Exception as snapshot_exc:
                    failed_manifest = getattr(snapshot_exc, "manifest", None)
                    if failed_manifest is not None:
                        snapshot_id = failed_manifest.snapshot_id
                        snapshot_status = failed_manifest.status
                    if snapshot_status is None:
                        snapshot_status = "failed"
                    if snapshot_warning is None:
                        snapshot_warning = (
                            f"pre-deploy snapshot failed: {snapshot_exc}"
                        )
                    if cfg.snapshots.pre_deploy == "required":
                        raise RuntimeError(snapshot_warning) from snapshot_exc
                    print(f"[deploy] warning: {snapshot_warning}")
        print("[deploy] rollout")
        run(["git", "fetch", "--all"], stream=True, cwd=str(ctx.project.root))
        run(["git", "checkout", selected_branch], stream=True, cwd=str(ctx.project.root))
        run(["git", "pull", "--ff-only"], stream=True, cwd=str(ctx.project.root))
        service_mutation_possible = True
        if hasattr(compose, "begin_rollout"):
            if not compose.supports_rollout(env.rollout_strategy):
                raise RuntimeError(
                    f"Runtime {compose.runtime_type} does not support rollout "
                    f"strategy {env.rollout_strategy!r}"
                )
            revision = git_commit(ctx.project.root) or "unknown"
            rollout_state = compose.begin_rollout(
                cfg.odoo.service,
                strategy=env.rollout_strategy,
                revision=revision,
                canary_percent=env.canary_percent,
            )
            command_workload = rollout_state.command_workload
        else:
            # Compatibility path for third-party pre-R9 adapters.
            compose.pull(cfg.odoo.service)
            compose.up(cfg.odoo.service)
            command_workload = cfg.odoo.service
        db_mutation_possible = True
        if env.update_modules and env.rollout_strategy in {
            "rolling",
            "blue_green",
            "canary",
        }:
            print(
                "[deploy] warning: module updates mutate the shared database; "
                "an incompatible schema can make the old workload unsafe even "
                "when application rollback succeeds"
            )
        update_modules_compose(
            compose,
            command_workload,
            env.db_name,
            env.update_modules,
            cli_command=cfg.odoo.cli_command,
            db_host=cfg.odoo.db_host,
            db_user=cfg.odoo.db_user,
            db_password_env=cfg.odoo.db_password_env,
            config_path=cfg.odoo.config_path,
        )
        if rollout_state and rollout_state.strategy == "canary":
            print("[deploy] verify canary")
            check_url(
                url,
                timeout=cfg.healthcheck.timeout_seconds,
                retries=cfg.healthcheck.retries,
                interval=cfg.healthcheck.interval_seconds,
            )
        if rollout_state:
            compose.promote_rollout(rollout_state)
        print("[deploy] verify")
        check_url(
            url,
            timeout=cfg.healthcheck.timeout_seconds,
            retries=cfg.healthcheck.retries,
            interval=cfg.healthcheck.interval_seconds,
        )
        if rollout_state:
            compose.finalize_rollout(rollout_state)
        status = "success"
        if snapshot_warning:
            message = snapshot_warning
        print("[deploy] done")
    except Exception as exc:
        failed_state = getattr(exc, "state", None)
        if rollout_state is None and isinstance(failed_state, RolloutState):
            rollout_state = failed_state
        message = str(exc)
        if snapshot_warning and snapshot_warning not in message:
            message = f"{snapshot_warning}; {message}"
        recovery_notes = []
        runtime_rollback_attempted = False
        if rollout_state is not None and env.auto_rollback:
            runtime_rollback_attempted = True
            try:
                compose.rollback_rollout(rollout_state)
                rollout_rollback = "success"
                recovery_notes.append(
                    f"{env.rollout_strategy} workload rollback complete"
                )
            except Exception as rollout_exc:
                rollout_rollback = "failed"
                recovery_notes.append(
                    f"workload rollback FAILED ({rollout_exc})"
                )
        if cfg.is_protected(environment):
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
            if service_mutation_possible and not runtime_rollback_attempted:
                try:
                    compose.restart(cfg.odoo.service)
                except Exception as recovery_exc:
                    recovery_notes.append(
                        f"recovery restart failed: {recovery_exc}"
                    )
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
                snapshot=snapshot_id,
                snapshot_status=snapshot_status,
                modules_updated=env.update_modules,
                status=status,
                health_check_url=url,
                message=message,
                rollout_strategy=env.rollout_strategy,
                rollout_rollback=rollout_rollback,
            )
        )
    return DeployResult(
        environment=environment,
        backup_id=backup_id,
        snapshot_id=snapshot_id,
        status=status,
    )
