"""Restore service — validate and apply backup archives."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from odooctl.adapters.db import make_db_adapter as make_context_db_adapter
from odooctl.adapters.docker_compose import DockerComposeAdapter
from odooctl.adapters.filestore import FilestoreAdapter, make_filestore_adapter
from odooctl.adapters.postgres import PostgresAdapter
from odooctl.adapters.reverse_proxy import public_url
from odooctl.adapters.runtime import make_runtime_adapter
from odooctl.odoo.healthcheck import check_url, with_db_selector
from odooctl.odoo.neutralize import neutralize_database, probe_native_neutralization
from odooctl.metadata.models import SanitizationMetadata
from odooctl.metadata.store import MetadataStore
from odooctl.services.models import RestoreResult

if TYPE_CHECKING:
    from odooctl.services.context import ServiceContext

REQUIRED_BACKUP_FILES = ("db.dump", "filestore.tar", "manifest.json")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_backup_dir(
    environment: str,
    backup: str,
    backups_root: Path,
    *,
    expected_project: str | None = None,
) -> Path:
    """Resolve a backup id to a directory strictly inside *backups_root*.

    Path containment (audit finding F10): *backup* is client-suppliable (CLI
    argument, API/runner params), so a hostile id like ``../../etc`` must never
    escape the backups root. Backup ids are plain directory names (e.g.
    ``staging_2026-01-02_000000``); anything containing a path separator or
    ``..`` is rejected outright, and the joined path is resolved and required
    to remain inside ``backups_root.resolve()`` (defense-in-depth against
    symlink tricks).
    """
    if backup != "latest":
        if (
            not backup
            or "/" in backup
            or "\\" in backup
            or ".." in backup
            or backup == "."
        ):
            raise ValueError(
                f"Invalid backup id {backup!r}: backup ids are plain directory names "
                "and must not contain path separators or '..'"
            )
        root = backups_root.resolve()
        candidate = (root / backup).resolve()
        if candidate == root or not candidate.is_relative_to(root):
            raise ValueError(
                f"Invalid backup id {backup!r}: resolved path {candidate} "
                f"escapes the backups root {root}"
            )
        return candidate
    candidates = sorted(
        path
        for path in backups_root.glob(f"{environment}_*")
        if path.is_dir() and not path.is_symlink()
    )
    if expected_project is not None:
        # A backups root may be shared by multiple projects. Select the newest
        # directory whose manifest claims this project instead of selecting a
        # newer foreign backup and only discovering the mismatch after the
        # choice has already been made. The selected manifest is validated
        # again by the caller, which closes the read/selection race.
        for candidate in reversed(candidates):
            manifest_path = candidate / "manifest.json"
            if not manifest_path.is_file() or manifest_path.is_symlink():
                continue
            try:
                manifest = json.loads(manifest_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if manifest.get("project") == expected_project:
                return candidate
        raise RuntimeError(
            f"No backups found for project {expected_project!r} "
            f"and environment {environment!r}"
        )
    if not candidates:
        raise RuntimeError(f"No backups found for environment: {environment}")
    return candidates[-1]


def validate_backup_dir(
    backup_dir: Path,
    *,
    expected_project: str | None = None,
    expected_environment: str | None = None,
    restore_mode: str = "full",
) -> dict:
    if not backup_dir.exists() or not backup_dir.is_dir():
        raise FileNotFoundError(f"Backup directory does not exist: {backup_dir}")
    missing = [name for name in REQUIRED_BACKUP_FILES if not (backup_dir / name).exists()]
    if missing:
        raise FileNotFoundError(f"Backup is missing required file(s): {', '.join(missing)}")
    manifest = json.loads((backup_dir / "manifest.json").read_text())
    if expected_project is not None and manifest.get("project") != expected_project:
        raise RuntimeError(
            f"Backup project mismatch: expected {expected_project}, got {manifest.get('project')}"
        )
    if (
        expected_environment is not None
        and manifest.get("environment") != expected_environment
    ):
        raise RuntimeError(
            f"Backup environment mismatch: expected {expected_environment}, got {manifest.get('environment')}"
        )
    if manifest.get("backup_id") != backup_dir.name:
        raise RuntimeError(
            f"Backup id mismatch: expected {backup_dir.name}, got {manifest.get('backup_id')}"
        )
    if manifest.get("status", "complete") != "complete":
        raise RuntimeError(
            f"Backup is not complete: {manifest.get('status')}"
        )
    if restore_mode == "full" and manifest.get("backup_mode", "full") != "full":
        raise RuntimeError(f"Unsupported backup mode for full restore: {manifest.get('backup_mode')}")
    checksums = manifest.get("checksums") or {}
    for key, file_name in (("db_dump", "db.dump"), ("filestore", "filestore.tar")):
        expected = checksums.get(key)
        if not expected:
            raise RuntimeError(f"Backup manifest is missing checksum for {file_name}")
        if sha256_file(backup_dir / file_name) != expected:
            raise RuntimeError(f"Backup checksum mismatch for {file_name}")
    return manifest


def restore_to_env(
    *,
    source_environment: str,
    target_environment: str,
    backup: str = "latest",
    ctx: "ServiceContext",
) -> RestoreResult:
    """Restore a backup from *source_environment* into *target_environment*.

    Uses a safe staging flow: restore into a temp DB first, then atomically
    swap/rename it into the target DB name. The target environment must not be
    protected. The source backup is validated (checksums) but environment-mismatch
    check is intentionally skipped so a production backup can be restored into staging.
    """
    from odooctl.odoo.db_swap import swap_temp_database

    cfg = ctx.project.config

    if cfg.is_protected(target_environment):
        raise RuntimeError(
            f"Cannot restore into protected environment {target_environment!r}. "
            "Use a non-production target (e.g. staging)."
        )

    env = cfg.env(target_environment)
    source_is_protected = cfg.is_protected(source_environment)
    if source_is_protected and not env.sanitize:
        raise RuntimeError(
            f"Refusing to restore protected-environment backup ({source_environment!r}) "
            f"into {target_environment!r} without sanitization. "
            "Set sanitize: true on the target environment."
        )

    backup_dir = resolve_backup_dir(source_environment, backup, ctx.project.backups_dir)
    # Validate checksums but skip environment-mismatch check (cross-env restore)
    validate_backup_dir(backup_dir, expected_project=cfg.project.name)
    sanitization_sql_files = ctx.project.sanitization_sql_files()
    if source_is_protected:
        for sql_file in sanitization_sql_files:
            if not sql_file.exists():
                raise FileNotFoundError(
                    f"Configured sanitization SQL file does not exist: {sql_file}"
                )

    temp_db = env.db_name + cfg.sanitization.temp_db_suffix
    if temp_db == env.db_name:
        raise RuntimeError(
            "Configured sanitization.temp_db_suffix must produce a temporary "
            "database distinct from the target"
        )

    compose = None
    native_capability = None
    if source_is_protected:
        compose = make_runtime_adapter(
            ctx.project,
            compose_adapter_cls=DockerComposeAdapter,
        )
        native_capability = probe_native_neutralization(cfg, compose=compose)

    pg = make_context_db_adapter(ctx.project) if cfg.runtime.execution_mode == "docker" else PostgresAdapter(cfg.postgres)
    # Restore into temp DB, not the live target DB
    pg.restore(temp_db, backup_dir / "db.dump")

    # Mirror clone safety contract: sanitize temp DB before swap when source is protected
    neutralization = None
    if source_is_protected:
        neutralization = neutralize_database(
            pg=pg,
            db_name=temp_db,
            env=env,
            cfg=cfg,
            sql_files=sanitization_sql_files,
            compose=compose,
            native_capability=native_capability,
        )

    # Preserve the live target filestore if neutralization fails.
    target_filestore = env.filestore_path if env.filestore_volume else str(ctx.project.resolve_path(env.filestore_path))
    fs = make_filestore_adapter(ctx.project, env) if env.filestore_volume else FilestoreAdapter()
    fs.restore_archive(backup_dir / "filestore.tar", target_filestore)

    # Atomically promote temp DB into the target DB name
    swap_temp_database(pg, temp_db=temp_db, target_db=env.db_name, target_env_name=target_environment)
    if neutralization:
        MetadataStore(ctx.project.state_dir).save_sanitization(
            SanitizationMetadata(
                project=cfg.project.name,
                source_environment=source_environment,
                target_environment=target_environment,
                database=env.db_name,
                policy=neutralization.policy,
                native_status=neutralization.native_status,
                profile=neutralization.profile,
                extension_statements=neutralization.extension_statements,
                custom_sql_files=neutralization.custom_sql_files,
                verification_checks=list(neutralization.verification_checks),
            )
        )

    scheme = cfg.healthcheck.scheme or env.scheme
    url = with_db_selector(
        public_url(env.domain, scheme=scheme, port=env.port) + cfg.healthcheck.path,
        env.db_name if env.db_selector else None,
    )
    check_url(url, timeout=cfg.healthcheck.timeout_seconds, retries=cfg.healthcheck.retries, interval=cfg.healthcheck.interval_seconds)
    return RestoreResult(backup_id=backup_dir.name)


def run_restore(ctx: ServiceContext, environment: str, backup: str = "latest") -> RestoreResult:
    """Restore *environment* from one of its own backups.

    Verify-before-destroy: the dump is restored into a temp database first,
    so a corrupt or failing restore never destroys the live database. Only
    after pg_restore succeeds is the temp DB swapped into place.
    """
    from odooctl.odoo.db_swap import swap_temp_database

    cfg = ctx.project.config
    env = cfg.env(environment)
    backup_dir = resolve_backup_dir(environment, backup, ctx.project.backups_dir)
    validate_backup_dir(backup_dir, expected_project=cfg.project.name, expected_environment=environment, restore_mode="full")
    pg = make_context_db_adapter(ctx.project) if cfg.runtime.execution_mode == "docker" else PostgresAdapter(cfg.postgres)
    temp_db = env.db_name + cfg.sanitization.temp_db_suffix
    if temp_db == env.db_name:
        raise RuntimeError(
            "Configured sanitization.temp_db_suffix must produce a temporary "
            "database distinct from the target"
        )
    pg.restore(temp_db, backup_dir / "db.dump")
    target_filestore = env.filestore_path if env.filestore_volume else str(ctx.project.resolve_path(env.filestore_path))
    fs = make_filestore_adapter(ctx.project, env) if env.filestore_volume else FilestoreAdapter()
    fs.restore_archive(backup_dir / "filestore.tar", target_filestore)
    # Same-environment recovery may target a protected env by design; the CLI
    # confirmation gate (--yes) is the policy layer for this path.
    swap_temp_database(pg, temp_db=temp_db, target_db=env.db_name, target_env_name=environment)
    scheme = cfg.healthcheck.scheme or env.scheme
    url = with_db_selector(
        public_url(env.domain, scheme=scheme, port=env.port) + cfg.healthcheck.path,
        env.db_name if env.db_selector else None,
    )
    check_url(url, timeout=cfg.healthcheck.timeout_seconds, retries=cfg.healthcheck.retries, interval=cfg.healthcheck.interval_seconds)
    return RestoreResult(backup_id=backup_dir.name)
