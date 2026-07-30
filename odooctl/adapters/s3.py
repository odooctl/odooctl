"""S3-compatible remote-object storage for portable backups.

The backup manifest is the completion marker.  Uploads publish every payload
object first and the root ``manifest.json`` last, so an interrupted transfer is
not advertised as a complete remote backup.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from rich.console import Console

from odooctl.config import RemoteBackupConfig
from odooctl.security.redaction import redact_text


SHA256_METADATA_KEY = "sha256"
COMPLETION_MARKER = "manifest.json"
ABANDONMENT_MARKER = "abandoned.json"
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
REQUIRED_ARTIFACT_CHECKSUMS = {
    "db.dump": "db_dump",
    "filestore.tar": "filestore",
}
STANDARD_AWS_SECRET_ENVS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    # The URI may contain HTTP userinfo or a signed query string.
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    # An endpoint may contain HTTP basic auth or a signed query string.
    "AWS_ENDPOINT_URL",
)
AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE = (
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE"
)
MAX_CONTAINER_AUTHORIZATION_TOKEN_BYTES = 1024 * 1024


class S3AdapterError(RuntimeError):
    """Base class for explicit remote-object failures."""


class S3DependencyError(S3AdapterError):
    """The optional S3 client dependency is unavailable."""


class S3ConfigurationError(S3AdapterError):
    """Remote storage configuration is incomplete or unsafe."""


class S3PathError(S3AdapterError):
    """An object key, prefix, or local path escapes the requested scope."""


class S3ProviderError(S3AdapterError):
    """The S3-compatible provider rejected or failed an operation."""


class S3IntegrityError(S3AdapterError):
    """Remote checksum, size, or completion-marker verification failed."""


def _container_authorization_token_file_values(
    *,
    validate: bool,
) -> tuple[str, ...]:
    """Read the default-chain token file without accepting unsafe file types."""

    reference = os.getenv(AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE)
    if not reference:
        return ()
    try:
        metadata = os.stat(reference)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("authorization token path is not a regular file")
        if metadata.st_size > MAX_CONTAINER_AUTHORIZATION_TOKEN_BYTES:
            raise OSError("authorization token file exceeds the size limit")
        with Path(reference).open("rb") as handle:
            payload = handle.read(
                MAX_CONTAINER_AUTHORIZATION_TOKEN_BYTES + 1
            )
        if len(payload) > MAX_CONTAINER_AUTHORIZATION_TOKEN_BYTES:
            raise OSError("authorization token file exceeds the size limit")
        token = payload.decode("utf-8")
    except (OSError, UnicodeError):
        if validate:
            raise S3ConfigurationError(
                "AWS container authorization token file could not be "
                "securely read"
            ) from None
        # The path itself may still carry sensitive information.
        return (reference,)

    stripped = token.strip()
    return tuple(
        dict.fromkeys(
            value
            for value in (reference, token, stripped)
            if value
        )
    )


def configured_s3_secret_values(
    config: RemoteBackupConfig,
    *,
    validate_container_token_file: bool = False,
) -> tuple[str, ...]:
    """Resolve configured and standard-chain values that must not reach errors."""

    references = (
        config.endpoint_env,
        config.access_key_env,
        config.secret_key_env,
        config.session_token_env,
        config.region_env,
        config.encryption_key_env,
        *STANDARD_AWS_SECRET_ENVS,
    )
    values = [
        value
        for reference in references
        if reference and (value := os.getenv(reference))
    ]
    values.extend(
        _container_authorization_token_file_values(
            validate=validate_container_token_file,
        )
    )
    return tuple(dict.fromkeys(values))


@dataclass(frozen=True)
class S3ObjectInfo:
    """Metadata returned for one object under the configured prefix."""

    key: str
    size: int
    sha256: str | None = None
    etag: str | None = None
    last_modified: Any | None = None


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    """Make a downloaded tree durable before its atomic directory rename."""

    directories = [root]
    for path in root.rglob("*"):
        if path.is_symlink():
            raise S3PathError(f"download tree contains a symbolic link: {path}")
        if path.is_file():
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        elif path.is_dir():
            directories.append(path)
        else:
            raise S3PathError(f"download tree contains a non-regular path: {path}")
    for directory in sorted(
        directories,
        key=lambda candidate: len(candidate.parts),
        reverse=True,
    ):
        _fsync_directory(directory)


class S3Adapter:
    def __init__(
        self,
        config: RemoteBackupConfig,
        root: str | Path = ".odooctl/remote-backups",
        *,
        console: Console | None = None,
        boto3_module: Any | None = None,
    ):
        self.config = config
        # Kept for source compatibility with the old local-mirror adapter.
        # Remote operations never write here or silently fall back to it.
        self.root = Path(root)
        self.console = console or Console(stderr=True)
        self._boto3 = boto3_module
        self._s3_client: Any | None = None
        # Keep every value used to initialize the provider client. Credential
        # files and environment variables can rotate before a later exception,
        # while the client may still echo the value it originally loaded.
        self._cached_secret_values: tuple[str, ...] = ()

    def _bucket(self) -> str:
        bucket = self.config.bucket
        if (
            not bucket
            or bucket.strip() != bucket
            or bucket in {".", ".."}
            or any(character in bucket for character in ("/", "\\", "\x00"))
        ):
            raise S3ConfigurationError("remote backup bucket must be a non-empty bucket name")
        return bucket

    def remote_dir(self) -> Path:
        """Return the legacy mirror path without using it as a fallback."""

        return self.root / self._bucket()

    def _load_boto3(self) -> Any:
        if self._boto3 is not None:
            return self._boto3
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:
            raise S3DependencyError(
                "boto3 is required for remote backups; install odooctl[s3]"
            ) from exc
        return boto3

    @staticmethod
    def _required_env(name: str, purpose: str) -> str:
        value = os.getenv(name)
        if not value:
            raise S3ConfigurationError(f"Missing {purpose} environment variable: {name}")
        return value

    def _region(self) -> str | None:
        if self.config.region_env:
            return self._required_env(
                self.config.region_env,
                "remote backup region",
            )
        return self.config.region or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")

    def _endpoint_url(self) -> str | None:
        if self.config.endpoint_env:
            return self._required_env(
                self.config.endpoint_env,
                "remote backup endpoint",
            )
        return None

    def _credentials(self) -> dict[str, str]:
        access_ref = self.config.access_key_env
        secret_ref = self.config.secret_key_env
        if bool(access_ref) != bool(secret_ref):
            raise S3ConfigurationError(
                "remote backup access_key_env and secret_key_env must be configured together"
            )
        if not access_ref or not secret_ref:
            return {}
        credentials = {
            "aws_access_key_id": self._required_env(
                access_ref,
                "remote backup access key",
            ),
            "aws_secret_access_key": self._required_env(
                secret_ref,
                "remote backup secret key",
            ),
        }
        if self.config.session_token_env:
            credentials["aws_session_token"] = self._required_env(
                self.config.session_token_env,
                "remote backup session token",
            )
        return credentials

    def _client(self) -> Any:
        if self._s3_client is not None:
            return self._s3_client
        credentials = self._credentials()
        resolved_secrets = configured_s3_secret_values(
            self.config,
            validate_container_token_file=not credentials,
        )
        self._cached_secret_values = tuple(
            dict.fromkeys(
                (*self._cached_secret_values, *resolved_secrets)
            )
        )
        module = self._load_boto3()
        kwargs: dict[str, Any] = {**credentials}
        region = self._region()
        endpoint_url = self._endpoint_url()
        if region:
            kwargs["region_name"] = region
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        try:
            self._s3_client = module.client("s3", **kwargs)
        except Exception as exc:
            detail = redact_text(str(exc), self._configured_env_values())
            raise S3ProviderError(f"S3 client initialization failed: {detail}") from None
        return self._s3_client

    def _configured_env_values(self) -> tuple[str, ...]:
        """Return resolved configured values that must never reach error text."""

        current = configured_s3_secret_values(self.config)
        return tuple(
            dict.fromkeys(
                (*self._cached_secret_values, *current)
            )
        )

    def _provider_call(
        self,
        operation: str,
        method: Any,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        try:
            return method(*args, **kwargs)
        except Exception as exc:
            detail = redact_text(str(exc), self._configured_env_values())
            raise S3ProviderError(f"S3 {operation} failed: {detail}") from None

    def _prefix_parts(self) -> tuple[str, ...]:
        raw = self.config.prefix.strip("/")
        if not raw:
            return ()
        if "\\" in raw or "\x00" in raw:
            raise S3PathError("remote backup prefix contains unsafe characters")
        parts = tuple(raw.split("/"))
        if any(
            not part or part in {".", ".."} or any(ord(character) < 32 for character in part)
            for part in parts
        ):
            raise S3PathError("remote backup prefix contains unsafe path segments")
        return parts

    @staticmethod
    def _backup_name(backup_name: str) -> str:
        if (
            not backup_name
            or backup_name in {".", ".."}
            or "/" in backup_name
            or "\\" in backup_name
            or "\x00" in backup_name
            or any(ord(character) < 32 for character in backup_name)
        ):
            raise S3PathError("backup name must be one safe path component")
        return backup_name

    @staticmethod
    def _relative_parts(relative_path: Path) -> tuple[str, ...]:
        raw = relative_path.as_posix()
        if raw in {"", "."}:
            return ()
        if relative_path.is_absolute() or "\\" in raw or "\x00" in raw:
            raise S3PathError("backup-relative path is unsafe")
        parts = PurePosixPath(raw).parts
        if any(
            not part or part in {".", ".."} or any(ord(character) < 32 for character in part)
            for part in parts
        ):
            raise S3PathError("backup-relative path contains unsafe segments")
        return tuple(parts)

    def _backup_prefix(self, backup_name: str) -> str:
        parts = (*self._prefix_parts(), self._backup_name(backup_name))
        return "/".join(parts)

    def manifest_key(self, backup_name: str) -> str:
        """Return the validated root completion-marker key for a backup."""

        return self._object_key(backup_name, Path(COMPLETION_MARKER))

    def abandonment_key(self, backup_name: str) -> str:
        """Return the marker used to authorize cleanup of an incomplete prefix."""

        return self._object_key(backup_name, Path(ABANDONMENT_MARKER))

    @staticmethod
    def _is_not_found_error(exc: BaseException) -> bool:
        """Recognize S3-compatible HEAD not-found responses without string matching."""

        response = getattr(exc, "response", None)
        if not isinstance(response, dict):
            return False
        error = response.get("Error")
        metadata = response.get("ResponseMetadata")
        code = str(error.get("Code", "")) if isinstance(error, dict) else ""
        status = (
            metadata.get("HTTPStatusCode")
            if isinstance(metadata, dict)
            else None
        )
        return code in {"404", "NoSuchKey", "NotFound"} or status == 404

    def abandonment_fence_exists(self, backup_name: str) -> bool:
        """Probe the exact abandonment key with a direct, strongly consistent HEAD."""

        safe_name = self._backup_name(backup_name)
        key = self.abandonment_key(safe_name)
        client = self._client()
        try:
            client.head_object(
                Bucket=self._bucket(),
                Key=key,
            )
        except Exception as exc:
            if self._is_not_found_error(exc):
                return False
            detail = redact_text(str(exc), self._configured_env_values())
            raise S3ProviderError(f"S3 head {key} failed: {detail}") from None
        return True

    def assert_not_abandoned(self, backup_name: str) -> None:
        """Reject every publication attempt for a permanently abandoned id."""

        safe_name = self._backup_name(backup_name)
        if self.abandonment_fence_exists(safe_name):
            raise S3IntegrityError(
                "An abandoned remote backup id cannot be republished; "
                "create a new globally unique backup"
            )

    def _object_key(self, backup_name: str, relative_path: Path) -> str:
        parts = (
            *self._prefix_parts(),
            self._backup_name(backup_name),
            *self._relative_parts(relative_path),
        )
        return "/".join(parts)

    def _validate_scoped_key(self, key: str) -> str:
        if not key or key.startswith("/") or key.endswith("/") or "\\" in key or "\x00" in key:
            raise S3PathError("S3 object key is outside the configured scope")
        parts = tuple(key.split("/"))
        if any(
            not part or part in {".", ".."} or any(ord(character) < 32 for character in part)
            for part in parts
        ):
            raise S3PathError("S3 object key contains unsafe path segments")
        prefix = self._prefix_parts()
        if prefix and parts[: len(prefix)] != prefix:
            raise S3PathError("S3 object key is outside the configured prefix")
        if len(parts) <= len(prefix):
            raise S3PathError("S3 object key does not identify an object")
        return "/".join(parts)

    def _relative_from_backup_key(self, backup_name: str, key: str) -> Path:
        validated = self._validate_scoped_key(key)
        prefix = self._backup_prefix(backup_name) + "/"
        if not validated.startswith(prefix):
            raise S3PathError(f"S3 object {validated!r} is outside backup {backup_name!r}")
        relative = validated[len(prefix) :]
        parts = self._relative_parts(Path(relative))
        if not parts:
            raise S3PathError("S3 backup object has no relative path")
        return Path(*parts)

    def _sse_extra_args(self) -> dict[str, Any]:
        if not self.config.encryption_algorithm:
            return {}
        extra: dict[str, Any] = {"ServerSideEncryption": self.config.encryption_algorithm}
        if self.config.encryption_key_env:
            extra["SSEKMSKeyId"] = self._required_env(
                self.config.encryption_key_env,
                "remote backup encryption key",
            )
        return extra

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _upload_extra_args(self, sha256: str) -> dict[str, Any]:
        return {
            **self._sse_extra_args(),
            "Metadata": {SHA256_METADATA_KEY: sha256},
        }

    def _backup_files(self, backup_dir: Path) -> list[tuple[Path, Path]]:
        if not backup_dir.exists() or not backup_dir.is_dir() or backup_dir.is_symlink():
            raise S3PathError(f"backup directory is not a real directory: {backup_dir}")
        self._backup_name(backup_dir.name)

        files: list[tuple[Path, Path]] = []
        for path in sorted(
            backup_dir.rglob("*"),
            key=lambda candidate: candidate.relative_to(backup_dir).as_posix(),
        ):
            if path.is_symlink():
                raise S3PathError(f"backup tree contains a symbolic link: {path}")
            if path.is_dir():
                continue
            if not path.is_file():
                raise S3PathError(f"backup tree contains a non-regular file: {path}")
            relative = path.relative_to(backup_dir)
            self._relative_parts(relative)
            files.append((path, relative))

        marker = backup_dir / COMPLETION_MARKER
        if not marker.is_file() or marker.is_symlink():
            raise S3IntegrityError(f"backup is incomplete: missing root {COMPLETION_MARKER}")
        # Publish the root manifest last. A nested file with the same basename is
        # ordinary payload and does not count as the completion marker.
        return sorted(
            files,
            key=lambda item: (
                item[1] == Path(COMPLETION_MARKER),
                item[1].as_posix(),
            ),
        )

    def upload_object(self, source: str | Path, key: str) -> S3ObjectInfo:
        """Upload one regular file with SHA-256 metadata and verify its HEAD."""

        source_path = Path(source)
        if not source_path.is_file() or source_path.is_symlink():
            raise S3PathError(f"upload source is not a regular file: {source_path}")
        scoped_key = self._validate_scoped_key(key)
        digest = self._sha256_file(source_path)
        size = source_path.stat().st_size
        client = self._client()
        self._provider_call(
            f"upload {scoped_key}",
            client.upload_file,
            str(source_path),
            self._bucket(),
            scoped_key,
            ExtraArgs=self._upload_extra_args(digest),
        )
        return self.verify_object(
            scoped_key,
            expected_sha256=digest,
            expected_size=size,
        )

    def upload_backup_payload(
        self,
        backup_dir: str | Path,
    ) -> list[S3ObjectInfo]:
        """Remove a stale marker, then upload every payload object."""

        source = Path(backup_dir)
        files = self._backup_files(source)
        self.assert_not_abandoned(source.name)
        # A retry may find a completion marker from an older upload under the
        # same backup name. Remove it before changing any payload bytes so a
        # crash cannot advertise a mixed-generation backup as complete.
        self.remove_completion_marker(source.name)
        uploaded: list[S3ObjectInfo] = []
        for file_path, relative_path in files:
            if relative_path == Path(COMPLETION_MARKER):
                continue
            uploaded.append(
                self.upload_object(
                    file_path,
                    self._object_key(source.name, relative_path),
                )
            )
        return uploaded

    def _upload_manifest_expectations(
        self,
        manifest_path: Path,
        backup_name: str,
        *,
        backup_dir: Path | None = None,
    ) -> dict[str, str]:
        """Validate a local completion manifest before it can be published."""

        if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
            raise S3IntegrityError("backup manifest exceeds the allowed size")
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise S3IntegrityError("backup manifest is not valid JSON") from None
        if not isinstance(manifest, dict):
            raise S3IntegrityError("backup manifest must be a JSON object")
        if manifest.get("backup_id") != backup_name:
            raise S3IntegrityError("backup manifest identity does not match")
        for field in ("project", "environment"):
            value = manifest.get(field)
            if not isinstance(value, str) or not value.strip():
                raise S3IntegrityError(f"backup manifest has no {field}")
        if manifest.get("status", "complete") != "complete":
            raise S3IntegrityError("backup manifest status is not complete")
        # Pre-R3 manifests have no remote_status and remain publishable. Once
        # the R3 field is present, only the final complete state is a valid
        # completion marker.
        if "remote_status" in manifest and manifest.get("remote_status") != "complete":
            raise S3IntegrityError("backup manifest remote status is not complete")
        if manifest.get("remote_status") == "complete" and manifest.get("remote_error"):
            raise S3IntegrityError("complete backup manifest must not contain a remote error")

        artifact_paths = manifest.get("artifact_paths")
        if not isinstance(artifact_paths, list) or any(
            not isinstance(path, str) for path in artifact_paths
        ):
            raise S3IntegrityError("backup manifest has invalid artifact_paths")
        normalized_paths = {
            PurePosixPath(*self._relative_parts(Path(path))).as_posix() for path in artifact_paths
        }
        missing = sorted(set(REQUIRED_ARTIFACT_CHECKSUMS) - normalized_paths)
        if missing:
            raise S3IntegrityError(
                "backup manifest is missing required artifacts: " + ", ".join(missing)
            )

        checksums = manifest.get("checksums")
        if not isinstance(checksums, dict):
            raise S3IntegrityError("backup manifest has no checksum map")
        expected: dict[str, str] = {}
        for artifact, checksum_name in REQUIRED_ARTIFACT_CHECKSUMS.items():
            digest = self._manifest_digest(
                checksums.get(checksum_name, checksums.get(artifact)),
                artifact,
            )
            expected[artifact] = digest
            if backup_dir is None:
                continue
            local_artifact = backup_dir / artifact
            if (
                not local_artifact.is_file()
                or local_artifact.is_symlink()
                or local_artifact.stat().st_size <= 0
            ):
                raise S3IntegrityError(f"backup is missing non-empty artifact {artifact}")
            if self._sha256_file(local_artifact) != digest:
                raise S3IntegrityError(f"backup manifest checksum mismatch for {artifact}")
        return expected

    def _verify_manifest_payloads(
        self,
        backup_name: str,
        expected: dict[str, str],
    ) -> None:
        """Require every manifest artifact to exist remotely before marking complete."""

        for artifact, digest in expected.items():
            key = self._object_key(backup_name, Path(artifact))
            info = self.head_object(key)
            if info.sha256 is None:
                # A legitimate pre-R3 payload has no custom metadata. Stream it
                # and validate its actual bytes against the manifest checksum.
                self._read_verified_object(
                    key,
                    expected_sha256=digest,
                    allow_missing_metadata=True,
                )
            else:
                self.verify_object(key, expected_sha256=digest)

    def _delete_completion_marker(self, backup_name: str) -> None:
        marker_key = self.manifest_key(backup_name)
        client = self._client()
        self._provider_call(
            f"delete {marker_key}",
            client.delete_object,
            Bucket=self._bucket(),
            Key=marker_key,
        )

    def upload_manifest(
        self,
        source: str | Path,
        *,
        backup_name: str | None = None,
    ) -> S3ObjectInfo:
        """Publish a manifest file as the completion marker.

        Passing a backup directory preserves the original API. Passing an
        explicit manifest file requires ``backup_name`` and lets callers upload
        a staged final manifest before replacing their local pending manifest.
        """

        source_path = Path(source)
        if source_path.is_dir() and not source_path.is_symlink():
            self._backup_files(source_path)
            safe_name = self._backup_name(backup_name or source_path.name)
            manifest = source_path / COMPLETION_MARKER
            backup_dir: Path | None = source_path
        else:
            if backup_name is None:
                raise S3ConfigurationError(
                    "backup_name is required when uploading an explicit manifest file"
                )
            safe_name = self._backup_name(backup_name)
            manifest = source_path
            if not manifest.is_file() or manifest.is_symlink():
                raise S3PathError(f"manifest source is not a regular file: {manifest}")
            backup_dir = None
        self.assert_not_abandoned(safe_name)
        try:
            expected = self._upload_manifest_expectations(
                manifest,
                safe_name,
                backup_dir=backup_dir,
            )
            self._verify_manifest_payloads(safe_name, expected)
            # Re-probe after payload verification. This closes the ordinary
            # delayed-publisher window when a fence was installed while the
            # payload was being checked. This is a direct read, not a claim of
            # conditional-write atomicity.
            self.assert_not_abandoned(safe_name)
        except Exception:
            # A rejected or unverifiable manifest must never remain advertised,
            # including when this method is called directly on a retry.
            self._delete_completion_marker(safe_name)
            raise
        return self.upload_object(
            manifest,
            self._object_key(safe_name, Path(COMPLETION_MARKER)),
        )

    def upload_backup(self, backup_dir: str | Path) -> str:
        """Upload verified payload objects, then publish the manifest last."""

        source = Path(backup_dir)
        # Validate every local completion invariant before deleting an older
        # marker or replacing payloads under the same backup ID.  The manifest
        # is validated again immediately before publication because local
        # files may change during a long upload.
        self._backup_files(source)
        self._upload_manifest_expectations(
            source / COMPLETION_MARKER,
            self._backup_name(source.name),
            backup_dir=source,
        )
        self.upload_backup_payload(source)
        self.upload_manifest(source)
        return f"s3://{self._bucket()}/{self._backup_prefix(source.name)}"

    def remove_completion_marker(self, backup_name: str) -> bool:
        """Remove one exact backup's marker before payload replacement."""

        safe_name = self._backup_name(backup_name)
        marker_key = self.manifest_key(safe_name)
        existed = marker_key in {item.key for item in self.list_objects(safe_name)}
        # S3 DeleteObject is idempotent. Delete even when LIST did not return
        # the marker, since a stale/eventually-consistent listing must not leave
        # an old completion marker in front of new payload uploads.
        self._delete_completion_marker(safe_name)
        return existed

    @staticmethod
    def _metadata_sha256(metadata: object) -> str | None:
        if not isinstance(metadata, dict):
            return None
        normalized = {str(key).lower(): str(value) for key, value in metadata.items()}
        value = normalized.get(SHA256_METADATA_KEY)
        return value.lower() if value else None

    def head_object(self, key: str) -> S3ObjectInfo:
        """Return provider metadata for one object within the configured prefix."""

        scoped_key = self._validate_scoped_key(key)
        client = self._client()
        response = self._provider_call(
            f"head {scoped_key}",
            client.head_object,
            Bucket=self._bucket(),
            Key=scoped_key,
        )
        if not isinstance(response, dict):
            raise S3ProviderError(f"S3 head {scoped_key} returned invalid metadata")
        try:
            size = int(response["ContentLength"])
        except (KeyError, TypeError, ValueError):
            raise S3ProviderError(f"S3 head {scoped_key} omitted ContentLength") from None
        return S3ObjectInfo(
            key=scoped_key,
            size=size,
            sha256=self._metadata_sha256(response.get("Metadata")),
            etag=(str(response["ETag"]).strip('"') if response.get("ETag") is not None else None),
            last_modified=response.get("LastModified"),
        )

    def verify_object(
        self,
        key: str,
        *,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
        allow_missing_metadata: bool = False,
    ) -> S3ObjectInfo:
        """Verify one object's stored SHA-256 metadata and size via HEAD."""

        info = self.head_object(key)
        if info.sha256 is None:
            if not allow_missing_metadata:
                raise S3IntegrityError(f"S3 object {info.key!r} has no valid SHA-256 metadata")
        elif len(info.sha256) != 64 or any(
            character not in "0123456789abcdef" for character in info.sha256
        ):
            raise S3IntegrityError(f"S3 object {info.key!r} has no valid SHA-256 metadata")
        if (
            expected_sha256 is not None
            and info.sha256 is not None
            and info.sha256 != expected_sha256.lower()
        ):
            raise S3IntegrityError(f"S3 object {info.key!r} checksum metadata mismatch")
        if expected_size is not None and info.size != expected_size:
            raise S3IntegrityError(
                f"S3 object {info.key!r} size mismatch: expected {expected_size}, got {info.size}"
            )
        return info

    def _read_verified_object(
        self,
        key: str,
        *,
        expected_sha256: str | None = None,
        collect: bool = False,
        max_bytes: int | None = None,
        allow_missing_metadata: bool = False,
    ) -> tuple[S3ObjectInfo, bytes | None]:
        info = self.verify_object(
            key,
            expected_sha256=expected_sha256,
            allow_missing_metadata=allow_missing_metadata,
        )
        if max_bytes is not None and info.size > max_bytes:
            raise S3IntegrityError(f"S3 object {info.key!r} exceeds the allowed size")
        client = self._client()
        response = self._provider_call(
            f"read {info.key}",
            client.get_object,
            Bucket=self._bucket(),
            Key=info.key,
        )
        if not isinstance(response, dict) or response.get("Body") is None:
            raise S3ProviderError(f"S3 read {info.key} returned no streaming body")
        body = response["Body"]
        digest = hashlib.sha256()
        actual_size = 0
        collected = bytearray() if collect else None
        try:
            while True:
                chunk = body.read(1024 * 1024)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise S3ProviderError(f"S3 read {info.key} returned non-byte content")
                actual_size += len(chunk)
                if max_bytes is not None and actual_size > max_bytes:
                    raise S3IntegrityError(f"S3 object {info.key!r} exceeds the allowed size")
                digest.update(chunk)
                if collected is not None:
                    collected.extend(chunk)
        except S3AdapterError:
            raise
        except Exception as exc:
            detail = redact_text(str(exc), self._configured_env_values())
            raise S3ProviderError(f"S3 read {info.key} failed: {detail}") from None
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    detail = redact_text(str(exc), self._configured_env_values())
                    raise S3ProviderError(f"S3 read {info.key} close failed: {detail}") from None

        actual_sha256 = digest.hexdigest()
        authoritative_sha256 = (
            expected_sha256.lower() if expected_sha256 is not None else info.sha256
        )
        if actual_size != info.size or (
            authoritative_sha256 is not None and actual_sha256 != authoritative_sha256
        ):
            raise S3IntegrityError(f"S3 object {info.key!r} content checksum mismatch")
        return (
            replace(info, sha256=actual_sha256),
            bytes(collected) if collected is not None else None,
        )

    def verify_object_content(
        self,
        key: str,
        *,
        expected_sha256: str | None = None,
    ) -> S3ObjectInfo:
        """Stream one remote object and verify its actual bytes against HEAD."""

        info, _ = self._read_verified_object(
            key,
            expected_sha256=expected_sha256,
        )
        return info

    @staticmethod
    def _manifest_digest(value: object, artifact: str) -> str:
        if not isinstance(value, str):
            raise S3IntegrityError(f"remote manifest has no SHA-256 checksum for {artifact}")
        normalized = value.lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise S3IntegrityError(
                f"remote manifest has an invalid SHA-256 checksum for {artifact}"
            )
        return normalized

    def _backup_manifest_expectations(
        self,
        backup_name: str,
    ) -> tuple[S3ObjectInfo, dict[str, str]]:
        safe_name = self._backup_name(backup_name)
        marker_key = self.manifest_key(safe_name)
        manifest_info, payload = self._read_verified_object(
            marker_key,
            collect=True,
            max_bytes=MAX_MANIFEST_BYTES,
            allow_missing_metadata=True,
        )
        assert payload is not None
        try:
            manifest = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise S3IntegrityError(
                f"remote backup {safe_name!r} has an invalid JSON manifest"
            ) from None
        if not isinstance(manifest, dict):
            raise S3IntegrityError(f"remote backup {safe_name!r} manifest must be a JSON object")
        if manifest.get("backup_id") != safe_name:
            raise S3IntegrityError(f"remote backup {safe_name!r} manifest identity does not match")
        for field in ("project", "environment"):
            value = manifest.get(field)
            if not isinstance(value, str) or not value.strip():
                raise S3IntegrityError(f"remote backup {safe_name!r} manifest has no {field}")

        artifact_paths = manifest.get("artifact_paths")
        if not isinstance(artifact_paths, list) or any(
            not isinstance(path, str) for path in artifact_paths
        ):
            raise S3IntegrityError(
                f"remote backup {safe_name!r} manifest has invalid artifact_paths"
            )
        normalized_paths: set[str] = set()
        for path in artifact_paths:
            parts = self._relative_parts(Path(path))
            if not parts:
                raise S3IntegrityError(
                    f"remote backup {safe_name!r} manifest has an empty artifact path"
                )
            normalized_paths.add(PurePosixPath(*parts).as_posix())

        missing_paths = sorted(set(REQUIRED_ARTIFACT_CHECKSUMS) - normalized_paths)
        if missing_paths:
            raise S3IntegrityError(
                f"remote backup {safe_name!r} manifest is missing required "
                f"artifacts: {', '.join(missing_paths)}"
            )

        checksums = manifest.get("checksums")
        if not isinstance(checksums, dict):
            raise S3IntegrityError(f"remote backup {safe_name!r} manifest has no checksum map")
        expected: dict[str, str] = {}
        for artifact, checksum_name in REQUIRED_ARTIFACT_CHECKSUMS.items():
            checksum = checksums.get(checksum_name, checksums.get(artifact))
            expected[self._object_key(safe_name, Path(artifact))] = self._manifest_digest(
                checksum, artifact
            )
        return manifest_info, expected

    def list_objects(self, backup_name: str | None = None) -> list[S3ObjectInfo]:
        """List objects under the configured prefix or one exact backup prefix."""

        if backup_name is None:
            base = "/".join(self._prefix_parts())
            query_prefix = f"{base}/" if base else ""
        else:
            query_prefix = self._backup_prefix(backup_name) + "/"

        client = self._client()
        continuation: str | None = None
        objects: dict[str, S3ObjectInfo] = {}
        while True:
            kwargs: dict[str, Any] = {
                "Bucket": self._bucket(),
                "Prefix": query_prefix,
            }
            if continuation:
                kwargs["ContinuationToken"] = continuation
            response = self._provider_call(
                f"list prefix {query_prefix!r}",
                client.list_objects_v2,
                **kwargs,
            )
            if not isinstance(response, dict):
                raise S3ProviderError("S3 list returned an invalid response")
            contents = response.get("Contents", [])
            if not isinstance(contents, list):
                raise S3ProviderError("S3 list returned invalid object metadata")
            for item in contents:
                if not isinstance(item, dict) or not item.get("Key"):
                    raise S3ProviderError("S3 list returned an object without a key")
                key = self._validate_scoped_key(str(item["Key"]))
                if not key.startswith(query_prefix):
                    raise S3PathError(f"S3 listed object {key!r} outside requested prefix")
                try:
                    size = int(item.get("Size", 0))
                except (TypeError, ValueError):
                    raise S3ProviderError(f"S3 listed invalid size for object {key!r}") from None
                objects[key] = S3ObjectInfo(
                    key=key,
                    size=size,
                    etag=(str(item["ETag"]).strip('"') if item.get("ETag") is not None else None),
                    last_modified=item.get("LastModified"),
                )
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
            if not token:
                raise S3ProviderError("S3 list response was truncated without a continuation token")
            continuation = str(token)
        return [objects[key] for key in sorted(objects)]

    def list_backups(self) -> list[str]:
        """List backup names that have a root manifest completion marker."""

        prefix = self._prefix_parts()
        completed: set[str] = set()
        for item in self.list_objects():
            parts = tuple(item.key.split("/"))[len(prefix) :]
            if len(parts) == 2 and parts[1] == COMPLETION_MARKER:
                completed.add(self._backup_name(parts[0]))
        return sorted(
            backup_name
            for backup_name in completed
            if not self.abandonment_fence_exists(backup_name)
        )

    def verify_backup(self, backup_name: str) -> list[S3ObjectInfo]:
        """Stream and verify every object in one completed remote backup."""

        self.assert_not_abandoned(backup_name)
        objects = self.list_objects(backup_name)
        marker_key = self.manifest_key(backup_name)
        if not objects or marker_key not in {item.key for item in objects}:
            raise S3IntegrityError(f"remote backup {backup_name!r} has no completion manifest")
        manifest_info, expected = self._backup_manifest_expectations(backup_name)
        object_keys = {item.key for item in objects}
        missing = sorted(set(expected) - object_keys)
        if missing:
            missing_names = ", ".join(Path(key).name for key in missing)
            raise S3IntegrityError(
                f"remote backup {backup_name!r} is missing required artifacts: {missing_names}"
            )
        verified: list[S3ObjectInfo] = []
        for item in objects:
            if item.key == marker_key:
                verified.append(manifest_info)
            else:
                info, _ = self._read_verified_object(
                    item.key,
                    expected_sha256=expected.get(item.key),
                    allow_missing_metadata=True,
                )
                verified.append(info)
        self.assert_not_abandoned(backup_name)
        return verified

    def _download_verified(
        self,
        key: str,
        destination: Path,
        *,
        expected_sha256: str | None = None,
        overwrite: bool = False,
        allow_missing_metadata: bool = False,
    ) -> S3ObjectInfo:
        info = self.verify_object(
            key,
            expected_sha256=expected_sha256,
            allow_missing_metadata=allow_missing_metadata,
        )
        if destination.exists() and not overwrite:
            raise S3PathError(f"download destination already exists: {destination}")
        if destination.is_symlink():
            raise S3PathError(f"download destination must not be a symbolic link: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.partial")
        try:
            client = self._client()
            self._provider_call(
                f"download {info.key}",
                client.download_file,
                self._bucket(),
                info.key,
                str(temporary),
            )
            actual_size = temporary.stat().st_size
            actual_sha256 = self._sha256_file(temporary)
            authoritative_sha256 = (
                expected_sha256.lower() if expected_sha256 is not None else info.sha256
            )
            if actual_size != info.size or (
                authoritative_sha256 is not None and actual_sha256 != authoritative_sha256
            ):
                raise S3IntegrityError(
                    f"downloaded S3 object {info.key!r} failed checksum verification"
                )
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return replace(info, sha256=actual_sha256)

    def download_object(
        self,
        key: str,
        destination: str | Path,
        *,
        expected_sha256: str | None = None,
        overwrite: bool = False,
    ) -> S3ObjectInfo:
        """Download one object atomically and verify its actual bytes."""

        scoped_key = self._validate_scoped_key(key)
        return self._download_verified(
            scoped_key,
            Path(destination),
            expected_sha256=expected_sha256,
            overwrite=overwrite,
        )

    def download_manifest(
        self,
        backup_name: str,
        destination: str | Path,
        *,
        expected_sha256: str | None = None,
        overwrite: bool = False,
    ) -> S3ObjectInfo:
        """Download and byte-verify only a backup's completion manifest."""

        target = Path(destination)
        existed = target.exists()
        self.assert_not_abandoned(backup_name)
        try:
            result = self._download_verified(
                self.manifest_key(backup_name),
                target,
                expected_sha256=expected_sha256,
                overwrite=overwrite,
                allow_missing_metadata=True,
            )
            self.assert_not_abandoned(backup_name)
            return result
        except Exception:
            if not existed:
                target.unlink(missing_ok=True)
            raise

    def download_backup(
        self,
        backup_name: str,
        destination_root: str | Path,
    ) -> Path:
        """Download and verify a complete backup, then atomically publish it."""

        safe_name = self._backup_name(backup_name)
        self.assert_not_abandoned(safe_name)
        objects = self.list_objects(safe_name)
        marker_key = self.manifest_key(safe_name)
        if not objects or marker_key not in {item.key for item in objects}:
            raise S3IntegrityError(f"remote backup {safe_name!r} has no completion manifest")
        manifest_info, expected = self._backup_manifest_expectations(safe_name)
        object_keys = {item.key for item in objects}
        missing = sorted(set(expected) - object_keys)
        if missing:
            missing_names = ", ".join(Path(key).name for key in missing)
            raise S3IntegrityError(
                f"remote backup {safe_name!r} is missing required artifacts: {missing_names}"
            )

        root = Path(destination_root)
        if root.exists() and (not root.is_dir() or root.is_symlink()):
            raise S3PathError(f"download root is not a real directory: {root}")
        root.mkdir(parents=True, exist_ok=True)
        target = root / safe_name
        if target.exists():
            raise S3PathError(f"download target already exists: {target}")
        partial = root / f".{safe_name}.{uuid.uuid4().hex}.partial"
        partial.mkdir()
        try:
            ordered = sorted(
                objects,
                key=lambda item: (
                    item.key == marker_key,
                    item.key,
                ),
            )
            for item in ordered:
                relative = self._relative_from_backup_key(safe_name, item.key)
                self._download_verified(
                    item.key,
                    partial / relative,
                    expected_sha256=(
                        manifest_info.sha256 if item.key == marker_key else expected.get(item.key)
                    ),
                    allow_missing_metadata=True,
                )
            _fsync_tree(partial)
            self.assert_not_abandoned(safe_name)
            os.replace(partial, target)
            _fsync_directory(root)
            try:
                self.assert_not_abandoned(safe_name)
            except Exception:
                shutil.rmtree(target, ignore_errors=True)
                _fsync_directory(root)
                raise
        except Exception:
            shutil.rmtree(partial, ignore_errors=True)
            raise
        return target

    def delete_backup(self, backup_name: str) -> list[str]:
        """Delete backup objects while permanently retaining its abandonment fence."""

        safe_name = self._backup_name(backup_name)
        objects = self.list_objects(safe_name)
        marker_key = self.manifest_key(safe_name)
        # Remove the completion marker first so an interrupted delete is no
        # longer advertised as complete. Delete it unconditionally: LIST can
        # be stale on S3-compatible providers and omit a marker that exists.
        ordered = [
            marker_key,
            *sorted(
                item.key
                for item in objects
                if item.key
                not in {
                    marker_key,
                    self.abandonment_key(safe_name),
                }
            ),
        ]
        client = self._client()
        for key in ordered:
            self._provider_call(
                f"delete {key}",
                client.delete_object,
                Bucket=self._bucket(),
                Key=key,
            )
        return ordered
