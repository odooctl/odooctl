"""Policy-aware publication, verification, download, and pruning of S3 backups."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from odooctl.adapters.s3 import (
    ABANDONMENT_MARKER,
    S3Adapter,
    S3IntegrityError,
    S3ObjectInfo,
    configured_s3_secret_values,
)
from odooctl.config import RemoteBackupConfig
from odooctl.metadata.models import BackupManifest
from odooctl.metadata.store import MetadataStore
from odooctl.security.redaction import redact_text

if TYPE_CHECKING:
    from odooctl.services.context import ServiceContext


class RemoteBackupPolicyError(RuntimeError):
    """A required remote-backup action failed after local backup publication."""


class RemoteBackupOwnershipError(S3IntegrityError):
    """A remote marker belongs to another project or environment."""


@dataclass(frozen=True)
class RemotePublishResult:
    manifest: BackupManifest
    uri: str | None
    status: str
    error: str | None = None


@dataclass(frozen=True)
class RemoteVerifyResult:
    backup_id: str
    uri: str
    object_count: int
    verified_at: str
    manifest: BackupManifest


@dataclass(frozen=True)
class RemotePruneResult:
    deleted_backup_ids: tuple[str, ...]
    error: str | None = None


@dataclass(frozen=True)
class _OwnedRemoteCandidate:
    backup_id: str
    adapter: S3Adapter
    manifest: BackupManifest


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remote_config(ctx: ServiceContext) -> RemoteBackupConfig:
    remote = ctx.project.config.backups.remote
    if remote is None:
        raise RuntimeError("Remote backups are not configured")
    if remote.policy == "disabled":
        raise RuntimeError("Remote backups are disabled by policy")
    return remote


def make_remote_adapter(ctx: ServiceContext) -> S3Adapter:
    remote = _remote_config(ctx)
    return S3Adapter(
        _project_scoped_config(
            remote,
            ctx.project.config.project.name,
        ),
        root=ctx.project.state_dir / "remote-backups",
    )


def _project_namespace(project: str) -> str:
    slug = (
        re.sub(
            r"[^a-z0-9-]+",
            "-",
            project.lower(),
        ).strip("-")[:32]
        or "project"
    )
    digest = hashlib.sha256(project.encode()).hexdigest()[:24]
    return f"projects/{slug}-{digest}"


def _project_scoped_config(
    remote: RemoteBackupConfig,
    project: str,
) -> RemoteBackupConfig:
    base = remote.prefix.strip("/")
    namespace = _project_namespace(project)
    return remote.model_copy(update={"prefix": (f"{base}/{namespace}" if base else namespace)})


def _legacy_remote_adapter(ctx: ServiceContext) -> S3Adapter:
    remote = _remote_config(ctx)
    return S3Adapter(
        remote,
        root=ctx.project.state_dir / "remote-backups",
    )


def remote_backup_uri(
    remote: RemoteBackupConfig,
    backup_id: str,
    *,
    project: str | None = None,
) -> str:
    if not remote.bucket:
        raise RuntimeError("Remote backup bucket is required")
    effective = _project_scoped_config(remote, project) if project is not None else remote
    prefix = effective.prefix.strip("/")
    key = f"{prefix}/{backup_id}" if prefix else backup_id
    return f"s3://{remote.bucket}/{key}"


def _adapter_uri(
    remote: RemoteBackupConfig,
    adapter: object,
    backup_id: str,
    *,
    project: str,
) -> str:
    adapter_config = getattr(adapter, "config", None)
    if isinstance(adapter_config, RemoteBackupConfig):
        return remote_backup_uri(adapter_config, backup_id)
    return remote_backup_uri(
        remote,
        backup_id,
        project=project,
    )


def _secret_values(remote: RemoteBackupConfig) -> tuple[str, ...]:
    return configured_s3_secret_values(remote)


def _safe_error(remote: RemoteBackupConfig, exc: BaseException) -> str:
    return redact_text(str(exc), _secret_values(remote))


def _write_atomic(path: Path, payload: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_tree(root: Path) -> None:
    directories = [root]
    for path in root.rglob("*"):
        if path.is_symlink():
            raise S3IntegrityError(f"Downloaded backup contains a symbolic link: {path}")
        if path.is_file():
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        elif path.is_dir():
            directories.append(path)
        else:
            raise S3IntegrityError(f"Downloaded backup contains a non-regular path: {path}")
    for directory in sorted(
        directories,
        key=lambda candidate: len(candidate.parts),
        reverse=True,
    ):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _last_modified_utc(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _scoped_object_groups(
    client: S3Adapter,
) -> dict[str, list[S3ObjectInfo]]:
    """Group every object in this adapter's already project-scoped prefix."""

    probe = "odooctl-scope-probe"
    probe_suffix = f"{probe}/manifest.json"
    probe_key = client.manifest_key(probe)
    if not probe_key.endswith(probe_suffix):
        raise S3IntegrityError("Remote adapter returned an invalid project scope")
    scope_prefix = probe_key[: -len(probe_suffix)]
    grouped: dict[str, list[S3ObjectInfo]] = {}
    for item in client.list_objects():
        if not item.key.startswith(scope_prefix):
            raise S3IntegrityError("Remote adapter listed an object outside the project scope")
        relative = item.key[len(scope_prefix) :]
        parts = relative.split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            # Never infer ownership of loose objects directly under the scope.
            continue
        backup_id = parts[0]
        try:
            expected_prefix = client.manifest_key(backup_id).removesuffix("manifest.json")
        except Exception:
            continue
        if not item.key.startswith(expected_prefix):
            continue
        grouped.setdefault(backup_id, []).append(item)
    return grouped


def _mark_remote_abandoned(
    ctx: ServiceContext,
    client: S3Adapter,
    manifest: BackupManifest,
) -> None:
    """Publish explicit authority to clean an incomplete, globally unique id."""

    marker = {
        "schema_version": 1,
        "status": "abandoned",
        "backup_id": manifest.backup_id,
        "project": manifest.project,
        "environment": manifest.environment,
        "abandoned_at": _now_utc(),
        "cleanup_token": uuid.uuid4().hex,
    }
    staging_dir = ctx.project.state_dir / "remote-abandonment"
    staging_dir.mkdir(parents=True, exist_ok=True)
    path = staging_dir / f".{manifest.backup_id}.{uuid.uuid4().hex}.json"
    try:
        _write_atomic(path, json.dumps(marker, indent=2))
        uploaded = client.upload_object(
            path,
            client.abandonment_key(manifest.backup_id),
        )
        client.verify_object_content(
            uploaded.key,
            expected_sha256=_sha256_file(path),
        )
    finally:
        path.unlink(missing_ok=True)


def _mark_incomplete_for_cleanup(
    ctx: ServiceContext,
    client: S3Adapter,
    manifest: BackupManifest,
) -> None:
    objects = client.list_objects(manifest.backup_id)
    keys = {item.key for item in objects}
    if not objects or client.manifest_key(manifest.backup_id) in keys:
        return
    if client.abandonment_key(manifest.backup_id) in keys:
        return
    _mark_remote_abandoned(ctx, client, manifest)


def _load_abandonment_marker(
    ctx: ServiceContext,
    client: S3Adapter,
    *,
    backup_id: str,
    environment: str,
) -> str:
    marker_key = client.abandonment_key(backup_id)
    path = (
        ctx.project.state_dir
        / "remote-verification"
        / f".{backup_id}.{uuid.uuid4().hex}.abandoned"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        info = client.download_object(marker_key, path)
        raw = json.loads(path.read_text())
        if (
            not isinstance(raw, dict)
            or raw.get("status") != "abandoned"
            or raw.get("backup_id") != backup_id
            or raw.get("project") != ctx.project.config.project.name
            or raw.get("environment") != environment
            or not isinstance(raw.get("cleanup_token"), str)
            or not raw["cleanup_token"]
        ):
            raise S3IntegrityError("Remote abandonment marker identity is invalid")
        return info.sha256 or _sha256_file(path)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise S3IntegrityError("Remote abandonment marker is invalid") from exc
    finally:
        path.unlink(missing_ok=True)


def _persist_local_manifest(
    ctx: ServiceContext,
    backup_dir: Path,
    manifest: BackupManifest,
) -> None:
    if backup_dir.name != manifest.backup_id:
        raise RuntimeError("Local backup directory and manifest identity do not match")
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise RuntimeError(f"Local backup manifest is missing: {manifest_path}")
    _write_atomic(
        manifest_path,
        manifest.model_dump_json(indent=2),
    )
    MetadataStore(ctx.project.state_dir).update_backup_manifest(manifest)


def _object_map(
    adapter: S3Adapter,
    manifest: BackupManifest,
    objects: Iterable[S3ObjectInfo],
) -> dict[str, S3ObjectInfo]:
    marker_key = adapter.manifest_key(manifest.backup_id)
    prefix = marker_key.removesuffix("manifest.json")
    mapped: dict[str, S3ObjectInfo] = {}
    for item in objects:
        if not item.key.startswith(prefix):
            raise S3IntegrityError(f"Remote object {item.key!r} escaped backup scope")
        relative = item.key[len(prefix) :]
        if not relative or relative in mapped:
            raise S3IntegrityError(
                f"Remote backup contains an invalid or duplicate object: {item.key!r}"
            )
        mapped[relative] = item
    return mapped


def _verify_payload_object_set(
    adapter: S3Adapter,
    manifest: BackupManifest,
    objects: Iterable[S3ObjectInfo],
) -> dict[str, S3ObjectInfo]:
    mapped = _object_map(adapter, manifest, objects)
    required = {
        "db.dump": manifest.checksums.get("db_dump"),
        "filestore.tar": manifest.checksums.get("filestore"),
    }
    for relative, expected_sha256 in required.items():
        if not expected_sha256:
            raise S3IntegrityError(f"Remote manifest has no checksum for {relative}")
        item = mapped.get(relative)
        if item is None:
            raise S3IntegrityError(f"Remote backup is missing required object {relative}")
        if item.sha256 != expected_sha256.lower():
            raise S3IntegrityError(f"Remote object {relative} does not match the backup manifest")
    return mapped


def _verify_manifest_object_set(
    adapter: S3Adapter,
    manifest: BackupManifest,
    objects: Iterable[S3ObjectInfo],
    *,
    manifest_sha256: str | None = None,
) -> dict[str, S3ObjectInfo]:
    mapped = _verify_payload_object_set(
        adapter,
        manifest,
        objects,
    )
    if ABANDONMENT_MARKER in mapped:
        raise S3IntegrityError("Remote backup is marked abandoned")
    marker = mapped.get("manifest.json")
    if marker is None:
        raise S3IntegrityError("Remote backup has no manifest completion marker")
    if manifest_sha256 is not None and marker.sha256 != manifest_sha256:
        raise S3IntegrityError("Remote manifest object does not match the expected final manifest")
    return mapped


def _stage_manifest(backup_dir: Path, manifest: BackupManifest) -> Path:
    path = backup_dir / f".manifest-{uuid.uuid4().hex}.remote"
    with path.open("x") as handle:
        handle.write(manifest.model_dump_json(indent=2))
        handle.flush()
        os.fsync(handle.fileno())
    return path


def publish_remote_backup(
    ctx: ServiceContext,
    backup_dir: Path,
    manifest: BackupManifest,
    *,
    adapter: S3Adapter | None = None,
) -> RemotePublishResult:
    """Publish a local backup with a fail-closed remote completion marker."""
    remote = ctx.project.config.backups.remote
    if remote is None or remote.policy == "disabled":
        disabled = manifest.model_copy(
            update={
                "remote_uri": None,
                "remote_status": "disabled",
                "remote_verified_at": None,
                "remote_error": None,
            }
        )
        if disabled != manifest:
            _persist_local_manifest(ctx, backup_dir, disabled)
        return RemotePublishResult(
            manifest=disabled,
            uri=None,
            status="disabled",
        )

    client = adapter or make_remote_adapter(ctx)
    uri = _adapter_uri(
        remote,
        client,
        manifest.backup_id,
        project=ctx.project.config.project.name,
    )
    pending = manifest.model_copy(
        update={
            "remote_uri": uri,
            "remote_status": "pending",
            "remote_verified_at": None,
            "remote_error": None,
        }
    )
    _persist_local_manifest(ctx, backup_dir, pending)
    staged_manifest: Path | None = None
    try:
        # The abandonment marker is a permanent publication fence. Probe its
        # exact key directly; LIST is inventory, not an authority check.
        client.assert_not_abandoned(manifest.backup_id)
        payload_objects = client.upload_backup_payload(backup_dir)
        if remote.verify_after_upload:
            verified_payload = [
                client.verify_object_content(
                    item.key,
                    expected_sha256=item.sha256,
                )
                for item in payload_objects
            ]
        else:
            verified_payload = payload_objects
        _verify_payload_object_set(
            client,
            pending,
            verified_payload,
        )
        verified_at = _now_utc() if remote.verify_after_upload else None
        complete = pending.model_copy(
            update={
                "remote_status": "complete",
                "remote_verified_at": verified_at,
                "remote_error": None,
            }
        )
        staged_manifest = _stage_manifest(backup_dir, complete)
        marker = client.upload_manifest(
            staged_manifest,
            backup_name=manifest.backup_id,
        )
        expected_manifest_sha256 = _sha256_file(staged_manifest)
        if marker.sha256 != expected_manifest_sha256:
            raise S3IntegrityError("Published completion marker does not match the final manifest")
        if remote.verify_after_upload:
            client.verify_object_content(
                marker.key,
                expected_sha256=expected_manifest_sha256,
            )
        _persist_local_manifest(ctx, backup_dir, complete)
        return RemotePublishResult(
            manifest=complete,
            uri=uri,
            status="complete",
        )
    except Exception as exc:
        cleanup_error: str | None = None
        abandonment_error: str | None = None
        try:
            # A marker published before a later verification failure must not
            # continue advertising the remote backup as complete.
            client.remove_completion_marker(manifest.backup_id)
        except Exception as cleanup_exc:
            cleanup_error = _safe_error(remote, cleanup_exc)
        try:
            _mark_incomplete_for_cleanup(
                ctx,
                client,
                pending,
            )
        except Exception as marker_exc:
            abandonment_error = _safe_error(remote, marker_exc)
        detail = _safe_error(remote, exc)
        if cleanup_error:
            detail = f"{detail}; completion marker cleanup failed: {cleanup_error}"
        if abandonment_error:
            detail = (
                f"{detail}; abandonment marker publication failed: "
                f"{abandonment_error}"
            )
        failure_status = "failed" if remote.policy == "required" else "degraded"
        failed = pending.model_copy(
            update={
                "remote_status": failure_status,
                "remote_verified_at": None,
                "remote_error": detail,
            }
        )
        _persist_local_manifest(ctx, backup_dir, failed)
        if remote.policy == "required":
            raise RemoteBackupPolicyError(
                f"Required remote backup failed for {manifest.backup_id}: {detail}"
            ) from exc
        return RemotePublishResult(
            manifest=failed,
            uri=uri,
            status=failure_status,
            error=detail,
        )
    finally:
        if staged_manifest is not None:
            staged_manifest.unlink(missing_ok=True)


def list_remote_backups(
    ctx: ServiceContext,
    environment: str,
    *,
    adapter: S3Adapter | None = None,
) -> list[str]:
    ctx.project.config.env(environment)
    clients = [adapter] if adapter is not None else _remote_read_adapters(ctx)
    inventories = [(client, client.list_backups()) for client in clients]
    return sorted(
        {
            candidate.backup_id
            for candidate in _owned_remote_candidates(
                ctx,
                environment,
                inventories,
            )
        },
        reverse=True,
    )


def _remote_read_adapters(ctx: ServiceContext) -> list[S3Adapter]:
    scoped = make_remote_adapter(ctx)
    legacy = _legacy_remote_adapter(ctx)
    if scoped.config.prefix == legacy.config.prefix:
        return [scoped]
    return [scoped, legacy]


def _resolve_remote_location(
    ctx: ServiceContext,
    environment: str,
    backup_id: str,
    *,
    adapter: S3Adapter | None = None,
) -> tuple[str, S3Adapter]:
    ctx.project.config.env(environment)
    prefix = f"{environment}_"
    inventories = [
        (client, set(client.list_backups()))
        for client in ([adapter] if adapter is not None else _remote_read_adapters(ctx))
    ]
    if backup_id != "latest":
        if not backup_id.startswith(prefix):
            raise RuntimeError(f"Remote backup {backup_id!r} does not belong to {environment!r}")
        for client, available in inventories:
            if backup_id in available:
                # Explicit IDs stay diagnostic: a foreign marker is an
                # ownership error, not an indistinguishable "not found".
                _load_remote_manifest(
                    ctx,
                    client,
                    environment,
                    backup_id,
                )
                return backup_id, client
        raise RuntimeError(f"Remote backup not found: {backup_id}")

    owned_by_id: dict[str, _OwnedRemoteCandidate] = {}
    for candidate in _owned_remote_candidates(
        ctx,
        environment,
        inventories,
    ):
        # Project-scoped storage is inspected before the legacy prefix. Keep
        # that precedence if the same owned ID exists in both locations.
        owned_by_id.setdefault(candidate.backup_id, candidate)
    if not owned_by_id:
        raise RuntimeError(f"No complete remote backups found for {environment}")
    try:
        selected = max(
            owned_by_id.values(),
            key=lambda candidate: (
                _manifest_timestamp_utc(candidate.manifest),
                candidate.backup_id,
            ),
        )
    except ValueError as exc:
        raise S3IntegrityError(
            f"Remote completion manifest has an invalid timestamp: {exc}"
        ) from exc
    return selected.backup_id, selected.adapter


def resolve_remote_backup_id(
    ctx: ServiceContext,
    environment: str,
    backup_id: str,
    *,
    adapter: S3Adapter | None = None,
) -> str:
    resolved, _ = _resolve_remote_location(
        ctx,
        environment,
        backup_id,
        adapter=adapter,
    )
    return resolved


def _load_remote_manifest(
    ctx: ServiceContext,
    adapter: S3Adapter,
    environment: str,
    backup_id: str,
) -> tuple[BackupManifest, str]:
    temporary = (
        ctx.project.state_dir / "remote-verification" / f".{backup_id}.{uuid.uuid4().hex}.manifest"
    )
    temporary.parent.mkdir(parents=True, exist_ok=True)
    try:
        adapter.assert_not_abandoned(backup_id)
        adapter.download_manifest(backup_id, temporary)
        adapter.assert_not_abandoned(backup_id)
        raw = json.loads(temporary.read_text())
        if not isinstance(raw, dict):
            raise S3IntegrityError("Remote completion manifest is not a JSON object")
        if raw.get("backup_id") != backup_id:
            raise S3IntegrityError(
                "Remote manifest backup identity does not match its object prefix"
            )
        if raw.get("environment") != environment:
            raise RemoteBackupOwnershipError(
                "Remote manifest environment does not match the requested environment"
            )
        if raw.get("project") != ctx.project.config.project.name:
            raise RemoteBackupOwnershipError("Remote manifest belongs to a different project")
        manifest = BackupManifest.model_validate(raw)
        if manifest.status != "complete":
            raise S3IntegrityError("Remote manifest does not describe a complete local backup")
        # Manifests created before R3 have no remote_status field. They remain
        # verifiable and are upgraded locally after a successful byte check.
        if "remote_status" in raw and manifest.remote_status != "complete":
            raise S3IntegrityError(
                f"Remote manifest status is {manifest.remote_status!r}, not complete"
            )
        return manifest, _sha256_file(temporary)
    except (json.JSONDecodeError, ValueError) as exc:
        raise S3IntegrityError(f"Remote completion manifest is invalid: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _owned_remote_candidates(
    ctx: ServiceContext,
    environment: str,
    inventories: Iterable[tuple[S3Adapter, Iterable[str]]],
) -> list[_OwnedRemoteCandidate]:
    """Return completed markers whose manifests belong to this project/env."""

    prefix = f"{environment}_"
    candidates: list[_OwnedRemoteCandidate] = []
    for client, backup_ids in inventories:
        for backup_id in backup_ids:
            if not backup_id.startswith(prefix):
                continue
            if client.abandonment_fence_exists(backup_id):
                continue
            try:
                manifest, _ = _load_remote_manifest(
                    ctx,
                    client,
                    environment,
                    backup_id,
                )
            except RemoteBackupOwnershipError:
                # Legacy prefixes may legitimately be shared by multiple
                # projects. Discovery must not leak their marker names.
                continue
            candidates.append(
                _OwnedRemoteCandidate(
                    backup_id=backup_id,
                    adapter=client,
                    manifest=manifest,
                )
            )
    return candidates


def _manifest_timestamp_utc(manifest: BackupManifest) -> datetime:
    parsed = datetime.fromisoformat(manifest.timestamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _persist_verified_local_copy(
    ctx: ServiceContext,
    manifest: BackupManifest,
    *,
    uri: str,
    verified_at: str,
) -> BackupManifest:
    backup_dir = ctx.project.backups_dir / manifest.backup_id
    if not backup_dir.is_dir() or backup_dir.is_symlink():
        return manifest
    local = BackupManifest.model_validate_json((backup_dir / "manifest.json").read_text())
    if (
        local.backup_id != manifest.backup_id
        or local.environment != manifest.environment
        or local.project != manifest.project
        or local.checksums != manifest.checksums
        or local.artifact_paths != manifest.artifact_paths
    ):
        raise S3IntegrityError(
            "Local and remote backup manifests do not identify the same artifacts"
        )
    updated = local.model_copy(
        update={
            "remote_uri": uri,
            "remote_status": "complete",
            "remote_verified_at": verified_at,
            "remote_error": None,
        }
    )
    _persist_local_manifest(ctx, backup_dir, updated)
    return updated


def verify_remote_backup(
    ctx: ServiceContext,
    environment: str,
    backup_id: str = "latest",
    *,
    adapter: S3Adapter | None = None,
    link_local: bool = True,
) -> RemoteVerifyResult:
    remote = _remote_config(ctx)
    resolved, client = _resolve_remote_location(
        ctx,
        environment,
        backup_id,
        adapter=adapter,
    )
    uri = _adapter_uri(
        remote,
        client,
        resolved,
        project=ctx.project.config.project.name,
    )
    try:
        manifest, manifest_sha256 = _load_remote_manifest(
            ctx,
            client,
            environment,
            resolved,
        )
        objects = client.verify_backup(resolved)
        _verify_manifest_object_set(
            client,
            manifest,
            objects,
            manifest_sha256=manifest_sha256,
        )
        client.assert_not_abandoned(resolved)
        if manifest.remote_uri is not None and manifest.remote_uri != uri:
            raise S3IntegrityError("Remote manifest URI does not match the configured destination")
        verified_at = _now_utc()
        updated = (
            _persist_verified_local_copy(
                ctx,
                manifest,
                uri=uri,
                verified_at=verified_at,
            )
            if link_local
            else manifest.model_copy(
                update={
                    "remote_uri": uri,
                    "remote_status": "complete",
                    "remote_verified_at": verified_at,
                    "remote_error": None,
                }
            )
        )
        return RemoteVerifyResult(
            backup_id=resolved,
            uri=uri,
            object_count=len(objects),
            verified_at=verified_at,
            manifest=updated,
        )
    except Exception as exc:
        backup_dir = ctx.project.backups_dir / resolved
        if link_local and backup_dir.is_dir() and not backup_dir.is_symlink():
            try:
                local = BackupManifest.model_validate_json(
                    (backup_dir / "manifest.json").read_text()
                )
                failed = local.model_copy(
                    update={
                        "remote_uri": uri,
                        "remote_status": ("failed" if remote.policy == "required" else "degraded"),
                        "remote_verified_at": None,
                        "remote_error": _safe_error(remote, exc),
                    }
                )
                _persist_local_manifest(ctx, backup_dir, failed)
            except Exception:
                pass
        raise


def download_remote_backup(
    ctx: ServiceContext,
    environment: str,
    backup_id: str = "latest",
    *,
    destination_root: Path | None = None,
    adapter: S3Adapter | None = None,
) -> Path:
    resolved, client = _resolve_remote_location(
        ctx,
        environment,
        backup_id,
        adapter=adapter,
    )
    verified = verify_remote_backup(
        ctx,
        environment,
        resolved,
        adapter=client,
        link_local=False,
    )
    root = Path(destination_root or ctx.project.backups_dir)
    if root.exists() and (not root.is_dir() or root.is_symlink()):
        raise RuntimeError(f"Remote download root is not a real directory: {root}")
    root.mkdir(parents=True, exist_ok=True)
    target = root / verified.backup_id
    if target.exists():
        raise RuntimeError(f"Remote download target already exists: {target}")
    staging_root = root / f".remote-download-{verified.backup_id}-{uuid.uuid4().hex}"
    staging_root.mkdir()
    staged_target: Path | None = None
    try:
        from odooctl.services.restore import validate_backup_dir

        staged_target = client.download_backup(
            verified.backup_id,
            staging_root,
        )
        validate_backup_dir(staged_target)
        manifest = BackupManifest.model_validate_json((staged_target / "manifest.json").read_text())
        if (
            manifest.backup_id != verified.backup_id
            or manifest.environment != environment
            or manifest.project != ctx.project.config.project.name
        ):
            raise S3IntegrityError(
                "Downloaded backup identity does not match the requested project/environment"
            )
        updated = manifest.model_copy(
            update={
                "remote_uri": verified.uri,
                "remote_status": "complete",
                "remote_verified_at": verified.verified_at,
                "remote_error": None,
            }
        )
        _write_atomic(
            staged_target / "manifest.json",
            updated.model_dump_json(indent=2),
        )
        _fsync_tree(staged_target)
        client.assert_not_abandoned(verified.backup_id)
        os.replace(staged_target, target)
        directory_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if root.resolve() == ctx.project.backups_dir.resolve():
            store = MetadataStore(ctx.project.state_dir)
            current_raw = store.latest_backup(environment)
            try:
                current = (
                    BackupManifest.model_validate(current_raw)
                    if current_raw is not None
                    else None
                )
            except ValueError:
                current = None
            if current is None or (
                _manifest_timestamp_utc(updated),
                updated.backup_id,
            ) > (
                _manifest_timestamp_utc(current),
                current.backup_id,
            ):
                store.save_backup_manifest(updated.backup_id, updated)
            else:
                store.update_backup_manifest(updated)
        return target
    finally:
        if staging_root.is_dir() and not staging_root.is_symlink():
            shutil.rmtree(staging_root)


def prune_remote_backups(
    ctx: ServiceContext,
    *,
    environment: str,
    adapter: S3Adapter | None = None,
    protected_backup_ids: Iterable[str] = (),
) -> RemotePruneResult:
    """Reconcile owned remote backups against configured UTC GFS retention."""
    remote = _remote_config(ctx)
    client = adapter or make_remote_adapter(ctx)
    ctx.project.config.env(environment)
    deleted: list[str] = []
    errors: list[str] = []
    attempted_deletes: set[str] = set()
    prefix = f"{environment}_"
    candidates: list[BackupManifest] = []
    try:
        object_inventory = {
            item.key: item
            for item in client.list_objects()
        }
        completed_backup_ids = client.list_backups()
    except Exception as exc:
        detail = f"remote inventory: {_safe_error(remote, exc)}"
        if remote.policy == "required":
            raise RemoteBackupPolicyError(
                f"Required remote retention failed: {detail}"
            ) from exc
        return RemotePruneResult(
            deleted_backup_ids=(),
            error=detail,
        )
    completion_times: dict[str, datetime | None] = {}
    for backup_id in completed_backup_ids:
        if not backup_id.startswith(prefix):
            continue
        try:
            if client.abandonment_fence_exists(backup_id):
                continue
            manifest, _ = _load_remote_manifest(
                ctx,
                client,
                environment,
                backup_id,
            )
            candidates.append(manifest)
            marker = object_inventory.get(
                client.manifest_key(backup_id)
            )
            completion_times[backup_id] = (
                _last_modified_utc(marker.last_modified)
                if marker is not None
                else None
            )
        except RemoteBackupOwnershipError:
            # Shared prefixes may contain another project. Presence never
            # grants authority to delete it and need not break this project.
            continue
        except Exception as exc:
            errors.append(f"{backup_id}: {_safe_error(remote, exc)}")

    def parsed_timestamp(manifest: BackupManifest) -> datetime:
        try:
            value = datetime.fromisoformat(manifest.timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise S3IntegrityError(
                f"Remote backup {manifest.backup_id!r} has an invalid timestamp"
            ) from exc
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    try:
        ordered = sorted(
            candidates,
            key=lambda item: (
                parsed_timestamp(item),
                item.backup_id,
            ),
            reverse=True,
        )
    except S3IntegrityError as exc:
        ordered = []
        errors.append(_safe_error(remote, exc))

    retained: set[str] = set(protected_backup_ids)
    if ordered:
        retained.add(ordered[0].backup_id)

    def retain_periods(count: int, bucket_for) -> None:
        periods: set[object] = set()
        for manifest in ordered:
            period = bucket_for(parsed_timestamp(manifest))
            if period in periods:
                continue
            periods.add(period)
            retained.add(manifest.backup_id)
            if len(periods) >= count:
                break

    policy = ctx.project.config.backups.retention
    if policy.daily:
        retain_periods(policy.daily, lambda value: value.date())
    if policy.weekly:
        retain_periods(
            policy.weekly,
            lambda value: (
                value.isocalendar().year,
                value.isocalendar().week,
            ),
        )
    if policy.monthly:
        retain_periods(
            policy.monthly,
            lambda value: (value.year, value.month),
        )
    reconciliation_time = datetime.now(timezone.utc)
    completed_grace_cutoff = reconciliation_time - timedelta(
        hours=policy.grace_hours
    )
    retained.update(
        manifest.backup_id
        for manifest in ordered
        if completion_times.get(manifest.backup_id) is None
        or completion_times[manifest.backup_id]
        >= completed_grace_cutoff
    )

    for manifest in reversed(ordered):
        if manifest.backup_id in retained:
            continue
        try:
            # Re-check ownership immediately before the destructive call.
            current, _ = _load_remote_manifest(
                ctx,
                client,
                environment,
                manifest.backup_id,
            )
            if (
                current.project != manifest.project
                or current.environment != manifest.environment
                or current.timestamp != manifest.timestamp
                or current.checksums != manifest.checksums
            ):
                raise RemoteBackupOwnershipError(
                    "Remote backup changed during retention reconciliation"
                )
            attempted_deletes.add(manifest.backup_id)
            client.delete_backup(manifest.backup_id)
            deleted.append(manifest.backup_id)
        except RemoteBackupOwnershipError:
            continue
        except Exception as exc:
            try:
                _mark_incomplete_for_cleanup(
                    ctx,
                    client,
                    manifest,
                )
            except Exception as marker_exc:
                errors.append(
                    f"{manifest.backup_id} abandonment marker: "
                    f"{_safe_error(remote, marker_exc)}"
                )
            errors.append(f"{manifest.backup_id}: {_safe_error(remote, exc)}")

    # Marker-first deletion and failed uploads can leave payload-only prefixes.
    # Automatic cleanup requires an explicit abandonment marker written by the
    # operation that stopped owning the globally unique backup id. Arbitrary
    # markerless payload is never deleted: it may still be an active publisher
    # on another host.
    cutoff = reconciliation_time - timedelta(
        hours=remote.orphan_grace_hours
    )
    try:
        groups = _scoped_object_groups(client)
    except Exception as exc:
        groups = {}
        errors.append(f"orphan inventory: {_safe_error(remote, exc)}")
    for backup_id, objects in sorted(groups.items()):
        if not backup_id.startswith(prefix):
            continue
        if backup_id in attempted_deletes:
            continue
        abandonment_key = client.abandonment_key(backup_id)
        marker_key = client.manifest_key(backup_id)
        object_keys = {item.key for item in objects}
        fence_was_in_inventory = abandonment_key in object_keys
        has_abandonment_fence = (
            fence_was_in_inventory
            or client.abandonment_fence_exists(backup_id)
        )
        if marker_key in object_keys and not has_abandonment_fence:
            continue
        if has_abandonment_fence and not fence_was_in_inventory:
            # The direct HEAD is authoritative for hiding the backup, but this
            # inventory has no trustworthy fence age for grace enforcement.
            continue
        modified = [_last_modified_utc(item.last_modified) for item in objects]
        if not modified or any(value is None for value in modified):
            # Unknown age is not authority to delete.
            continue
        if max(value for value in modified if value is not None) > cutoff:
            continue
        if not has_abandonment_fence:
            errors.append(
                f"{backup_id}: old markerless prefix has no abandonment "
                "marker; manual review required"
            )
            continue
        try:
            current = client.list_objects(backup_id)
            current_keys = {item.key for item in current}
            if not client.abandonment_fence_exists(backup_id):
                continue
            payload_keys = current_keys - {abandonment_key}
            if not payload_keys:
                # The durable fence is intentionally retained after every
                # payload object has been removed.
                continue
            current_modified = [_last_modified_utc(item.last_modified) for item in current]
            if (
                not current
                or any(value is None for value in current_modified)
                or max(value for value in current_modified if value is not None) > cutoff
            ):
                continue
            _load_abandonment_marker(
                ctx,
                client,
                backup_id=backup_id,
                environment=environment,
            )
            client.delete_backup(backup_id)
            if backup_id not in deleted:
                deleted.append(backup_id)
        except Exception as exc:
            errors.append(f"{backup_id} orphan cleanup: {_safe_error(remote, exc)}")
    detail = "; ".join(errors) or None
    if detail and remote.policy == "required":
        raise RemoteBackupPolicyError(f"Required remote retention failed: {detail}")
    return RemotePruneResult(
        deleted_backup_ids=tuple(deleted),
        error=detail,
    )


def record_remote_retention_error(
    ctx: ServiceContext,
    backup_dir: Path,
    manifest: BackupManifest,
    error: str,
) -> BackupManifest:
    """Record a retention alert without misclassifying a verified upload."""
    updated = manifest.model_copy(update={"remote_error": error})
    _persist_local_manifest(ctx, backup_dir, updated)
    return updated
