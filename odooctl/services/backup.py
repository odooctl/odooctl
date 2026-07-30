"""Backup service — create, prune, and upload backups."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from odooctl.adapters.db import make_db_adapter as make_context_db_adapter
from odooctl.adapters.filestore import FilestoreAdapter, make_filestore_adapter
from odooctl.adapters.postgres import PostgresAdapter
from odooctl.metadata.models import BackupManifest
from odooctl.metadata.store import MetadataStore
from odooctl.services.models import BackupResult
from odooctl.utils.paths import ensure_dir
from odooctl.utils.shell import run as shell_run

if TYPE_CHECKING:
    from odooctl.services.context import ServiceContext

SENSITIVE_CONFIG_KEYS = re.compile(
    r"(password|passwd|admin_passwd|secret|token|api[_-]?key|smtp|oauth|webhook|license)",
    re.IGNORECASE,
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit(cwd: str | Path | None = None) -> str | None:
    r = shell_run(
        ["git", "rev-parse", "--short", "HEAD"],
        check=False,
        cwd=str(cwd) if cwd is not None else None,
    )
    return r.stdout.strip() or None


def _remove_tree(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
        return
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_file() or child.is_symlink():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    path.rmdir()


def _is_published_backup_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and not path.name.startswith(".partial-")


def prune_backups(
    backup_root: Path,
    keep: int,
    *,
    environment: str | None = None,
    newer_than_days: int | None = None,
    now: float | None = None,
) -> list[Path]:
    if not backup_root.exists():
        return []
    backups = sorted(
        [
            p
            for p in backup_root.iterdir()
            if _is_published_backup_dir(p)
            and (environment is None or p.name.startswith(f"{environment}_"))
        ],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed: list[Path] = []
    keep_count = max(keep, 0)
    to_remove = list(backups[keep_count:])
    if newer_than_days is not None:
        cutoff = (now if now is not None else datetime.now(timezone.utc).timestamp()) - (
            newer_than_days * 86400
        )
        to_remove.extend([p for p in backups[:keep_count] if p.stat().st_mtime < cutoff])
    for path in sorted(set(to_remove), key=lambda p: p.stat().st_mtime):
        removed.append(path)
        _remove_tree(path)
    return removed


@dataclass(frozen=True)
class _BackupCandidate:
    path: Path
    manifest: BackupManifest
    timestamp: datetime
    published_at: datetime


def _parse_manifest_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _backup_candidates(
    backup_root: Path,
    *,
    environment: str,
    project: str | None = None,
) -> list[_BackupCandidate]:
    if not backup_root.exists():
        return []
    candidates: list[_BackupCandidate] = []
    for path in backup_root.iterdir():
        if not _is_published_backup_dir(path) or not path.name.startswith(f"{environment}_"):
            continue
        manifest_path = path / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = BackupManifest.model_validate_json(manifest_path.read_text())
            timestamp = _parse_manifest_timestamp(manifest.timestamp)
            published_at = datetime.fromtimestamp(
                manifest_path.stat().st_mtime,
                timezone.utc,
            )
        except Exception:
            # Retention must never delete a directory it cannot identify as a
            # valid, published backup.
            continue
        if (
            manifest.backup_id != path.name
            or manifest.environment != environment
            or (
                project is not None
                and manifest.project != project
            )
            or manifest.status != "complete"
        ):
            continue
        candidates.append(
            _BackupCandidate(
                path=path,
                manifest=manifest,
                timestamp=timestamp,
                published_at=published_at,
            )
        )
    return sorted(
        candidates,
        key=lambda item: (item.timestamp, item.manifest.backup_id),
        reverse=True,
    )


def _select_gfs_candidates(
    candidates: list[_BackupCandidate],
    *,
    daily: int,
    weekly: int,
    monthly: int,
    keep_latest: bool,
) -> list[_BackupCandidate]:
    retained_ids: set[str] = set()
    if keep_latest and candidates:
        retained_ids.add(candidates[0].manifest.backup_id)

    def retain_periods(
        count: int,
        bucket_for,
    ) -> None:
        if count <= 0:
            return
        selected_buckets: set[object] = set()
        for candidate in candidates:
            bucket = bucket_for(candidate.timestamp)
            if bucket in selected_buckets:
                continue
            selected_buckets.add(bucket)
            retained_ids.add(candidate.manifest.backup_id)
            if len(selected_buckets) >= count:
                break

    retain_periods(max(daily, 0), lambda value: value.date())
    retain_periods(
        max(weekly, 0),
        lambda value: (value.isocalendar().year, value.isocalendar().week),
    )
    retain_periods(
        max(monthly, 0),
        lambda value: (value.year, value.month),
    )
    return [candidate for candidate in candidates if candidate.manifest.backup_id in retained_ids]


def select_gfs_backups(
    backup_root: Path,
    *,
    environment: str,
    daily: int,
    weekly: int,
    monthly: int,
    keep_latest: bool = True,
    project: str | None = None,
) -> list[Path]:
    """Select deterministic daily/ISO-weekly/monthly GFS restore points.

    Each tier keeps the newest complete backup in each represented UTC period,
    walking periods newest-first. The union is retained, and the newest backup
    is kept as a safety floor even when every tier is configured to zero.
    """
    candidates = _backup_candidates(
        backup_root,
        environment=environment,
        project=project,
    )
    return [
        candidate.path
        for candidate in _select_gfs_candidates(
            candidates,
            daily=daily,
            weekly=weekly,
            monthly=monthly,
            keep_latest=keep_latest,
        )
    ]


def prune_backups_gfs(
    backup_root: Path,
    *,
    environment: str,
    daily: int,
    weekly: int,
    monthly: int,
    project: str,
    metadata_store: MetadataStore | None = None,
    keep_latest: bool = True,
    protected_backup_ids: Iterable[str] = (),
    latest_backup_id: str | None = None,
    grace_hours: int = 0,
    now: datetime | None = None,
) -> list[Path]:
    """Apply GFS retention and synchronize the local manifest index."""
    candidates = _backup_candidates(
        backup_root,
        environment=environment,
        project=project,
    )
    retained = _select_gfs_candidates(
        candidates,
        daily=daily,
        weekly=weekly,
        monthly=monthly,
        keep_latest=keep_latest,
    )
    protected = set(protected_backup_ids)
    if grace_hours > 0:
        current_time = (now or _utc_now()).astimezone(timezone.utc)
        fresh_cutoff = current_time - timedelta(hours=grace_hours)
        protected.update(
            candidate.manifest.backup_id
            for candidate in candidates
            if candidate.published_at >= fresh_cutoff
        )
    retained_ids = {
        candidate.manifest.backup_id
        for candidate in retained
    } | protected
    retained = [
        candidate
        for candidate in candidates
        if candidate.manifest.backup_id in retained_ids
    ]

    # Point metadata only at the restore points that will survive. Writing the
    # latest pointer before directory deletion means a crash cannot leave it
    # pointing at a directory that this retention pass already removed.
    synchronize = (
        getattr(metadata_store, "synchronize_backup_manifests", None)
        if metadata_store is not None
        else None
    )
    if callable(synchronize):
        synchronize(
            environment,
            [candidate.manifest for candidate in retained],
            latest_backup_id=(
                latest_backup_id
                if latest_backup_id is not None
                else (
                    retained[0].manifest.backup_id
                    if retained
                    else None
                )
            ),
        )

    removed: list[Path] = []
    for candidate in sorted(
        candidates,
        key=lambda item: (item.timestamp, item.manifest.backup_id),
    ):
        if candidate.manifest.backup_id in retained_ids:
            continue
        removed.append(candidate.path)
        _remove_tree(candidate.path)
    return removed


def redact_config_snapshot(text: str) -> str:
    redacted_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            redacted_lines.append(line)
            continue
        if "=" not in line:
            if (
                stripped.startswith(("#", ";"))
                and SENSITIVE_CONFIG_KEYS.search(stripped)
            ):
                marker_index = min(
                    index
                    for index in (
                        line.find("#"),
                        line.find(";"),
                    )
                    if index >= 0
                )
                redacted_lines.append(
                    f"{line[:marker_index + 1]} ***REDACTED***"
                )
            else:
                redacted_lines.append(line)
            continue
        key, value = line.split("=", 1)
        if SENSITIVE_CONFIG_KEYS.search(key):
            redacted_lines.append(f"{key.rstrip()} = ***REDACTED***")
        elif stripped.startswith(("#", ";")):
            redacted_lines.append(line)
        else:
            redacted_lines.append(f"{key.rstrip()} = {value.strip()}")
    return "\n".join(redacted_lines) + ("\n" if text.endswith("\n") else "")


def remote_encryption_metadata(ctx: ServiceContext) -> dict[str, str] | None:
    """Return non-secret remote-backup encryption metadata for manifests."""
    remote = ctx.project.config.backups.remote
    if remote is None or not remote.encryption_algorithm:
        return None
    metadata = {"algorithm": remote.encryption_algorithm}
    if remote.encryption_key_env:
        metadata["key_ref"] = f"env:{remote.encryption_key_env}"
    return metadata


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _reserve_backup_staging(
    backup_root: Path,
    environment: str,
    *,
    created_at: datetime,
) -> tuple[str, Path, Path]:
    """Atomically reserve a unique backup id and same-filesystem staging dir."""
    timestamp = created_at.astimezone(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    base_id = f"{environment}_{timestamp}"
    for _ in range(128):
        # The random component is unconditional. Local mkdir is enough to
        # coordinate writers sharing one filesystem, but remote publishers can
        # run on independent hosts and still share the same project prefix.
        backup_id = f"{base_id}_{secrets.token_hex(12)}"
        final_path = backup_root / backup_id
        staging_path = backup_root / f".partial-{backup_id}"
        if final_path.exists():
            continue
        try:
            staging_path.mkdir()
        except FileExistsError:
            continue
        # The staging name is the reservation. If an earlier owner already
        # published this id before our mkdir succeeded, release and retry.
        if final_path.exists():
            staging_path.rmdir()
            continue
        return backup_id, staging_path, final_path
    raise RuntimeError("Could not reserve a unique backup id")


def _write_text_synced(path: Path, value: str) -> None:
    with path.open("w") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_staged_backup(
    backup_dir: Path,
    manifest: BackupManifest,
) -> None:
    expected_artifacts = {
        "db_dump": "db.dump",
        "filestore": "filestore.tar",
    }
    for checksum_key, file_name in expected_artifacts.items():
        artifact = backup_dir / file_name
        if not artifact.is_file() or artifact.is_symlink() or artifact.stat().st_size <= 0:
            raise RuntimeError(f"Backup staging is missing non-empty artifact {file_name}")
        expected = manifest.checksums.get(checksum_key)
        if not expected:
            raise RuntimeError(f"Backup manifest is missing checksum for {file_name}")
        if _sha256_file(artifact) != expected:
            raise RuntimeError(f"Backup checksum mismatch for staged {file_name}")

    manifest_path = backup_dir / "manifest.json"
    persisted = BackupManifest.model_validate_json(manifest_path.read_text())
    if persisted != manifest:
        raise RuntimeError("Staged backup manifest does not round-trip exactly")
    if (
        manifest.backup_id != backup_dir.name.removeprefix(".partial-")
        or manifest.status != "complete"
    ):
        raise RuntimeError("Staged backup manifest identity or status is invalid")


def _publish_staged_backup(
    staging_path: Path,
    final_path: Path,
) -> None:
    if final_path.exists():
        raise RuntimeError(f"Backup destination already exists: {final_path}")
    for path in sorted(item for item in staging_path.rglob("*") if item.is_file()):
        _fsync_file(path)
    _fsync_directory(staging_path)
    os.rename(staging_path, final_path)
    _fsync_directory(final_path.parent)


def run_backup(ctx: ServiceContext, environment: str) -> BackupResult:
    cfg = ctx.project.config
    env = cfg.env(environment)
    created_at = _utc_now()
    backup_root = ensure_dir(ctx.project.backups_dir)
    backup_id, staging_dir, backup_dir = _reserve_backup_staging(
        backup_root,
        environment,
        created_at=created_at,
    )
    pg = (
        make_context_db_adapter(ctx.project)
        if cfg.runtime.execution_mode == "docker"
        else PostgresAdapter(cfg.postgres)
    )
    fs = make_filestore_adapter(ctx.project, env) if env.filestore_volume else FilestoreAdapter()
    try:
        pg.dump(env.db_name, staging_dir / "db.dump")
        filestore_path = (
            env.filestore_path
            if env.filestore_volume
            else str(ctx.project.resolve_path(env.filestore_path))
        )
        fs.archive(filestore_path, staging_dir / "filestore.tar")
        if ctx.project.odoo_config_path.exists():
            text = ctx.project.odoo_config_path.read_text()
            _write_text_synced(
                staging_dir / "odoo.conf.redacted",
                redact_config_snapshot(text),
            )
        commit = git_commit(ctx.project.root)
        _write_text_synced(
            staging_dir / "git_commit.txt",
            commit or "unknown",
        )
        _write_text_synced(
            staging_dir / "docker_image.txt",
            cfg.odoo.image,
        )
        remote_cfg = cfg.backups.remote
        if remote_cfg is not None and remote_cfg.policy != "disabled":
            from odooctl.services.remote_backup import remote_backup_uri

            initial_remote_uri = remote_backup_uri(
                remote_cfg,
                backup_id,
                project=cfg.project.name,
            )
            initial_remote_status = "pending"
        else:
            initial_remote_uri = None
            initial_remote_status = "disabled"
        manifest = BackupManifest(
            backup_id=backup_id,
            project=cfg.project.name,
            environment=environment,
            timestamp=created_at.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
            db_name=env.db_name,
            filestore_path=env.filestore_path,
            artifact_paths=["db.dump", "filestore.tar"],
            backup_mode="full",
            git_commit=commit,
            docker_image=cfg.odoo.image,
            odoo_version=cfg.project.odoo_version,
            checksums={
                "db_dump": _sha256_file(staging_dir / "db.dump"),
                "filestore": _sha256_file(staging_dir / "filestore.tar"),
            },
            encryption=remote_encryption_metadata(ctx),
            remote_uri=initial_remote_uri,
            remote_status=initial_remote_status,
        )
        _write_text_synced(
            staging_dir / "manifest.json",
            manifest.model_dump_json(indent=2),
        )
        _validate_staged_backup(staging_dir, manifest)
        _publish_staged_backup(staging_dir, backup_dir)
    except Exception:
        if staging_dir.exists():
            _remove_tree(staging_dir)
        raise

    metadata_store = MetadataStore(ctx.project.state_dir)
    metadata_store.save_backup_manifest(backup_id, manifest)
    remote_manifest = manifest
    remote_uri: str | None = None
    remote_status = "disabled"
    remote_error: str | None = None
    remote_failure: Exception | None = None
    remote_adapter = None
    if cfg.backups.remote is not None:
        from odooctl.services.remote_backup import (
            RemoteBackupPolicyError,
            make_remote_adapter,
            publish_remote_backup,
        )

        if cfg.backups.remote.policy != "disabled":
            remote_adapter = make_remote_adapter(ctx)
        try:
            remote_result = publish_remote_backup(
                ctx,
                backup_dir,
                manifest,
                adapter=remote_adapter,
            )
            remote_manifest = remote_result.manifest
            remote_uri = remote_result.uri
            remote_status = remote_result.status
            remote_error = remote_result.error
        except RemoteBackupPolicyError as exc:
            remote_failure = exc
            remote_manifest = BackupManifest.model_validate_json(
                (backup_dir / "manifest.json").read_text()
            )
            remote_uri = remote_manifest.remote_uri
            remote_status = remote_manifest.remote_status
            remote_error = remote_manifest.remote_error

    prune_backups_gfs(
        ctx.project.backups_dir,
        environment=environment,
        daily=cfg.backups.retention.daily,
        weekly=cfg.backups.retention.weekly,
        monthly=cfg.backups.retention.monthly,
        project=cfg.project.name,
        metadata_store=metadata_store,
        protected_backup_ids=(backup_id,),
        latest_backup_id=backup_id,
        grace_hours=getattr(
            cfg.backups.retention,
            "grace_hours",
            1,
        ),
    )
    if (
        remote_failure is None
        and remote_adapter is not None
        and remote_status == "complete"
        and remote_manifest.remote_verified_at is not None
    ):
        from odooctl.services.remote_backup import (
            RemoteBackupPolicyError,
            prune_remote_backups,
            record_remote_retention_error,
        )

        try:
            prune_result = prune_remote_backups(
                ctx,
                environment=environment,
                adapter=remote_adapter,
                protected_backup_ids=(backup_id,),
            )
            if prune_result.error:
                remote_error = f"Remote retention incomplete: {prune_result.error}"
                remote_manifest = record_remote_retention_error(
                    ctx,
                    backup_dir,
                    remote_manifest,
                    remote_error,
                )
        except RemoteBackupPolicyError as exc:
            remote_error = str(exc)
            remote_manifest = record_remote_retention_error(
                ctx,
                backup_dir,
                remote_manifest,
                remote_error,
            )
            remote_failure = exc

    if remote_failure is not None:
        raise remote_failure
    return BackupResult(
        backup_id=backup_id,
        remote_uri=remote_uri,
        remote_status=remote_status,
        remote_error=remote_error,
    )


@dataclass
class BackupVerifyResult:
    ok: bool
    backup_id: str
    error: str | None = None


def verify_backup(
    backups_root: Path,
    backup_id: str,
    *,
    environment: str | None = None,
) -> BackupVerifyResult:
    """Verify backup integrity by re-checking manifest checksums.

    Pass *backup_id* = "latest" with *environment* to resolve the most recent
    backup for that environment first.
    """
    from odooctl.services.restore import resolve_backup_dir, validate_backup_dir

    if backup_id == "latest":
        if environment is None:
            return BackupVerifyResult(
                ok=False, backup_id="latest", error="'latest' requires environment= to be specified"
            )
        try:
            backup_dir = resolve_backup_dir(environment, "latest", backups_root)
        except RuntimeError as exc:
            return BackupVerifyResult(ok=False, backup_id="latest", error=str(exc))
        resolved_id = backup_dir.name
    else:
        backup_dir = backups_root / backup_id
        resolved_id = backup_id

    try:
        validate_backup_dir(backup_dir)
        return BackupVerifyResult(ok=True, backup_id=resolved_id)
    except Exception as exc:
        return BackupVerifyResult(ok=False, backup_id=resolved_id, error=str(exc))
