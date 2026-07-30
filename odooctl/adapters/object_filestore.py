"""Content-addressed S3 storage for verified Odoo filestore migrations."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from odooctl.adapters.s3 import (
    SHA256_METADATA_KEY,
    S3Adapter,
    S3IntegrityError,
    S3ObjectInfo,
    S3ProviderError,
)
from odooctl.config import (
    FilestoreObjectStoreConfig,
    RemoteBackupConfig,
)
from odooctl.metadata.models import FilestoreMigrationManifest


MAX_FILESTORE_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_ACTIVE_POINTER_BYTES = 64 * 1024


class ObjectFilestoreError(RuntimeError):
    """Base error for a scoped object-filestore operation."""


class ObjectFilestoreConflict(ObjectFilestoreError):
    """A remote immutable object or active pointer changed unexpectedly."""


@dataclass(frozen=True)
class ActiveFilestoreGeneration:
    migration_id: str
    inventory_sha256: str
    key: str
    etag: str


@dataclass(frozen=True)
class ObjectFilestoreVerification:
    migration_id: str
    object_count: int
    total_size: int
    manifest: S3ObjectInfo


def _project_namespace(project: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", project.lower()).strip("-")[:32]
    digest = hashlib.sha256(project.encode()).hexdigest()[:16]
    return f"{slug or 'project'}-{digest}"


def _canonical_json(document: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ObjectFilestoreAdapter:
    """S3-compatible, project/environment-scoped filestore generations."""

    def __init__(
        self,
        config: FilestoreObjectStoreConfig,
        *,
        project: str,
        environment: str,
        state_dir: str | Path,
        boto3_module: Any | None = None,
    ):
        if not project.strip() or any(ord(character) < 32 for character in project):
            raise ValueError("project must be a non-empty printable string")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", environment):
            raise ValueError("environment must be one safe component")
        prefix = "/".join(
            part
            for part in (
                config.prefix.strip("/"),
                "projects",
                _project_namespace(project),
                "filestore",
                environment,
            )
            if part
        )
        remote = RemoteBackupConfig(
            bucket=config.bucket,
            region=config.region,
            prefix=prefix,
            endpoint_env=config.endpoint_env,
            access_key_env=config.access_key_env,
            secret_key_env=config.secret_key_env,
            session_token_env=config.session_token_env,
            region_env=config.region_env,
            encryption_algorithm=config.encryption_algorithm,
            encryption_key_env=config.encryption_key_env,
            policy="required",
            verify_after_upload=True,
        )
        self.project = project
        self.environment = environment
        self.prefix = prefix
        self.bucket = config.bucket
        self.s3 = S3Adapter(
            remote,
            root=Path(state_dir) / "filestore-objects",
            boto3_module=boto3_module,
        )

    def _scoped_key(self, *parts: str) -> str:
        normalized: list[str] = []
        for raw in parts:
            path = PurePosixPath(raw)
            if (
                not raw
                or raw.startswith("/")
                or raw.endswith("/")
                or "\\" in raw
                or any(
                    part in {"", ".", ".."}
                    or any(ord(character) < 32 for character in part)
                    for part in path.parts
                )
            ):
                raise ValueError("object-filestore key contains an unsafe component")
            normalized.extend(path.parts)
        key = "/".join((self.prefix, *normalized))
        if len(key.encode()) > 1024:
            raise ValueError(
                "object-filestore key exceeds the S3 1024-byte limit"
            )
        return key

    @staticmethod
    def _migration_id(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
            raise ValueError("migration_id must be one safe component")
        return value

    @staticmethod
    def _digest(value: str) -> str:
        normalized = value.lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("filestore object digest must be a SHA-256")
        return normalized

    def object_key(self, digest: str) -> str:
        return self._scoped_key("objects", self._digest(digest))

    def migration_manifest_key(self, migration_id: str) -> str:
        return self._scoped_key(
            "migrations",
            self._migration_id(migration_id),
            "manifest.json",
        )

    @property
    def active_key(self) -> str:
        return self._scoped_key("active.json")

    def uri_for_migration(self, migration_id: str) -> str:
        key = self.migration_manifest_key(migration_id)
        return f"s3://{self.bucket}/{key}"

    def _assert_manifest_scope(
        self,
        manifest: FilestoreMigrationManifest,
    ) -> None:
        if (
            manifest.project != self.project
            or manifest.environment != self.environment
            or manifest.target_backend
            not in {"object_mirror", "odoo_module"}
            or manifest.target_location
            != self.uri_for_migration(manifest.migration_id)
        ):
            raise ObjectFilestoreConflict(
                "filestore manifest is outside this adapter's "
                "project/environment/target scope"
            )

    @staticmethod
    def _manifest_document(
        manifest: FilestoreMigrationManifest,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "migration_id": manifest.migration_id,
            "project": manifest.project,
            "environment": manifest.environment,
            "source_backend": manifest.source_backend,
            "target_backend": manifest.target_backend,
            "module_name": manifest.module_name,
            "previous_active_migration_id": (
                manifest.previous_active_migration_id
            ),
            "inventory_sha256": manifest.inventory_sha256,
            "total_size": manifest.total_size,
            "entries": [
                entry.model_dump(mode="json") for entry in manifest.entries
            ],
        }

    def _object_exists(self, key: str) -> bool:
        try:
            self.s3._client().head_object(
                Bucket=self.bucket,
                Key=key,
            )
        except Exception as exc:
            if self.s3._is_not_found_error(exc):
                return False
            raise S3ProviderError(
                "filestore object lookup failed"
            ) from None
        return True

    def _put_immutable_document(
        self,
        key: str,
        payload: bytes,
    ) -> None:
        """Create one immutable JSON marker or accept an identical retry."""

        if len(payload) > MAX_FILESTORE_MANIFEST_BYTES:
            raise ObjectFilestoreError(
                "filestore migration manifest exceeds the allowed size"
            )
        digest = hashlib.sha256(payload).hexdigest()
        original_error: BaseException | None = None
        try:
            self.s3._client().put_object(
                Bucket=self.bucket,
                Key=key,
                Body=io.BytesIO(payload),
                Metadata={SHA256_METADATA_KEY: digest},
                IfNoneMatch="*",
                **self.s3._sse_extra_args(),
            )
        except Exception as exc:
            original_error = exc

        if original_error is None:
            self.s3.verify_object_content(
                key,
                expected_sha256=digest,
            )
            return

        try:
            _info, existing = self.s3._read_verified_object(
                key,
                collect=True,
                max_bytes=MAX_FILESTORE_MANIFEST_BYTES,
            )
        except Exception:
            raise S3ProviderError(
                "immutable filestore marker publication failed"
            ) from None
        if existing != payload:
            raise ObjectFilestoreConflict(
                "remote filestore migration manifest conflicts with local inventory"
            )

    def upload_inventory(
        self,
        source_root: str | Path,
        manifest: FilestoreMigrationManifest,
    ) -> ObjectFilestoreVerification:
        self._assert_manifest_scope(manifest)
        root = Path(source_root)
        if not root.is_dir() or root.is_symlink():
            raise ObjectFilestoreError(
                f"filestore source is not a real directory: {root}"
            )
        completed_keys: set[str] = set()
        for entry in manifest.entries:
            source = root.joinpath(*PurePosixPath(entry.path).parts)
            if not source.is_file() or source.is_symlink():
                raise ObjectFilestoreError(
                    f"filestore entry is not a regular file: {entry.path}"
                )
            if source.stat().st_size != entry.size or _sha256_file(source) != entry.sha256:
                raise ObjectFilestoreConflict(
                    f"filestore source changed after inventory: {entry.path}"
                )
            key = self.object_key(entry.sha256)
            if key in completed_keys:
                continue
            if self._object_exists(key):
                self.s3.verify_object_content(
                    key,
                    expected_sha256=entry.sha256,
                )
                completed_keys.add(key)
                continue
            uploaded = self.s3.upload_object(source, key)
            if uploaded.size != entry.size:
                raise S3IntegrityError(
                    f"uploaded filestore object size changed for {entry.path}"
                )
            self.s3.verify_object_content(
                key,
                expected_sha256=entry.sha256,
            )
            completed_keys.add(key)

        document = self._manifest_document(manifest)
        payload = _canonical_json(document)
        manifest_key = self.migration_manifest_key(manifest.migration_id)
        self._put_immutable_document(manifest_key, payload)
        return self.verify_inventory(manifest)

    def verify_inventory(
        self,
        manifest: FilestoreMigrationManifest,
    ) -> ObjectFilestoreVerification:
        self._assert_manifest_scope(manifest)
        expected = _canonical_json(self._manifest_document(manifest))
        manifest_info, payload = self.s3._read_verified_object(
            self.migration_manifest_key(manifest.migration_id),
            collect=True,
            max_bytes=MAX_FILESTORE_MANIFEST_BYTES,
        )
        if payload != expected:
            raise ObjectFilestoreConflict(
                "remote filestore migration manifest failed identity verification"
            )
        for entry in manifest.entries:
            info = self.s3.verify_object_content(
                self.object_key(entry.sha256),
                expected_sha256=entry.sha256,
            )
            if info.size != entry.size:
                raise S3IntegrityError(
                    f"remote filestore object size mismatch for {entry.path}"
                )
        return ObjectFilestoreVerification(
            migration_id=manifest.migration_id,
            object_count=len(manifest.entries),
            total_size=manifest.total_size,
            manifest=manifest_info,
        )

    def download_inventory(
        self,
        manifest: FilestoreMigrationManifest,
        destination: str | Path,
    ) -> Path:
        self._assert_manifest_scope(manifest)
        self.verify_inventory(manifest)
        target = Path(destination)
        if os.path.lexists(target):
            raise FileExistsError(f"filestore download target already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.parent / f".{target.name}.{manifest.migration_id}.partial"
        if os.path.lexists(partial):
            raise FileExistsError(f"filestore partial target already exists: {partial}")
        partial.mkdir(mode=0o700)
        try:
            for entry in manifest.entries:
                output = partial.joinpath(*PurePosixPath(entry.path).parts)
                output.parent.mkdir(parents=True, exist_ok=True)
                self.s3.download_object(
                    self.object_key(entry.sha256),
                    output,
                    expected_sha256=entry.sha256,
                    overwrite=False,
                )
                if output.stat().st_size != entry.size:
                    raise S3IntegrityError(
                        f"downloaded filestore object size mismatch for {entry.path}"
                    )
            for directory in sorted(
                (path for path in partial.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                _fsync_directory(directory)
            _fsync_directory(partial)
            os.replace(partial, target)
            _fsync_directory(target.parent)
        except Exception:
            shutil.rmtree(partial, ignore_errors=True)
            raise
        return target

    def read_active(self) -> ActiveFilestoreGeneration | None:
        client = self.s3._client()
        try:
            client.head_object(
                Bucket=self.bucket,
                Key=self.active_key,
            )
        except Exception as exc:
            if self.s3._is_not_found_error(exc):
                return None
            raise S3ProviderError(
                "active filestore pointer lookup failed"
            ) from None
        info, payload = self.s3._read_verified_object(
            self.active_key,
            collect=True,
            max_bytes=MAX_ACTIVE_POINTER_BYTES,
        )
        assert payload is not None
        try:
            document = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError):
            raise ObjectFilestoreConflict(
                "active filestore pointer is not valid JSON"
            ) from None
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != 1
            or document.get("project") != self.project
            or document.get("environment") != self.environment
        ):
            raise ObjectFilestoreConflict(
                "active filestore pointer is outside this project/environment"
            )
        migration_id = self._migration_id(str(document.get("migration_id", "")))
        inventory = self._digest(str(document.get("inventory_sha256", "")))
        etag = info.etag
        if not etag:
            raise ObjectFilestoreConflict("active filestore pointer has no ETag")
        return ActiveFilestoreGeneration(
            migration_id=migration_id,
            inventory_sha256=inventory,
            key=self.active_key,
            etag=etag,
        )

    def publish_active(
        self,
        manifest: FilestoreMigrationManifest,
        *,
        expected_previous_migration_id: str | None,
    ) -> ActiveFilestoreGeneration:
        self._assert_manifest_scope(manifest)
        self.verify_inventory(manifest)
        current = self.read_active()
        if (
            current is not None
            and current.migration_id == manifest.migration_id
            and current.inventory_sha256 == manifest.inventory_sha256
        ):
            return current
        current_id = current.migration_id if current is not None else None
        if current_id != expected_previous_migration_id:
            raise ObjectFilestoreConflict(
                "active filestore generation changed after migration planning"
            )
        document = {
            "schema_version": 1,
            "project": self.project,
            "environment": self.environment,
            "migration_id": manifest.migration_id,
            "inventory_sha256": manifest.inventory_sha256,
        }
        payload = _canonical_json(document)
        digest = hashlib.sha256(payload).hexdigest()
        client = self.s3._client()
        kwargs: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": self.active_key,
            "Body": io.BytesIO(payload),
            "Metadata": {SHA256_METADATA_KEY: digest},
            **self.s3._sse_extra_args(),
        }
        if current is None:
            kwargs["IfNoneMatch"] = "*"
        else:
            kwargs["IfMatch"] = f'"{current.etag}"'
        original_error: BaseException | None = None
        try:
            client.put_object(**kwargs)
        except Exception as exc:
            original_error = exc
        try:
            published = self.read_active()
        except Exception:
            if original_error is not None:
                raise S3ProviderError(
                    "active filestore pointer publication failed"
                ) from None
            raise
        if (
            published is not None
            and published.migration_id == manifest.migration_id
            and published.inventory_sha256 == manifest.inventory_sha256
        ):
            return published
        if original_error is not None:
            raise ObjectFilestoreConflict(
                "active filestore conditional publication lost a race"
            ) from None
        raise S3ProviderError(
            "active filestore pointer publication returned but did not persist"
        )

    def delete_migration_manifest(
        self,
        migration_id: str,
        *,
        confirm_not_active: bool,
    ) -> str:
        """Delete only an inactive migration marker, never content objects."""

        if not confirm_not_active:
            raise ValueError("remote deletion requires confirm_not_active=True")
        active = self.read_active()
        safe_id = self._migration_id(migration_id)
        if active is not None and active.migration_id == safe_id:
            raise ObjectFilestoreConflict(
                "refusing to delete the active filestore migration manifest"
            )
        key = self.migration_manifest_key(safe_id)
        self.s3._provider_call(
            f"delete {key}",
            self.s3._client().delete_object,
            Bucket=self.bucket,
            Key=key,
        )
        return key
