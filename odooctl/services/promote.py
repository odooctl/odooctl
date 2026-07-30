"""Promote service — safe staging→production flow with backup and rollback."""
from __future__ import annotations

from typing import TYPE_CHECKING

from odooctl.adapters.docker_compose import DockerComposeAdapter
from odooctl.adapters.reverse_proxy import public_url
from odooctl.metadata.models import DeploymentMetadata
from odooctl.metadata.store import MetadataStore
from odooctl.odoo.healthcheck import check_url, with_db_selector
from odooctl.odoo.module_update import update_modules_compose
from odooctl.services.backup import git_commit, run_backup as backup_execute
from odooctl.services.models import PromoteResult
from odooctl.services.restore import run_restore
from odooctl.utils.shell import run

if TYPE_CHECKING:
    from odooctl.services.context import ServiceContext


def _env_url(cfg, name: str) -> str:
    env = cfg.env(name)
    scheme = cfg.healthcheck.scheme or env.scheme
    return with_db_selector(
        public_url(env.domain, scheme=scheme, port=env.port) + cfg.healthcheck.path,
        env.db_name if env.db_selector else None,
    )


def _assert_clean_worktree(cwd: str) -> None:
    result = run(["git", "status", "--porcelain"], check=False, cwd=cwd)
    dirty = getattr(result, "stdout", "") or ""
    if dirty.strip():
        raise RuntimeError(
            "Working tree is dirty; commit or stash changes before promoting."
        )


def promote_preview(ctx: ServiceContext, source: str, target: str) -> PromoteResult:
    """Show what promote would do — no side effects."""
    cfg = ctx.project.config
    src_env = cfg.env(source)
    tgt_env = cfg.env(target)
    if src_env.promotes_to != target:
        raise RuntimeError(
            f"Environment '{source}' does not promote to '{target}'. "
            f"Configure promotes_to: {target} in the '{source}' environment to enable this flow."
        )
    source_url = _env_url(cfg, source)
    target_url = _env_url(cfg, target)
    print(f"[promote] preview: {source} → {target}")
    print(f"  source branch   : {src_env.branch}")
    print(f"  target branch   : {tgt_env.branch}")
    print(f"  merge policy    : fast-forward only ({src_env.branch} → {tgt_env.branch})")
    print(f"  source health   : {source_url}")
    print(f"  target health   : {target_url}")
    print("  backup target   : yes (before deploy)")
    print("  rollback on fail: yes (restore backup + code reset + redeploy)")
    print(f"  protected target: {cfg.is_protected(target)}")
    return PromoteResult(source=source, target=target, status="preview")


def run_promote(
    ctx: ServiceContext, source: str, target: str, confirm: bool = False
) -> PromoteResult:
    """Promote source into target: health check → backup → ff-merge → deploy → verify → rollback on failure."""
    cfg = ctx.project.config
    src_env = cfg.env(source)
    tgt_env = cfg.env(target)

    if src_env.promotes_to != target:
        raise RuntimeError(
            f"Environment '{source}' does not promote to '{target}'. "
            f"Configure promotes_to: {target} in the '{source}' environment."
        )

    compose_path = ctx.project.compose_file
    if not compose_path.exists():
        raise FileNotFoundError(f"Compose file not found: {compose_path}")

    missing_env_vars = cfg.missing_env_vars()
    if missing_env_vars:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing_env_vars)}")

    if cfg.is_protected(target) and not confirm:
        raise RuntimeError(
            f"Target environment '{target}' is protected. "
            "Pass confirm=True (CLI: --yes) to proceed."
        )

    # 1. Check source health
    print("[promote] check source health")
    source_url = _env_url(cfg, source)
    check_url(
        source_url,
        timeout=cfg.healthcheck.timeout_seconds,
        retries=cfg.healthcheck.retries,
        interval=cfg.healthcheck.interval_seconds,
    )

    # 2. Clean-worktree preflight before any mutation
    print("[promote] preflight: clean worktree")
    _assert_clean_worktree(str(ctx.project.root))

    # 3. Backup target before any mutation
    print("[promote] backup target")
    backup_result = backup_execute(ctx, target)
    backup_id = backup_result.backup_id

    # 4. Fast-forward merge source into target, deploy, verify
    selected_branch = tgt_env.branch
    compose = DockerComposeAdapter(cfg.runtime.compose_file, project_dir=str(ctx.project.root))
    target_url = _env_url(cfg, target)
    status = "failed"
    message = None
    pre_promote_commit = None

    try:
        run(["git", "fetch", "--all"], stream=True, cwd=str(ctx.project.root))
        run(["git", "checkout", selected_branch], stream=True, cwd=str(ctx.project.root))
        pre_promote_commit = git_commit(ctx.project.root)
        run(["git", "merge", "--ff-only", src_env.branch], stream=True, cwd=str(ctx.project.root))
        compose.pull(cfg.odoo.service)
        compose.up(cfg.odoo.service)
        update_modules_compose(
            compose,
            cfg.odoo.service,
            tgt_env.db_name,
            tgt_env.update_modules,
            cli_command=cfg.odoo.cli_command,
            db_host=cfg.odoo.db_host,
            db_user=cfg.odoo.db_user,
            db_password_env=cfg.odoo.db_password_env,
            config_path=cfg.odoo.config_path,
        )
        # 5. Healthcheck target
        print("[promote] verify target")
        check_url(
            target_url,
            timeout=cfg.healthcheck.timeout_seconds,
            retries=cfg.healthcheck.retries,
            interval=cfg.healthcheck.interval_seconds,
        )
        status = "success"
        print("[promote] done")
    except Exception as exc:
        message = str(exc)
        print(f"[promote] failed: {message}")
        rollback_ok = True

        # Data rollback
        print(f"[promote] rollback: restoring {target} data from {backup_id}")
        try:
            run_restore(ctx, target, backup_id)
            print("[promote] data rollback complete")
        except Exception as restore_exc:
            rollback_ok = False
            print(f"[promote] WARNING: data restore failed: {restore_exc}")

        # Code rollback: reset to pre-promote commit and redeploy
        if pre_promote_commit is not None:
            print(f"[promote] rollback: restoring code to {pre_promote_commit}")
            try:
                run(["git", "checkout", selected_branch], stream=True, cwd=str(ctx.project.root))
                run(["git", "reset", "--hard", pre_promote_commit], stream=True, cwd=str(ctx.project.root))
                compose.up(cfg.odoo.service)
                print("[promote] code rollback complete")
            except Exception as code_exc:
                rollback_ok = False
                print(f"[promote] WARNING: code rollback failed: {code_exc}")

        if rollback_ok:
            raise RuntimeError(
                f"Promote failed; rolled back to backup {backup_id}: {exc}"
            ) from exc
        raise RuntimeError(
            f"Promote failed and rollback was incomplete. "
            f"Manual intervention required. Original error: {exc}"
        ) from exc
    finally:
        MetadataStore(ctx.project.state_dir).save_deployment(
            DeploymentMetadata(
                project=cfg.project.name,
                environment=target,
                branch=selected_branch,
                commit=git_commit(ctx.project.root),
                docker_image=cfg.odoo.image,
                backup=backup_id,
                modules_updated=tgt_env.update_modules,
                status=status,
                health_check_url=target_url,
                message=message,
            )
        )

    return PromoteResult(source=source, target=target, status=status, backup_id=backup_id)
