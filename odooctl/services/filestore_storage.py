"""Verified filestore backend inventory, migration, sync, and cutover."""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Iterator

from odooctl.adapters.filestore import (
    FilestoreAdapter,
    make_filestore_adapter,
)
from odooctl.adapters.object_filestore import (
    ObjectFilestoreAdapter,
    ObjectFilestoreVerification,
)
from odooctl.metadata.models import (
    FilestoreMigrationManifest,
    FilestoreObjectEntry,
    filestore_inventory_sha256,
    now_utc,
)
from odooctl.metadata.store import MetadataStore
from odooctl.security.redaction import redact_text

if TYPE_CHECKING:
    from odooctl.config import EnvironmentConfig, FilestoreBackendConfig
    from odooctl.services.context import ServiceContext


class FilestoreStorageError(RuntimeError):
    """A filestore inventory or migration failed closed."""


@dataclass(frozen=True)
class FilestoreBackendStatus:
    environment: str
    backend: str
    source_location: str
    target_location: str | None
    migration_count: int
    latest_migration_id: str | None
    latest_status: str | None


@dataclass(frozen=True)
class FilestoreVerifyResult:
    migration_id: str
    object_count: int
    total_size: int
    inventory_sha256: str
    verified_at: str


def _validated_update(model, **updates):
    payload = model.model_dump(mode="python")
    payload.update(updates)
    return type(model).model_validate(payload)


def _safe_error(exc: BaseException, ctx: "ServiceContext") -> str:
    values = [
        os.getenv(name, "")
        for name in ctx.project.config.referenced_env_vars()
    ]
    return redact_text(str(exc), tuple(value for value in values if value))


def _backend(
    ctx: "ServiceContext",
    environment: str,
) -> tuple["EnvironmentConfig", "FilestoreBackendConfig"]:
    env = ctx.project.config.env(environment)
    backend = env.filestore_backend
    if backend is None:
        raise FilestoreStorageError(
            "filestore migration requires an explicit filestore_backend "
            "configuration"
        )
    return env, backend


def _resolve_host_path(ctx: "ServiceContext", value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = ctx.project.root / candidate
    # Normalize to an absolute path without resolving the leaf symlink. Source
    # and target guards must be able to reject a configured symlink rather than
    # silently following it before ``is_symlink`` is checked.
    return Path(os.path.abspath(candidate))


def _source_description(
    ctx: "ServiceContext",
    env: "EnvironmentConfig",
    backend: "FilestoreBackendConfig",
) -> tuple[str, str]:
    if backend.source_path:
        return "local", str(_resolve_host_path(ctx, backend.source_path))
    if env.filestore_volume:
        return (
            "docker_volume",
            f"volume:{env.filestore_volume}:{env.filestore_path}",
        )
    # ``filestore_backend`` describes the migration target/integration mode.
    # Without an explicit Docker volume, the legacy filestore_path remains the
    # local source even when the target is a POSIX object mount.
    return "local", str(_resolve_host_path(ctx, env.filestore_path))


def _object_adapter(
    ctx: "ServiceContext",
    environment: str,
    backend: "FilestoreBackendConfig",
) -> ObjectFilestoreAdapter:
    if backend.object_store is None:
        raise FilestoreStorageError(
            f"filestore backend {backend.type} has no object_store"
        )
    return ObjectFilestoreAdapter(
        backend.object_store,
        project=ctx.project.config.project.name,
        environment=environment,
        state_dir=ctx.project.state_dir,
    )


def _target_location(
    ctx: "ServiceContext",
    environment: str,
    backend: "FilestoreBackendConfig",
    migration_id: str,
    *,
    object_adapter: ObjectFilestoreAdapter | None = None,
) -> str:
    if backend.type in {"object_mirror", "odoo_module"}:
        client = object_adapter or _object_adapter(
            ctx,
            environment,
            backend,
        )
        return client.uri_for_migration(migration_id)
    if backend.type == "posix_object_mount" and backend.mount_path:
        return str(_resolve_host_path(ctx, backend.mount_path))
    raise FilestoreStorageError(
        f"filestore backend {backend.type} is not a migration target"
    )


@contextmanager
def _materialized_source(
    ctx: "ServiceContext",
    env: "EnvironmentConfig",
    backend: "FilestoreBackendConfig",
) -> Iterator[Path]:
    if backend.source_path:
        source = _resolve_host_path(ctx, backend.source_path)
        if not source.is_dir() or source.is_symlink():
            raise FilestoreStorageError(
                f"filestore source is not a real directory: {source}"
            )
        yield source
        return
    if not env.filestore_volume:
        source = _resolve_host_path(ctx, env.filestore_path)
        if not source.is_dir() or source.is_symlink():
            raise FilestoreStorageError(
                f"filestore source is not a real directory: {source}"
            )
        yield source
        return

    ctx.project.state_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=ctx.project.state_dir,
        prefix=".filestore-source-",
    ) as temporary:
        root = Path(temporary)
        archive = root / "filestore.tar"
        volume = make_filestore_adapter(ctx.project, env)
        volume.archive(env.filestore_path, archive)
        extracted = root / "materialized" / Path(env.filestore_path).name
        FilestoreAdapter().restore_archive(archive, str(extracted))
        if not extracted.is_dir() or extracted.is_symlink():
            raise FilestoreStorageError(
                "Docker volume filestore did not materialize as a real directory"
            )
        yield extracted


def _safe_relative_path(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise FilestoreStorageError(
            f"filestore path escaped its root: {path}"
        ) from None
    value = PurePosixPath(*relative.parts).as_posix()
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or any(
            part in {"", ".", ".."}
            or any(ord(character) < 32 for character in part)
            for part in PurePosixPath(value).parts
        )
    ):
        raise FilestoreStorageError(
            f"filestore contains an unsafe relative path: {value!r}"
        )
    return value


def _hash_regular_file(path: Path) -> tuple[str, int]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FilestoreStorageError(
            f"could not securely open filestore file {path}: {exc}"
        ) from None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise FilestoreStorageError(
                f"filestore entry is not a regular file: {path}"
            )
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if identity_before != identity_after or size != after.st_size:
            raise FilestoreStorageError(
                f"filestore file changed during inventory: {path}"
            )
        return digest.hexdigest(), size
    finally:
        os.close(descriptor)


def scan_filestore(
    root: str | Path,
) -> tuple[list[FilestoreObjectEntry], str, int]:
    source = Path(root)
    if not source.is_dir() or source.is_symlink():
        raise FilestoreStorageError(
            f"filestore root is not a real directory: {source}"
        )
    source = source.resolve(strict=True)
    entries: list[FilestoreObjectEntry] = []
    for current, directories, files in os.walk(source, followlinks=False):
        current_path = Path(current)
        for directory in list(directories):
            candidate = current_path / directory
            if candidate.is_symlink():
                raise FilestoreStorageError(
                    f"filestore contains a symbolic-link directory: {candidate}"
                )
            if not candidate.is_dir():
                raise FilestoreStorageError(
                    f"filestore contains a non-directory entry: {candidate}"
                )
        for filename in files:
            candidate = current_path / filename
            if candidate.is_symlink():
                raise FilestoreStorageError(
                    f"filestore contains a symbolic-link file: {candidate}"
                )
            relative = _safe_relative_path(source, candidate)
            digest, size = _hash_regular_file(candidate)
            entries.append(
                FilestoreObjectEntry(
                    path=relative,
                    sha256=digest,
                    size=size,
                )
            )
    entries.sort(key=lambda entry: entry.path)
    inventory = filestore_inventory_sha256(entries)
    return entries, inventory, sum(entry.size for entry in entries)


def _assert_source_matches(
    root: Path,
    manifest: FilestoreMigrationManifest,
) -> None:
    entries, inventory, total_size = scan_filestore(root)
    if (
        entries != manifest.entries
        or inventory != manifest.inventory_sha256
        or total_size != manifest.total_size
    ):
        raise FilestoreStorageError(
            "filestore source changed after migration planning; create a new plan"
        )


def _copy_inventory(
    source: Path,
    destination: Path,
    manifest: FilestoreMigrationManifest,
) -> None:
    if os.path.lexists(destination):
        entries, inventory, total = scan_filestore(destination)
        if (
            entries == manifest.entries
            and inventory == manifest.inventory_sha256
            and total == manifest.total_size
        ):
            return
        raise FilestoreStorageError(
            f"filestore target already exists with different content: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.parent / (
        f".{destination.name}.{manifest.migration_id}.partial"
    )
    if os.path.lexists(partial):
        raise FilestoreStorageError(
            f"filestore partial target already exists: {partial}"
        )
    partial.mkdir(mode=0o700)
    try:
        for entry in manifest.entries:
            src = source.joinpath(*PurePosixPath(entry.path).parts)
            dst = partial.joinpath(*PurePosixPath(entry.path).parts)
            dst.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            source_lstat = os.lstat(src)
            if not stat.S_ISREG(source_lstat.st_mode):
                raise FilestoreStorageError(
                    f"filestore source changed type: {entry.path}"
                )
            source_fd = os.open(src, flags)
            try:
                source_stat = os.fstat(source_fd)
                if (
                    not stat.S_ISREG(source_stat.st_mode)
                    or source_stat.st_dev != source_lstat.st_dev
                    or source_stat.st_ino != source_lstat.st_ino
                ):
                    raise FilestoreStorageError(
                        f"filestore source changed type: {entry.path}"
                    )
                output_fd = os.open(
                    dst,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                try:
                    digest = hashlib.sha256()
                    size = 0
                    while True:
                        chunk = os.read(source_fd, 1024 * 1024)
                        if not chunk:
                            break
                        remaining = memoryview(chunk)
                        while remaining:
                            written = os.write(output_fd, remaining)
                            if written < 1:
                                raise FilestoreStorageError(
                                    f"short write while copying {entry.path}"
                                )
                            remaining = remaining[written:]
                        digest.update(chunk)
                        size += len(chunk)
                    os.fsync(output_fd)
                finally:
                    os.close(output_fd)
            finally:
                os.close(source_fd)
            if digest.hexdigest() != entry.sha256 or size != entry.size:
                raise FilestoreStorageError(
                    f"filestore source changed during copy: {entry.path}"
                )
        copied_entries, copied_inventory, copied_total = scan_filestore(partial)
        if (
            copied_entries != manifest.entries
            or copied_inventory != manifest.inventory_sha256
            or copied_total != manifest.total_size
        ):
            raise FilestoreStorageError(
                "copied filestore target failed inventory verification"
            )
        os.replace(partial, destination)
        descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except Exception:
        shutil.rmtree(partial, ignore_errors=True)
        raise


def backend_status(
    ctx: "ServiceContext",
    environment: str,
) -> FilestoreBackendStatus:
    env = ctx.project.config.env(environment)
    backend = env.filestore_backend
    effective = env.effective_filestore_backend
    if backend is None:
        source_kind = "docker_volume" if env.filestore_volume else "local"
        source = (
            f"volume:{env.filestore_volume}:{env.filestore_path}"
            if env.filestore_volume
            else str(_resolve_host_path(ctx, env.filestore_path))
        )
        target = None
    else:
        source_kind, source = _source_description(ctx, env, backend)
        target = None
        if backend.type == "posix_object_mount" and backend.mount_path:
            target = str(_resolve_host_path(ctx, backend.mount_path))
        elif backend.object_store is not None:
            target = f"s3://{backend.object_store.bucket}/{backend.object_store.prefix}"
        effective = backend.type
    migrations = MetadataStore(
        ctx.project.state_dir
    ).list_filestore_migrations(environment=environment)
    latest = migrations[0] if migrations else None
    return FilestoreBackendStatus(
        environment=environment,
        backend=effective,
        source_location=f"{source_kind}:{source}",
        target_location=target,
        migration_count=len(migrations),
        latest_migration_id=latest.migration_id if latest else None,
        latest_status=latest.status if latest else None,
    )


def plan_migration(
    ctx: "ServiceContext",
    environment: str,
) -> FilestoreMigrationManifest:
    env, backend = _backend(ctx, environment)
    if backend.type not in {
        "object_mirror",
        "posix_object_mount",
        "odoo_module",
    }:
        raise FilestoreStorageError(
            f"filestore backend {backend.type} has no migration target"
        )
    migration_id = (
        f"{environment}_filestore_{secrets.token_hex(12)}"
    )
    object_client = (
        _object_adapter(ctx, environment, backend)
        if backend.type in {"object_mirror", "odoo_module"}
        else None
    )
    source_backend, source_location = _source_description(
        ctx,
        env,
        backend,
    )
    target_location = _target_location(
        ctx,
        environment,
        backend,
        migration_id,
        object_adapter=object_client,
    )
    if (
        backend.type == "posix_object_mount"
        and Path(source_location).resolve(strict=False)
        == Path(target_location).resolve(strict=False)
    ):
        raise FilestoreStorageError(
            "POSIX object-mount migration source and target must differ"
        )
    previous_active = None
    if object_client is not None:
        active = object_client.read_active()
        previous_active = active.migration_id if active is not None else None
    with _materialized_source(ctx, env, backend) as source:
        entries, inventory, total_size = scan_filestore(source)
    manifest = FilestoreMigrationManifest(
        migration_id=migration_id,
        project=ctx.project.config.project.name,
        environment=environment,
        source_backend=source_backend,
        target_backend=backend.type,
        source_location=source_location,
        target_location=target_location,
        module_name=backend.module_name,
        previous_active_migration_id=previous_active,
        entries=entries,
        inventory_sha256=inventory,
        total_size=total_size,
    )
    MetadataStore(ctx.project.state_dir).save_filestore_migration(manifest)
    return manifest


def _owned_migration(
    ctx: "ServiceContext",
    environment: str,
    migration_id: str,
) -> tuple[
    FilestoreMigrationManifest,
    "EnvironmentConfig",
    "FilestoreBackendConfig",
]:
    env, backend = _backend(ctx, environment)
    manifest = MetadataStore(
        ctx.project.state_dir
    ).get_filestore_migration(migration_id)
    if (
        manifest.project != ctx.project.config.project.name
        or manifest.environment != environment
        or manifest.target_backend != backend.type
    ):
        raise FilestoreStorageError(
            "filestore migration does not belong to this project/environment/backend"
        )
    current_source_backend, current_source_location = _source_description(
        ctx,
        env,
        backend,
    )
    current_target_location = _target_location(
        ctx,
        environment,
        backend,
        migration_id,
    )
    if (
        manifest.source_backend != current_source_backend
        or manifest.source_location != current_source_location
        or manifest.target_location != current_target_location
        or manifest.module_name != backend.module_name
    ):
        raise FilestoreStorageError(
            "filestore source or target configuration changed after planning; "
            "create a new migration plan"
        )
    return manifest, env, backend


def sync_migration(
    ctx: "ServiceContext",
    environment: str,
    migration_id: str,
) -> FilestoreMigrationManifest:
    manifest, env, backend = _owned_migration(
        ctx,
        environment,
        migration_id,
    )
    if manifest.status not in {"planned", "failed", "synced"}:
        raise FilestoreStorageError(
            f"migration cannot sync from status {manifest.status!r}"
        )
    store = MetadataStore(ctx.project.state_dir)
    try:
        with _materialized_source(ctx, env, backend) as source:
            _assert_source_matches(source, manifest)
            if backend.type in {"object_mirror", "odoo_module"}:
                _object_adapter(
                    ctx,
                    environment,
                    backend,
                ).upload_inventory(source, manifest)
            elif backend.type == "posix_object_mount":
                _copy_inventory(
                    source,
                    Path(manifest.target_location),
                    manifest,
                )
            else:  # pragma: no cover - _owned_migration validates target
                raise FilestoreStorageError("unsupported filestore target")
        updated = _validated_update(
            manifest,
            status="synced",
            synced_at=manifest.synced_at or now_utc(),
            last_error=None,
        )
        store.save_filestore_migration(updated)
        return updated
    except BaseException as exc:
        store.save_filestore_migration(
            _validated_update(
                manifest,
                status="failed",
                last_error=_safe_error(exc, ctx),
            )
        )
        raise


def verify_migration(
    ctx: "ServiceContext",
    environment: str,
    migration_id: str,
) -> FilestoreVerifyResult:
    manifest, _env, backend = _owned_migration(
        ctx,
        environment,
        migration_id,
    )
    if manifest.status not in {"synced", "verified", "cutover"}:
        raise FilestoreStorageError(
            f"migration cannot verify from status {manifest.status!r}"
        )
    if backend.type in {"object_mirror", "odoo_module"}:
        remote: ObjectFilestoreVerification = _object_adapter(
            ctx,
            environment,
            backend,
        ).verify_inventory(manifest)
        if (
            remote.object_count != len(manifest.entries)
            or remote.total_size != manifest.total_size
        ):
            raise FilestoreStorageError(
                "remote filestore verification summary changed"
            )
    else:
        entries, inventory, total_size = scan_filestore(
            manifest.target_location
        )
        if (
            entries != manifest.entries
            or inventory != manifest.inventory_sha256
            or total_size != manifest.total_size
        ):
            raise FilestoreStorageError(
                "POSIX object-mount target failed inventory verification"
            )
    verified_at = now_utc()
    if manifest.status != "cutover":
        manifest = _validated_update(
            manifest,
            status="verified",
            synced_at=manifest.synced_at or verified_at,
            verified_at=verified_at,
            last_error=None,
        )
        MetadataStore(ctx.project.state_dir).save_filestore_migration(
            manifest
        )
    return FilestoreVerifyResult(
        migration_id=manifest.migration_id,
        object_count=len(manifest.entries),
        total_size=manifest.total_size,
        inventory_sha256=manifest.inventory_sha256,
        verified_at=verified_at,
    )


def cutover_migration(
    ctx: "ServiceContext",
    environment: str,
    migration_id: str,
    *,
    confirm_environment: str,
    confirm_source_retained: bool,
) -> FilestoreMigrationManifest:
    if (
        confirm_environment != environment
        or not confirm_source_retained
    ):
        raise FilestoreStorageError(
            "filestore cutover requires the exact environment and explicit "
            "confirmation that the source remains retained"
        )
    manifest, env, backend = _owned_migration(
        ctx,
        environment,
        migration_id,
    )
    if manifest.status == "cutover":
        return manifest
    if manifest.status != "verified":
        raise FilestoreStorageError(
            "filestore cutover requires a verified migration"
        )
    verify_migration(ctx, environment, migration_id)
    with _materialized_source(ctx, env, backend) as source:
        _assert_source_matches(source, manifest)
    if backend.type in {"object_mirror", "odoo_module"}:
        _object_adapter(
            ctx,
            environment,
            backend,
        ).publish_active(
            manifest,
            expected_previous_migration_id=(
                manifest.previous_active_migration_id
            ),
        )
    updated = _validated_update(
        manifest,
        status="cutover",
        cutover_at=now_utc(),
        last_error=None,
    )
    MetadataStore(ctx.project.state_dir).save_filestore_migration(updated)
    return updated


def download_migration(
    ctx: "ServiceContext",
    environment: str,
    migration_id: str,
    destination: str | Path,
) -> Path:
    manifest, _env, backend = _owned_migration(
        ctx,
        environment,
        migration_id,
    )
    if manifest.status not in {"verified", "cutover"}:
        raise FilestoreStorageError(
            "filestore download requires a verified migration"
        )
    target = _resolve_host_path(ctx, str(destination))
    if backend.type in {"object_mirror", "odoo_module"}:
        return _object_adapter(
            ctx,
            environment,
            backend,
        ).download_inventory(manifest, target)
    _copy_inventory(
        Path(manifest.target_location),
        target,
        manifest,
    )
    return target


def _source_exists(
    ctx: "ServiceContext",
    env: "EnvironmentConfig",
    backend: "FilestoreBackendConfig",
) -> bool:
    if backend.source_path:
        return FilestoreAdapter().exists(
            str(_resolve_host_path(ctx, backend.source_path))
        )
    if env.filestore_volume:
        return make_filestore_adapter(ctx.project, env).exists(
            env.filestore_path
        )
    return FilestoreAdapter().exists(
        str(_resolve_host_path(ctx, env.filestore_path))
    )


def delete_source_after_cutover(
    ctx: "ServiceContext",
    environment: str,
    migration_id: str,
    *,
    confirm_environment: str,
    confirm_migration_id: str,
    delete_source: bool,
) -> FilestoreMigrationManifest:
    if (
        confirm_environment != environment
        or confirm_migration_id != migration_id
        or not delete_source
    ):
        raise FilestoreStorageError(
            "source deletion requires exact environment/migration "
            "confirmations and --delete-source"
        )
    manifest, env, backend = _owned_migration(
        ctx,
        environment,
        migration_id,
    )
    if backend.type == "object_mirror":
        raise FilestoreStorageError(
            "object_mirror is a retained copy, not an Odoo serving backend; "
            "its source cannot be deleted"
        )
    if manifest.source_deleted:
        return manifest
    if manifest.status != "cutover":
        raise FilestoreStorageError(
            "filestore source cannot be deleted before explicit cutover"
        )
    verify_migration(ctx, environment, migration_id)
    source_exists = _source_exists(ctx, env, backend)
    if manifest.source_delete_started_at is None:
        if not source_exists:
            raise FilestoreStorageError(
                "filestore source disappeared before deletion was authorized"
            )
        with _materialized_source(ctx, env, backend) as source:
            _assert_source_matches(source, manifest)
        manifest = _validated_update(
            manifest,
            source_delete_started_at=now_utc(),
            last_error=None,
        )
        MetadataStore(ctx.project.state_dir).save_filestore_migration(
            manifest
        )
    elif source_exists:
        # A retry after an interrupted delete must not remove newly-created or
        # changed content at the same path.
        with _materialized_source(ctx, env, backend) as source:
            _assert_source_matches(source, manifest)

    if backend.source_path:
        source_path = _resolve_host_path(ctx, backend.source_path)
        target_path = (
            Path(manifest.target_location).resolve(strict=False)
            if backend.type == "posix_object_mount"
            else None
        )
        if target_path is not None and source_path == target_path:
            raise FilestoreStorageError(
                "refusing to delete a source that equals the active target"
            )
        shutil.rmtree(source_path)
    elif env.filestore_volume:
        make_filestore_adapter(ctx.project, env).delete(env.filestore_path)
    else:
        source_path = _resolve_host_path(ctx, env.filestore_path)
        if (
            backend.type == "posix_object_mount"
            and source_path
            == Path(manifest.target_location).resolve(strict=False)
        ):
            raise FilestoreStorageError(
                "refusing to delete a source that equals the active target"
            )
        shutil.rmtree(source_path)
    updated = _validated_update(
        manifest,
        source_deleted=True,
        source_deleted_at=now_utc(),
        last_error=None,
    )
    MetadataStore(ctx.project.state_dir).save_filestore_migration(updated)
    return updated


def delete_inactive_remote_marker(
    ctx: "ServiceContext",
    environment: str,
    migration_id: str,
    *,
    confirm_migration_id: str,
) -> str:
    if confirm_migration_id != migration_id:
        raise FilestoreStorageError(
            "remote marker deletion requires exact migration confirmation"
        )
    manifest, _env, backend = _owned_migration(
        ctx,
        environment,
        migration_id,
    )
    if backend.type not in {"object_mirror", "odoo_module"}:
        raise FilestoreStorageError(
            "POSIX object-mount migrations have no remote marker"
        )
    return _object_adapter(
        ctx,
        environment,
        backend,
    ).delete_migration_manifest(
        manifest.migration_id,
        confirm_not_active=True,
    )
