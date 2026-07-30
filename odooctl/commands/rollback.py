from __future__ import annotations
from pathlib import Path

import typer
from odooctl.services.backup import git_commit
from odooctl.services.deploy import _assert_clean_worktree
from odooctl.commands.restore import execute as restore_execute
from odooctl.context import ProjectContext
from odooctl.adapters.docker_compose import DockerComposeAdapter
from odooctl.adapters.reverse_proxy import public_url
from odooctl.adapters.runtime import make_runtime_adapter, validate_runtime_definition
from odooctl.metadata.models import DeploymentMetadata
from odooctl.metadata.store import MetadataStore
from odooctl.odoo.healthcheck import check_url, with_db_selector
from odooctl.operations.audit import AuditStore
from odooctl.operations.engine import run_operation
from odooctl.operations.models import OperationKind
from odooctl.operations.store import OperationStore
from odooctl.utils.logging import warn
from odooctl.utils.shell import run


def _runtime_adapter(context: ProjectContext):
    try:
        return make_runtime_adapter(
            context,
            compose_adapter_cls=DockerComposeAdapter,
        )
    except TypeError:
        return DockerComposeAdapter(context.config.runtime.compose_file)


def _store(root: Path):
    try:
        return MetadataStore(root)
    except TypeError:
        return MetadataStore()


def _git_commit(root: Path) -> str | None:
    try:
        return git_commit(root)
    except TypeError:
        return git_commit()


def _clean_worktree(operation: str, root: Path) -> None:
    try:
        _assert_clean_worktree(operation, cwd=root)
    except TypeError:
        _assert_clean_worktree(operation)


def _verify_health(cfg, environment: str) -> None:
    env = cfg.env(environment)
    scheme = cfg.healthcheck.scheme or env.scheme
    url = with_db_selector(
        public_url(env.domain, scheme=scheme, port=env.port) + cfg.healthcheck.path,
        env.db_name if env.db_selector else None,
    )
    print("[rollback] verify")
    check_url(
        url,
        timeout=cfg.healthcheck.timeout_seconds,
        retries=cfg.healthcheck.retries,
        interval=cfg.healthcheck.interval_seconds,
    )


def execute(environment: str, mode: str = "code", backup: str | None = None, config_path: str = "odooctl.yml") -> None:
    context = ProjectContext.from_config_path(config_path)
    op_store = OperationStore(context.state_dir)
    audit = AuditStore(context.state_dir)
    cfg = context.config
    if mode not in {"code", "full"}:
        raise typer.BadParameter("--mode must be code or full")
    if mode == "full" and not backup:
        raise typer.BadParameter("Full rollback requires --backup")
    if mode == "full":
        missing_env_vars = cfg.missing_env_vars()
        if missing_env_vars:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing_env_vars)}")

    validate_runtime_definition(context)

    with run_operation(
        op_store,
        audit,
        kind=OperationKind.ROLLBACK,
        project=cfg.project.name,
        environment=environment,
        actor="cli",
        params_redacted={"environment": environment, "mode": mode},
        state_dir=context.state_dir,
    ) as op_ctx:
        op_ctx.emit(f"rollback {mode} on {environment}", phase="rollback")
        _run_rollback(context, environment, mode, backup, config_path, op_ctx)


def _run_rollback(context, environment, mode, backup, config_path, op_ctx=None):
    cfg = context.config
    env = cfg.env(environment)
    scheme = cfg.healthcheck.scheme or env.scheme
    url = with_db_selector(
        public_url(env.domain, scheme=scheme, port=env.port) + cfg.healthcheck.path,
        env.db_name if env.db_selector else None,
    )
    compose = _runtime_adapter(context)
    meta_store = _store(context.state_dir)
    status = "failed"
    message = None
    commit: str | None = _git_commit(context.root)
    image: str | None = cfg.odoo.image

    if mode == "code":
        previous = meta_store.previous_successful_deployment(environment)
        if not previous or not previous.get("commit"):
            raise RuntimeError(f"No previous successful deployment commit recorded for environment '{environment}'")
        recorded_branch = previous.get("branch")
        if recorded_branch and recorded_branch != env.branch:
            raise RuntimeError(
                f"Previous successful deployment for '{environment}' was on branch "
                f"'{recorded_branch}', but the environment now maps to '{env.branch}'; "
                "refusing code rollback across branches"
            )
        commit = str(previous["commit"])
        previous_image = previous.get("docker_image")
        image = str(previous_image) if previous_image else cfg.odoo.image
        _clean_worktree("code rollback", context.root)

    try:
        if mode == "code":
            warn("Code-only rollback: redeploying the last successful commit does not restore database or filestore.")
            print(f"[rollback] target commit: {commit}")
            print(f"[rollback] recorded image: {image}")
            run(["git", "fetch", "--all"], stream=True, cwd=str(context.root))
            if commit is None:
                raise RuntimeError("No current git commit available for code rollback metadata")
            run(["git", "checkout", commit], stream=True, cwd=str(context.root))
        else:
            if backup is None:
                raise RuntimeError("Full rollback requires a backup id")
            warn("Full rollback may discard production data created after the backup.")
            restore_execute(environment, backup, config_path)
        compose.up(cfg.odoo.service)
        _verify_health(cfg, environment)
        status = "success"
    except Exception as exc:
        message = str(exc)
        raise
    finally:
        meta_store.save_deployment(
            DeploymentMetadata(
                project=cfg.project.name,
                environment=environment,
                branch=env.branch,
                commit=commit,
                docker_image=image,
                backup=backup if mode == "full" else None,
                modules_updated=[],
                status=status,
                health_check_url=url,
                message=f"rollback:{mode}" if message is None else f"rollback:{mode}: {message}",
            )
        )
