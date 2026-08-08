"""Immutable S3-compatible storage for PostgreSQL WAL and PITR base backups.

This adapter intentionally does not share the portable-backup adapter's
replacement, abandonment, or retention semantics. WAL and physical-base
objects are immutable: every write is conditional, and a retry is successful
only when the already-stored bytes and SHA-256 metadata are identical.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Literal

from odooctl.security.redaction import redact_text


SHA256_METADATA_KEY = "sha256"
BASE_COMPLETION_MARKER = "manifest.json"
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_TOKEN_FILE_BYTES = 1024 * 1024
MAX_PIN_BYTES = 32 * 1024 * 1024
DEFAULT_COORDINATION_LEASE_TTL_SECONDS = 300
MIN_COORDINATION_LEASE_TTL_SECONDS = 30
MAX_COORDINATION_LEASE_TTL_SECONDS = 3600
MAX_COORDINATION_CLOCK_SKEW_SECONDS = 60
COORDINATION_LEASE_NAME = "pitr-mutation.json"

_WAL_SEGMENT_RE = re.compile(r"[0-9A-F]{24}")
_WAL_HISTORY_RE = re.compile(r"[0-9A-F]{8}\.history")
_WAL_BACKUP_RE = re.compile(r"[0-9A-F]{24}\.[0-9A-F]{8}\.backup")
_SAFE_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}")
_COORDINATION_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}")
_SYSTEM_IDENTIFIER_RE = re.compile(r"[0-9]{1,32}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_LEASE_ID_RE = re.compile(r"[0-9a-f]{32}")

_STANDARD_AWS_SECRET_ENVS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_ENDPOINT_URL",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
)
_AWS_TOKEN_FILE_ENVS = (
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
)


class WalS3Error(RuntimeError):
    """Base class for WAL/archive storage failures."""


class WalS3DependencyError(WalS3Error):
    """The optional S3 SDK is unavailable."""


class WalS3ConfigurationError(WalS3Error):
    """The archive destination or cluster identity is invalid."""


class WalS3PathError(WalS3Error):
    """A remote key or local path is outside its permitted scope."""


class WalS3ProviderError(WalS3Error):
    """The S3-compatible provider failed an operation."""


class WalS3IntegrityError(WalS3Error):
    """Stored bytes, metadata, or a base manifest failed verification."""


class WalS3ConflictError(WalS3IntegrityError):
    """An immutable key already contains different or unverifiable content."""


class WalS3LeaseError(WalS3Error):
    """The cross-host PITR coordination lease cannot be used safely."""


class WalS3LeaseBusyError(WalS3LeaseError):
    """Another active owner holds the cross-host PITR coordination lease."""


class WalS3LeaseExpiredError(WalS3LeaseError):
    """A stale lease blocks PITR mutations until explicitly recovered."""


class WalS3ClockSkewError(WalS3LeaseError):
    """The local and object-store clocks differ beyond the safe tolerance."""


class WalS3PinError(WalS3IntegrityError):
    """A remote PITR retention pin is malformed, conflicting, or unverifiable."""


class _ObjectNotFound(Exception):
    pass


@dataclass(frozen=True)
class WalObjectInfo:
    key: str
    size: int
    sha256: str
    etag: str | None = None
    last_modified: Any | None = None


@dataclass(frozen=True)
class ImmutableObjectReceipt:
    key: str
    size: int
    sha256: str
    idempotent: bool = False


@dataclass(frozen=True)
class WalArchiveReceipt:
    filename: str
    key: str
    size: int
    sha256: str
    idempotent: bool = False


@dataclass(frozen=True)
class BaseBackupReceipt:
    base_backup_id: str
    uri: str
    manifest: WalObjectInfo
    objects: tuple[WalObjectInfo, ...]
    idempotent: bool = False


@dataclass(frozen=True)
class PitrPinnedWal:
    """One exact immutable WAL object protected by a remote PITR pin."""

    filename: str
    sha256: str
    size: int


@dataclass(frozen=True)
class PitrRemotePin:
    """A byte-verified remote retention pin held in this adapter's scope."""

    kind: Literal["plan", "restore"]
    pin_id: str
    environment: str
    base_backup_id: str
    wal_segments: tuple[PitrPinnedWal, ...]
    key: str
    etag: str
    sha256: str
    last_modified: datetime

    @property
    def first_wal(self) -> str:
        return self.wal_segments[0].filename

    @property
    def last_wal(self) -> str:
        return self.wal_segments[-1].filename


@dataclass(frozen=True)
class PitrCoordinationLease:
    """A proven active cross-host lease for PITR metadata mutations."""

    key: str
    lease_id: str
    owner: str
    purpose: str
    generation: int
    ttl_seconds: int
    etag: str
    acquired_at: datetime
    expires_at: datetime
    observed_at: datetime

    @property
    def expired(self) -> bool:
        return self.observed_at >= self.expires_at


@dataclass(frozen=True)
class _ArtifactExpectation:
    path: str
    sha256: str
    size: int | None = None


@dataclass(frozen=True)
class _BaseManifest:
    raw: dict[str, Any]
    environment: str
    artifacts: tuple[_ArtifactExpectation, ...]


@dataclass(frozen=True)
class _CoordinationHead:
    info: WalObjectInfo | None
    server_time: datetime


def _config_value(config: object, name: str, default: Any = None) -> Any:
    return getattr(config, name, default)


def _canonical_json(document: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _aware_utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


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


def _fsync_tree(root: Path) -> None:
    directories = [root]
    for path in root.rglob("*"):
        if path.is_symlink():
            raise WalS3PathError(f"download tree contains a symbolic link: {path}")
        if path.is_file():
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        elif path.is_dir():
            directories.append(path)
        else:
            raise WalS3PathError(f"download tree contains a non-regular path: {path}")
    for directory in sorted(
        directories,
        key=lambda candidate: len(candidate.parts),
        reverse=True,
    ):
        _fsync_directory(directory)


class WalS3Adapter:
    """Create-only S3 storage scoped to one PostgreSQL cluster identity."""

    def __init__(
        self,
        config: object,
        project: str,
        cluster_id: str,
        system_identifier: str | int,
        boto3_module: Any | None = None,
    ):
        self.config = config
        self.project = self._project(project)
        self.cluster_id = self._component(cluster_id, "cluster_id")
        self.system_identifier = self._system_identifier(system_identifier)
        self._boto3 = boto3_module
        self._s3_client: Any | None = None
        self._cached_secret_values: tuple[str, ...] = ()

    @staticmethod
    def _project(value: str) -> str:
        if (
            not isinstance(value, str)
            or not value.strip()
            or any(ord(character) < 32 for character in value)
        ):
            raise WalS3ConfigurationError("project must be a non-empty printable string")
        return value

    @staticmethod
    def _component(value: object, label: str) -> str:
        normalized = str(value)
        if not _SAFE_COMPONENT_RE.fullmatch(normalized):
            raise WalS3ConfigurationError(f"{label} must be one safe identifier component")
        return normalized

    @staticmethod
    def _printable_label(value: object, label: str) -> str:
        if not isinstance(value, str) or not _COORDINATION_LABEL_RE.fullmatch(value):
            raise WalS3ConfigurationError(f"{label} must be a non-empty printable string")
        return value

    @staticmethod
    def _system_identifier(value: object) -> str:
        normalized = str(value)
        if not _SYSTEM_IDENTIFIER_RE.fullmatch(normalized):
            raise WalS3ConfigurationError(
                "system_identifier must be a PostgreSQL decimal system identifier"
            )
        return normalized

    @staticmethod
    def validate_archive_name(filename: str) -> str:
        if not isinstance(filename, str) or not any(
            pattern.fullmatch(filename)
            for pattern in (_WAL_SEGMENT_RE, _WAL_HISTORY_RE, _WAL_BACKUP_RE)
        ):
            raise WalS3PathError(
                "WAL archive name must be an uppercase segment, timeline history, "
                "or backup-history filename"
            )
        return filename

    @staticmethod
    def _base_backup_id(value: str) -> str:
        if not isinstance(value, str) or not _SAFE_COMPONENT_RE.fullmatch(value):
            raise WalS3PathError("base_backup_id must be one safe path component")
        return value

    @staticmethod
    def _relative_path(value: str | Path) -> str:
        raw = str(value).replace(os.sep, "/")
        path = PurePosixPath(raw)
        if (
            not raw
            or raw in {".", ".."}
            or raw.startswith("/")
            or raw.endswith("/")
            or "\\" in raw
            or "\x00" in raw
            or any(
                not part or part in {".", ".."} or any(ord(character) < 32 for character in part)
                for part in path.parts
            )
        ):
            raise WalS3PathError("base artifact path must be a safe relative POSIX path")
        return path.as_posix()

    def _bucket(self) -> str:
        value = _config_value(self.config, "bucket")
        if (
            not isinstance(value, str)
            or not value
            or value.strip() != value
            or value in {".", ".."}
            or any(character in value for character in ("/", "\\", "\x00"))
            or any(ord(character) < 32 for character in value)
        ):
            raise WalS3ConfigurationError("WAL archive bucket must be a non-empty bucket name")
        return value

    def _prefix_parts(self) -> tuple[str, ...]:
        configured = _config_value(self.config, "prefix", "")
        if configured is None:
            return ()
        raw = str(configured)
        if raw.startswith("/") or "\\" in raw or "\x00" in raw:
            raise WalS3ConfigurationError("WAL archive prefix must be a safe relative POSIX prefix")
        normalized = raw.rstrip("/")
        if not normalized:
            return ()
        parts = tuple(normalized.split("/"))
        if any(
            not part or part in {".", ".."} or any(ord(character) < 32 for character in part)
            for part in parts
        ):
            raise WalS3ConfigurationError("WAL archive prefix contains unsafe path segments")
        return parts

    @property
    def project_namespace(self) -> str:
        slug = re.sub(r"[^a-z0-9-]+", "-", self.project.lower()).strip("-")[:32] or "project"
        digest = hashlib.sha256(self.project.encode()).hexdigest()[:24]
        return f"{slug}-{digest}"

    @property
    def scope_prefix(self) -> str:
        return "/".join(
            (
                *self._prefix_parts(),
                "projects",
                self.project_namespace,
                "clusters",
                self.cluster_id,
                self.system_identifier,
            )
        )

    @property
    def wal_prefix(self) -> str:
        return f"{self.scope_prefix}/wal"

    @property
    def bases_prefix(self) -> str:
        return f"{self.scope_prefix}/bases"

    @property
    def coordination_prefix(self) -> str:
        return f"{self.scope_prefix}/coordination"

    @property
    def coordination_lease_key(self) -> str:
        return f"{self.coordination_prefix}/{COORDINATION_LEASE_NAME}"

    @property
    def pins_prefix(self) -> str:
        return f"{self.scope_prefix}/pins"

    def wal_key(self, filename: str) -> str:
        return f"{self.wal_prefix}/{self.validate_archive_name(filename)}"

    def base_prefix(self, base_backup_id: str) -> str:
        return f"{self.bases_prefix}/{self._base_backup_id(base_backup_id)}"

    def base_manifest_key(self, base_backup_id: str) -> str:
        return f"{self.base_prefix(base_backup_id)}/{BASE_COMPLETION_MARKER}"

    def base_object_key(self, base_backup_id: str, relative_path: str | Path) -> str:
        relative = self._relative_path(relative_path)
        return f"{self.base_prefix(base_backup_id)}/{relative}"

    @staticmethod
    def _pin_kind(value: object) -> Literal["plan", "restore"]:
        if value not in {"plan", "restore"}:
            raise WalS3PathError("PITR pin kind must be 'plan' or 'restore'")
        return value

    def pin_key(self, kind: str, pin_id: str) -> str:
        safe_kind = self._pin_kind(kind)
        safe_id = self._component(pin_id, "pin_id")
        return f"{self.pins_prefix}/{safe_kind}/{safe_id}.json"

    def _required_env(self, reference: str, purpose: str) -> str:
        value = os.getenv(reference)
        if not value:
            raise WalS3ConfigurationError(f"Missing {purpose} environment variable: {reference}")
        return value

    def _credentials(self) -> dict[str, str]:
        access_ref = _config_value(self.config, "access_key_env")
        secret_ref = _config_value(self.config, "secret_key_env")
        session_ref = _config_value(self.config, "session_token_env")
        if bool(access_ref) != bool(secret_ref):
            raise WalS3ConfigurationError(
                "WAL archive access_key_env and secret_key_env must be configured together"
            )
        if session_ref and not access_ref:
            raise WalS3ConfigurationError(
                "WAL archive session_token_env requires static credential references"
            )
        if not access_ref:
            return {}
        result = {
            "aws_access_key_id": self._required_env(
                str(access_ref),
                "WAL archive access key",
            ),
            "aws_secret_access_key": self._required_env(
                str(secret_ref),
                "WAL archive secret key",
            ),
        }
        if session_ref:
            result["aws_session_token"] = self._required_env(
                str(session_ref),
                "WAL archive session token",
            )
        return result

    def _region(self) -> str | None:
        reference = _config_value(self.config, "region_env")
        if reference:
            return self._required_env(str(reference), "WAL archive region")
        configured = _config_value(self.config, "region")
        return (
            str(configured)
            if configured
            else os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
        )

    def _endpoint(self) -> str | None:
        reference = _config_value(self.config, "endpoint_env")
        if reference:
            return self._required_env(str(reference), "WAL archive endpoint")
        configured = _config_value(self.config, "endpoint_url")
        return str(configured) if configured else None

    def _sse_args(self) -> dict[str, str]:
        algorithm = _config_value(self.config, "encryption_algorithm")
        key_ref = _config_value(self.config, "encryption_key_env")
        if key_ref and not algorithm:
            raise WalS3ConfigurationError(
                "WAL archive encryption_key_env requires encryption_algorithm"
            )
        if not algorithm:
            return {}
        result = {"ServerSideEncryption": str(algorithm)}
        if key_ref:
            result["SSEKMSKeyId"] = self._required_env(
                str(key_ref),
                "WAL archive encryption key",
            )
        return result

    @staticmethod
    def _token_file_values(reference: str) -> tuple[str, ...]:
        try:
            metadata = os.stat(reference)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_TOKEN_FILE_BYTES:
                return (reference,)
            payload = Path(reference).read_bytes()
            if len(payload) > MAX_TOKEN_FILE_BYTES:
                return (reference,)
            token = payload.decode("utf-8")
        except (OSError, UnicodeError):
            return (reference,)
        return tuple(dict.fromkeys(value for value in (reference, token, token.strip()) if value))

    def _configured_secret_values(self) -> tuple[str, ...]:
        references = (
            _config_value(self.config, "endpoint_env"),
            _config_value(self.config, "access_key_env"),
            _config_value(self.config, "secret_key_env"),
            _config_value(self.config, "session_token_env"),
            _config_value(self.config, "region_env"),
            _config_value(self.config, "encryption_key_env"),
            *_STANDARD_AWS_SECRET_ENVS,
        )
        values = [
            value for reference in references if reference and (value := os.getenv(str(reference)))
        ]
        for token_file_env in _AWS_TOKEN_FILE_ENVS:
            reference = os.getenv(token_file_env)
            if reference:
                values.extend(self._token_file_values(reference))
        return tuple(dict.fromkeys((*self._cached_secret_values, *values)))

    def _load_boto3(self) -> Any:
        if self._boto3 is not None:
            return self._boto3
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:
            raise WalS3DependencyError(
                "boto3 is required for WAL archiving; install odooctl[s3]"
            ) from exc
        return boto3

    def _client(self) -> Any:
        if self._s3_client is not None:
            return self._s3_client
        credentials = self._credentials()
        self._cached_secret_values = tuple(
            dict.fromkeys((*self._cached_secret_values, *self._configured_secret_values()))
        )
        kwargs: dict[str, Any] = {**credentials}
        region = self._region()
        endpoint = self._endpoint()
        if region:
            kwargs["region_name"] = region
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        try:
            self._s3_client = self._load_boto3().client("s3", **kwargs)
        except Exception as exc:
            raise self._provider_error("client initialization", exc) from None
        return self._s3_client

    def _provider_error(self, operation: str, exc: BaseException) -> WalS3ProviderError:
        detail = redact_text(str(exc), self._configured_secret_values())
        return WalS3ProviderError(f"S3 {operation} failed: {detail}")

    @staticmethod
    def _error_code(exc: BaseException) -> tuple[str, int | None]:
        response = getattr(exc, "response", None)
        if not isinstance(response, dict):
            return "", None
        error = response.get("Error")
        metadata = response.get("ResponseMetadata")
        code = str(error.get("Code", "")) if isinstance(error, dict) else ""
        status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
        return code, status if isinstance(status, int) else None

    @classmethod
    def _is_not_found(cls, exc: BaseException) -> bool:
        code, status = cls._error_code(exc)
        return code in {"404", "NoSuchKey", "NotFound"} or status == 404

    @staticmethod
    def _metadata_digest(metadata: object) -> str | None:
        if not isinstance(metadata, dict):
            return None
        normalized = {str(key).lower(): str(value).lower() for key, value in metadata.items()}
        digest = normalized.get(SHA256_METADATA_KEY)
        return digest if digest and _DIGEST_RE.fullmatch(digest) else None

    def _head_info(self, key: str, response: object) -> WalObjectInfo:
        if not isinstance(response, dict):
            raise WalS3ProviderError(f"S3 head {key} returned invalid metadata")
        try:
            size = int(response["ContentLength"])
        except (KeyError, TypeError, ValueError):
            raise WalS3ProviderError(f"S3 head {key} omitted a valid ContentLength") from None
        digest = self._metadata_digest(response.get("Metadata"))
        if digest is None:
            raise WalS3IntegrityError(f"S3 object {key!r} has no valid SHA-256 metadata")
        return WalObjectInfo(
            key=key,
            size=size,
            sha256=digest,
            etag=(str(response["ETag"]).strip('"') if response.get("ETag") is not None else None),
            last_modified=response.get("LastModified"),
        )

    def _head(self, key: str) -> WalObjectInfo:
        client = self._client()
        try:
            response = client.head_object(Bucket=self._bucket(), Key=key)
        except Exception as exc:
            if self._is_not_found(exc):
                raise _ObjectNotFound(key) from None
            raise self._provider_error(f"head {key}", exc) from None
        return self._head_info(key, response)

    @staticmethod
    def _response_server_time(response: object, operation: str) -> datetime:
        if not isinstance(response, dict):
            raise WalS3LeaseError(f"S3 {operation} returned no authoritative server time")
        metadata = response.get("ResponseMetadata")
        headers = metadata.get("HTTPHeaders") if isinstance(metadata, dict) else None
        if not isinstance(headers, dict):
            raise WalS3LeaseError(f"S3 {operation} returned no authoritative server time")
        date_value = next(
            (value for key, value in headers.items() if str(key).lower() == "date"),
            None,
        )
        if not isinstance(date_value, str):
            raise WalS3LeaseError(f"S3 {operation} returned no authoritative server time")
        try:
            parsed = parsedate_to_datetime(date_value)
        except (TypeError, ValueError, OverflowError):
            raise WalS3LeaseError(f"S3 {operation} returned an invalid server time") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise WalS3LeaseError(f"S3 {operation} returned a timezone-naive server time")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _exception_response(exc: BaseException) -> object:
        response = getattr(exc, "response", None)
        return response if isinstance(response, dict) else {}

    def _validate_coordination_time(
        self,
        response: object,
        operation: str,
        now: datetime | None,
    ) -> datetime:
        local_time = _aware_utc(
            now or datetime.now(timezone.utc),
            "coordination clock",
        )
        server_time = self._response_server_time(response, operation)
        skew = abs((local_time - server_time).total_seconds())
        if skew > MAX_COORDINATION_CLOCK_SKEW_SECONDS:
            raise WalS3ClockSkewError(
                "Local clock differs from S3 server time by "
                f"{skew:.0f}s; maximum safe skew is "
                f"{MAX_COORDINATION_CLOCK_SKEW_SECONDS}s"
            )
        return server_time

    def _coordination_head(
        self,
        key: str,
        *,
        now: datetime | None,
    ) -> _CoordinationHead:
        client = self._client()
        try:
            response = client.head_object(Bucket=self._bucket(), Key=key)
        except Exception as exc:
            server_time = self._validate_coordination_time(
                self._exception_response(exc),
                f"head {key}",
                now,
            )
            if self._is_not_found(exc):
                return _CoordinationHead(info=None, server_time=server_time)
            raise self._provider_error(f"head {key}", exc) from None

        server_time = self._validate_coordination_time(
            response,
            f"head {key}",
            now,
        )
        info = self._head_info(key, response)
        if not info.etag:
            raise WalS3LeaseError(f"S3 coordination object {key!r} has no ETag")
        if (
            not isinstance(info.last_modified, datetime)
            or info.last_modified.tzinfo is None
            or info.last_modified.utcoffset() is None
        ):
            raise WalS3LeaseError(
                f"S3 coordination object {key!r} has no authoritative LastModified time"
            )
        modified = info.last_modified.astimezone(timezone.utc)
        if (modified - server_time).total_seconds() > MAX_COORDINATION_CLOCK_SKEW_SECONDS:
            raise WalS3ClockSkewError(
                f"S3 coordination object {key!r} has a future LastModified time"
            )
        return _CoordinationHead(
            info=WalObjectInfo(
                key=info.key,
                size=info.size,
                sha256=info.sha256,
                etag=info.etag,
                last_modified=modified,
            ),
            server_time=server_time,
        )

    def _open_remote_body(self, key: str) -> Any:
        client = self._client()
        try:
            response = client.get_object(Bucket=self._bucket(), Key=key)
        except Exception as exc:
            if self._is_not_found(exc):
                raise _ObjectNotFound(key) from None
            raise self._provider_error(f"read {key}", exc) from None
        if not isinstance(response, dict) or response.get("Body") is None:
            raise WalS3ProviderError(f"S3 read {key} returned no body")
        return response["Body"]

    def _read_verified(
        self,
        key: str,
        *,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
        collect: bool = False,
        max_bytes: int | None = None,
    ) -> tuple[WalObjectInfo, bytes | None]:
        info = self._head(key)
        expected_digest = expected_sha256.lower() if expected_sha256 is not None else info.sha256
        if not _DIGEST_RE.fullmatch(expected_digest):
            raise WalS3IntegrityError(f"invalid expected checksum for {key!r}")
        if info.sha256 != expected_digest:
            raise WalS3IntegrityError(f"S3 object {key!r} checksum metadata mismatch")
        if expected_size is not None and info.size != expected_size:
            raise WalS3IntegrityError(
                f"S3 object {key!r} size mismatch: expected {expected_size}, got {info.size}"
            )
        if max_bytes is not None and info.size > max_bytes:
            raise WalS3IntegrityError(f"S3 object {key!r} exceeds the size limit")

        body = self._open_remote_body(key)
        digest = hashlib.sha256()
        size = 0
        output = bytearray() if collect else None
        try:
            while True:
                chunk = body.read(1024 * 1024)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise WalS3ProviderError(f"S3 read {key} returned non-byte content")
                size += len(chunk)
                if max_bytes is not None and size > max_bytes:
                    raise WalS3IntegrityError(f"S3 object {key!r} exceeds the size limit")
                digest.update(chunk)
                if output is not None:
                    output.extend(chunk)
        except WalS3Error:
            raise
        except Exception as exc:
            raise self._provider_error(f"read {key}", exc) from None
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    raise self._provider_error(f"close {key}", exc) from None

        if size != info.size or digest.hexdigest() != expected_digest:
            raise WalS3IntegrityError(f"S3 object {key!r} content checksum mismatch")
        return info, bytes(output) if output is not None else None

    def _reconcile_immutable(
        self,
        key: str,
        *,
        expected_sha256: str,
        expected_size: int,
    ) -> WalObjectInfo:
        try:
            info, _ = self._read_verified(
                key,
                expected_sha256=expected_sha256,
                expected_size=expected_size,
            )
        except _ObjectNotFound:
            raise
        except WalS3ProviderError:
            raise
        except WalS3IntegrityError as exc:
            raise WalS3ConflictError(
                f"immutable S3 key {key!r} already contains different or unverifiable content"
            ) from exc
        return info

    def _put_immutable(
        self,
        source: Path,
        key: str,
    ) -> ImmutableObjectReceipt:
        if not source.is_file() or source.is_symlink():
            raise WalS3PathError(f"upload source is not a regular file: {source}")
        digest = _sha256_file(source)
        size = source.stat().st_size
        client = self._client()
        try:
            with source.open("rb") as handle:
                client.put_object(
                    Bucket=self._bucket(),
                    Key=key,
                    Body=handle,
                    IfNoneMatch="*",
                    Metadata={SHA256_METADATA_KEY: digest},
                    **self._sse_args(),
                )
        except Exception as original:
            try:
                info = self._reconcile_immutable(
                    key,
                    expected_sha256=digest,
                    expected_size=size,
                )
            except _ObjectNotFound:
                raise self._provider_error(f"create {key}", original) from None
            except WalS3ConflictError:
                raise
            except WalS3ProviderError:
                raise self._provider_error(f"create {key}", original) from None
            return ImmutableObjectReceipt(
                key=key,
                size=info.size,
                sha256=info.sha256,
                idempotent=True,
            )

        info, _ = self._read_verified(
            key,
            expected_sha256=digest,
            expected_size=size,
        )
        return ImmutableObjectReceipt(
            key=key,
            size=info.size,
            sha256=info.sha256,
        )

    def _put_bytes_immutable(
        self,
        payload: bytes,
        key: str,
    ) -> ImmutableObjectReceipt:
        digest = hashlib.sha256(payload).hexdigest()
        size = len(payload)
        client = self._client()
        try:
            client.put_object(
                Bucket=self._bucket(),
                Key=key,
                Body=io.BytesIO(payload),
                IfNoneMatch="*",
                Metadata={SHA256_METADATA_KEY: digest},
                **self._sse_args(),
            )
        except Exception as original:
            try:
                info = self._reconcile_immutable(
                    key,
                    expected_sha256=digest,
                    expected_size=size,
                )
            except _ObjectNotFound:
                raise self._provider_error(f"create {key}", original) from None
            except WalS3ConflictError:
                raise
            except WalS3ProviderError:
                raise self._provider_error(f"create {key}", original) from None
            return ImmutableObjectReceipt(
                key=key,
                size=info.size,
                sha256=info.sha256,
                idempotent=True,
            )

        info, _ = self._read_verified(
            key,
            expected_sha256=digest,
            expected_size=size,
        )
        return ImmutableObjectReceipt(
            key=key,
            size=info.size,
            sha256=info.sha256,
        )

    @classmethod
    def _is_conditional_conflict(cls, exc: BaseException) -> bool:
        code, status = cls._error_code(exc)
        return code in {
            "409",
            "412",
            "ConditionalRequestConflict",
            "PreconditionFailed",
        } or status in {409, 412}

    @staticmethod
    def _if_match_etag(etag: str) -> str:
        if (
            not etag
            or '"' in etag
            or "\\" in etag
            or any(ord(character) < 33 or ord(character) > 126 for character in etag)
        ):
            raise WalS3LeaseError("S3 coordination object has an unsafe ETag")
        # HTTP entity tags are quoted. boto3 serializes this value directly
        # into If-Match rather than adding the entity-tag quotes itself.
        return f'"{etag}"'

    def _read_stable_coordination_payload(
        self,
        key: str,
        *,
        now: datetime | None,
        max_bytes: int,
    ) -> tuple[_CoordinationHead, bytes]:
        before = self._coordination_head(key, now=now)
        if before.info is None:
            raise _ObjectNotFound(key)
        info, payload = self._read_verified(
            key,
            expected_sha256=before.info.sha256,
            expected_size=before.info.size,
            collect=True,
            max_bytes=max_bytes,
        )
        assert payload is not None
        after = self._coordination_head(key, now=now)
        if (
            after.info is None
            or after.info.etag != before.info.etag
            or after.info.sha256 != before.info.sha256
            or info.sha256 != before.info.sha256
        ):
            raise WalS3ConflictError(f"S3 coordination object {key!r} changed while it was read")
        return after, payload

    def archive_wal(
        self,
        source: str | Path,
        filename: str | None = None,
    ) -> WalArchiveReceipt:
        source_path = Path(source)
        safe_filename = self.validate_archive_name(
            filename if filename is not None else source_path.name
        )
        receipt = self._put_immutable(
            source_path,
            self.wal_key(safe_filename),
        )
        return WalArchiveReceipt(
            filename=safe_filename,
            key=receipt.key,
            size=receipt.size,
            sha256=receipt.sha256,
            idempotent=receipt.idempotent,
        )

    upload_wal = archive_wal

    def verify_wal(
        self,
        filename: str,
        *,
        expected_sha256: str | None = None,
    ) -> WalObjectInfo:
        info, _ = self._read_verified(
            self.wal_key(filename),
            expected_sha256=expected_sha256,
        )
        return info

    def _list_objects(self, prefix: str) -> list[tuple[str, int, Any | None]]:
        client = self._client()
        continuation: str | None = None
        objects: dict[str, tuple[int, Any | None]] = {}
        while True:
            kwargs: dict[str, Any] = {
                "Bucket": self._bucket(),
                "Prefix": prefix,
            }
            if continuation:
                kwargs["ContinuationToken"] = continuation
            try:
                response = client.list_objects_v2(**kwargs)
            except Exception as exc:
                raise self._provider_error(f"list {prefix}", exc) from None
            if not isinstance(response, dict):
                raise WalS3ProviderError("S3 list returned an invalid response")
            contents = response.get("Contents", [])
            if not isinstance(contents, list):
                raise WalS3ProviderError("S3 list returned invalid object metadata")
            for item in contents:
                if not isinstance(item, dict) or not isinstance(
                    item.get("Key"),
                    str,
                ):
                    raise WalS3ProviderError("S3 list returned an object without a key")
                key = item["Key"]
                # Some S3-compatible fakes and gateways have returned entries
                # outside the requested prefix. Ignore them; never infer
                # ownership from their shape.
                if not key.startswith(prefix):
                    continue
                try:
                    size = int(item.get("Size", 0))
                except (TypeError, ValueError):
                    raise WalS3ProviderError(f"S3 listed an invalid size for {key!r}") from None
                objects[key] = (size, item.get("LastModified"))
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
            if not token:
                raise WalS3ProviderError("S3 list was truncated without a continuation token")
            continuation = str(token)
        return [(key, *objects[key]) for key in sorted(objects)]

    def _assert_owned_key(self, key: str) -> str:
        scope = f"{self.scope_prefix}/"
        if (
            not isinstance(key, str)
            or not key.startswith(scope)
            or key.startswith("/")
            or key.endswith("/")
            or "\\" in key
            or "\x00" in key
        ):
            raise WalS3PathError("S3 object key is outside this WAL archive scope")
        relative = key[len(scope) :]
        if any(
            not part or part in {".", ".."} or any(ord(character) < 32 for character in part)
            for part in relative.split("/")
        ):
            raise WalS3PathError("S3 object key has unsafe path segments")
        return key

    def _delete_owned_key(self, key: str) -> None:
        owned = self._assert_owned_key(key)
        client = self._client()
        try:
            client.delete_object(Bucket=self._bucket(), Key=owned)
        except Exception as exc:
            raise self._provider_error(f"delete {owned}", exc) from None

    def list_wal(self) -> list[WalObjectInfo]:
        prefix = f"{self.wal_prefix}/"
        result: list[WalObjectInfo] = []
        for key, _size, _modified in self._list_objects(prefix):
            relative = key[len(prefix) :]
            if "/" in relative:
                continue
            try:
                self.validate_archive_name(relative)
            except WalS3PathError:
                continue
            result.append(self._head(key))
        return result

    def delete_wal(self, filename: str) -> str:
        """Delete one validated WAL object within this exact cluster scope."""

        key = self.wal_key(filename)
        self._delete_owned_key(key)
        return key

    @staticmethod
    def _digest(value: object, label: str) -> str:
        normalized = str(value).lower() if isinstance(value, str) else ""
        if not _DIGEST_RE.fullmatch(normalized):
            raise WalS3IntegrityError(f"base manifest has no valid SHA-256 for {label}")
        return normalized

    @staticmethod
    def _artifact_size(value: object, label: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise WalS3IntegrityError(f"base manifest has an invalid size for {label}")
        try:
            size = int(value)
        except (TypeError, ValueError):
            raise WalS3IntegrityError(f"base manifest has an invalid size for {label}") from None
        if size < 0:
            raise WalS3IntegrityError(f"base manifest has an invalid size for {label}")
        return size

    def _manifest_artifacts(
        self,
        manifest: dict[str, Any],
    ) -> tuple[_ArtifactExpectation, ...]:
        checksums = manifest.get("checksums")
        sizes = manifest.get("sizes", {})
        if checksums is None:
            checksums = {}
        if not isinstance(checksums, dict) or not isinstance(sizes, dict):
            raise WalS3IntegrityError("base manifest checksums and sizes must be mappings")

        artifacts = manifest.get("artifacts")
        if artifacts is None:
            artifacts = manifest.get("artifact_paths")
        entries: list[_ArtifactExpectation] = []

        if isinstance(artifacts, dict):
            iterable: list[Any] = []
            for path, details in artifacts.items():
                if isinstance(details, dict):
                    iterable.append({"path": path, **details})
                else:
                    iterable.append({"path": path, "sha256": details})
        elif isinstance(artifacts, list):
            iterable = artifacts
        else:
            raise WalS3IntegrityError("base manifest must declare a non-empty artifact collection")

        for item in iterable:
            if isinstance(item, str):
                path = self._relative_path(item)
                digest_value = checksums.get(item, checksums.get(path))
                size_value = sizes.get(item, sizes.get(path))
            elif isinstance(item, dict):
                path = self._relative_path(str(item.get("path", "")))
                digest_value = item.get(
                    "sha256",
                    checksums.get(path),
                )
                size_value = item.get("size", sizes.get(path))
            else:
                raise WalS3IntegrityError("base manifest contains an invalid artifact entry")
            if path == BASE_COMPLETION_MARKER:
                raise WalS3IntegrityError(
                    "base completion manifest cannot declare itself as payload"
                )
            entries.append(
                _ArtifactExpectation(
                    path=path,
                    sha256=self._digest(digest_value, path),
                    size=self._artifact_size(size_value, path),
                )
            )

        if not entries:
            raise WalS3IntegrityError("base manifest must declare at least one artifact")
        paths = [entry.path for entry in entries]
        if len(paths) != len(set(paths)):
            raise WalS3IntegrityError("base manifest declares duplicate artifact paths")
        return tuple(sorted(entries, key=lambda entry: entry.path))

    def _parse_base_manifest(
        self,
        payload: bytes,
        base_backup_id: str,
    ) -> _BaseManifest:
        try:
            manifest = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise WalS3IntegrityError("base completion manifest is not valid JSON") from None
        if not isinstance(manifest, dict):
            raise WalS3IntegrityError("base completion manifest must be a JSON object")

        canonical_id = manifest.get("base_backup_id")
        legacy_id = manifest.get("base_id")
        if canonical_id is not None and legacy_id is not None and canonical_id != legacy_id:
            raise WalS3IntegrityError("base completion manifest has conflicting base identities")
        manifest_id = canonical_id if canonical_id is not None else legacy_id
        if manifest_id != base_backup_id:
            raise WalS3IntegrityError("base completion manifest identity does not match its key")
        if manifest.get("project") != self.project:
            raise WalS3IntegrityError(
                "base completion manifest project does not match archive scope"
            )
        if manifest.get("cluster_id") != self.cluster_id:
            raise WalS3IntegrityError(
                "base completion manifest cluster does not match archive scope"
            )
        if str(manifest.get("system_identifier", "")) != self.system_identifier:
            raise WalS3IntegrityError(
                "base completion manifest system identifier does not match archive scope"
            )
        environment = manifest.get("environment")
        if not isinstance(environment, str) or not environment.strip():
            raise WalS3IntegrityError("base completion manifest has no environment")
        if manifest.get("status") != "complete":
            raise WalS3IntegrityError("base completion manifest status must be 'complete'")
        return _BaseManifest(
            raw=manifest,
            environment=environment,
            artifacts=self._manifest_artifacts(manifest),
        )

    def _base_files(self, base_dir: Path) -> list[tuple[Path, str]]:
        if not base_dir.is_dir() or base_dir.is_symlink():
            raise WalS3PathError(f"base backup source is not a real directory: {base_dir}")
        files: list[tuple[Path, str]] = []
        for path in sorted(
            base_dir.rglob("*"),
            key=lambda candidate: candidate.relative_to(base_dir).as_posix(),
        ):
            if path.is_symlink():
                raise WalS3PathError(f"base backup tree contains a symbolic link: {path}")
            if path.is_dir():
                continue
            if not path.is_file():
                raise WalS3PathError(f"base backup tree contains a non-regular file: {path}")
            relative = self._relative_path(path.relative_to(base_dir).as_posix())
            files.append((path, relative))
        return files

    def _validate_local_base(
        self,
        base_dir: Path,
        manifest: _BaseManifest,
    ) -> None:
        files = {
            relative: path
            for path, relative in self._base_files(base_dir)
            if relative != BASE_COMPLETION_MARKER
        }
        expected = {artifact.path: artifact for artifact in manifest.artifacts}
        if set(files) != set(expected):
            missing = sorted(set(expected) - set(files))
            unexpected = sorted(set(files) - set(expected))
            details: list[str] = []
            if missing:
                details.append("missing: " + ", ".join(missing))
            if unexpected:
                details.append("unexpected: " + ", ".join(unexpected))
            raise WalS3IntegrityError(
                "local base payload does not match its manifest (" + "; ".join(details) + ")"
            )
        for relative, artifact in expected.items():
            source = files[relative]
            size = source.stat().st_size
            if artifact.size is not None and size != artifact.size:
                raise WalS3IntegrityError(f"local base artifact {relative!r} size does not match")
            if _sha256_file(source) != artifact.sha256:
                raise WalS3IntegrityError(
                    f"local base artifact {relative!r} checksum does not match"
                )

    def upload_base_payload(
        self,
        base_backup_id: str,
        base_dir: str | Path,
    ) -> tuple[ImmutableObjectReceipt, ...]:
        safe_id = self._base_backup_id(base_backup_id)
        uploaded: list[ImmutableObjectReceipt] = []
        for source, relative in self._base_files(Path(base_dir)):
            if relative == BASE_COMPLETION_MARKER:
                continue
            uploaded.append(
                self._put_immutable(
                    source,
                    self.base_object_key(safe_id, relative),
                )
            )
        return tuple(uploaded)

    def _inspect_base(
        self,
        base_backup_id: str,
        *,
        verify_payload: bool,
    ) -> tuple[WalObjectInfo, _BaseManifest, tuple[WalObjectInfo, ...]]:
        safe_id = self._base_backup_id(base_backup_id)
        marker_key = self.base_manifest_key(safe_id)
        marker, payload = self._read_verified(
            marker_key,
            collect=True,
            max_bytes=MAX_MANIFEST_BYTES,
        )
        assert payload is not None
        manifest = self._parse_base_manifest(payload, safe_id)
        expected_by_key = {
            self.base_object_key(safe_id, artifact.path): artifact
            for artifact in manifest.artifacts
        }
        listed = {
            key for key, _size, _modified in self._list_objects(f"{self.base_prefix(safe_id)}/")
        }
        expected_keys = {marker_key, *expected_by_key}
        if listed != expected_keys:
            missing = sorted(expected_keys - listed)
            unexpected = sorted(listed - expected_keys)
            details: list[str] = []
            if missing:
                details.append("missing: " + ", ".join(missing))
            if unexpected:
                details.append("unexpected: " + ", ".join(unexpected))
            raise WalS3IntegrityError(
                "base backup object inventory does not match its manifest ("
                + "; ".join(details)
                + ")"
            )

        objects: list[WalObjectInfo] = []
        for key, artifact in sorted(expected_by_key.items()):
            if verify_payload:
                info, _ = self._read_verified(
                    key,
                    expected_sha256=artifact.sha256,
                    expected_size=artifact.size,
                )
            else:
                info = self._head(key)
                if info.sha256 != artifact.sha256 or (
                    artifact.size is not None and info.size != artifact.size
                ):
                    raise WalS3IntegrityError(
                        f"base artifact {artifact.path!r} metadata does not match"
                    )
            objects.append(info)
        return marker, manifest, tuple(objects)

    def publish_base_manifest(
        self,
        base_backup_id: str,
        manifest_path: str | Path,
    ) -> BaseBackupReceipt:
        safe_id = self._base_backup_id(base_backup_id)
        source = Path(manifest_path)
        if not source.is_file() or source.is_symlink():
            raise WalS3PathError(f"base manifest source is not a regular file: {source}")
        if source.stat().st_size > MAX_MANIFEST_BYTES:
            raise WalS3IntegrityError("base completion manifest exceeds the size limit")
        manifest = self._parse_base_manifest(source.read_bytes(), safe_id)
        for artifact in manifest.artifacts:
            self._read_verified(
                self.base_object_key(safe_id, artifact.path),
                expected_sha256=artifact.sha256,
                expected_size=artifact.size,
            )
        marker_key = self.base_manifest_key(safe_id)
        expected_keys = {
            self.base_object_key(safe_id, artifact.path) for artifact in manifest.artifacts
        }
        listed = {
            key for key, _size, _modified in self._list_objects(f"{self.base_prefix(safe_id)}/")
        }
        if listed not in (expected_keys, expected_keys | {marker_key}):
            raise WalS3IntegrityError(
                "refusing to publish a base manifest over an unexpected "
                "or incomplete object inventory"
            )
        marker_receipt = self._put_immutable(
            source,
            marker_key,
        )
        try:
            verified = self.verify_base_backup(safe_id)
        except Exception:
            # If this call created the marker, do not leave an invalid base
            # advertised. An idempotent retry did not create the marker and
            # therefore must not delete another publisher's completion record.
            if not marker_receipt.idempotent:
                self._delete_owned_key(marker_key)
            raise
        return BaseBackupReceipt(
            base_backup_id=verified.base_backup_id,
            uri=verified.uri,
            manifest=verified.manifest,
            objects=verified.objects,
            idempotent=marker_receipt.idempotent,
        )

    def upload_base_backup(
        self,
        base_backup_id: str,
        base_dir: str | Path,
    ) -> BaseBackupReceipt:
        safe_id = self._base_backup_id(base_backup_id)
        root = Path(base_dir)
        manifest_path = root / BASE_COMPLETION_MARKER
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise WalS3IntegrityError(f"base backup is missing root {BASE_COMPLETION_MARKER}")
        # Reject an invalid manifest before creating immutable orphan payload.
        manifest = self._parse_base_manifest(
            manifest_path.read_bytes(),
            safe_id,
        )
        self._validate_local_base(root, manifest)
        self.upload_base_payload(safe_id, root)
        return self.publish_base_manifest(safe_id, manifest_path)

    def list_base_backups(self) -> list[str]:
        prefix = f"{self.bases_prefix}/"
        completed: set[str] = set()
        for key, _size, _modified in self._list_objects(prefix):
            relative = key[len(prefix) :]
            parts = relative.split("/")
            if len(parts) != 2 or parts[1] != BASE_COMPLETION_MARKER:
                continue
            try:
                completed.add(self._base_backup_id(parts[0]))
            except WalS3PathError:
                continue
        return sorted(completed)

    def verify_base_backup(self, base_backup_id: str) -> BaseBackupReceipt:
        safe_id = self._base_backup_id(base_backup_id)
        marker, _manifest, objects = self._inspect_base(
            safe_id,
            verify_payload=True,
        )
        return BaseBackupReceipt(
            base_backup_id=safe_id,
            uri=f"s3://{self._bucket()}/{self.base_prefix(safe_id)}",
            manifest=marker,
            objects=objects,
        )

    def read_base_manifest(self, base_backup_id: str) -> dict[str, Any]:
        """Return an authoritative, byte-verified remote completion document."""

        safe_id = self._base_backup_id(base_backup_id)
        _marker, manifest, _objects = self._inspect_base(
            safe_id,
            verify_payload=True,
        )
        # Round-trip through JSON so callers cannot mutate nested values held
        # by an internal parsed-document reference.
        return json.loads(json.dumps(manifest.raw))

    def delete_base_backup(
        self,
        base_backup_id: str,
        *,
        before_delete: Callable[[str], None] | None = None,
    ) -> tuple[str, ...]:
        """Unpublish, then delete one validated base backup.

        Every key is resolved from this adapter's project/cluster/system scope.
        The completion marker is always deleted first; if that delete fails,
        no payload delete is attempted. ``before_delete``, when supplied by a
        retention coordinator, must reassert or renew its remote lease. It is
        invoked immediately before the marker and before every payload object
        so a long multi-object delete cannot run on one stale lease check.
        """

        safe_id = self._base_backup_id(base_backup_id)
        prefix = f"{self.base_prefix(safe_id)}/"
        payload_keys: list[str] = []
        marker = self.base_manifest_key(safe_id)
        for key, _size, _modified in self._list_objects(prefix):
            owned = self._assert_owned_key(key)
            if not owned.startswith(prefix):
                raise WalS3PathError("S3 listed a base object outside the requested base backup")
            relative = owned[len(prefix) :]
            self._relative_path(relative)
            if owned != marker:
                payload_keys.append(owned)

        ordered = (marker, *sorted(set(payload_keys)))
        for key in ordered:
            if before_delete is not None:
                before_delete(key)
            self._delete_owned_key(key)
        return ordered

    @staticmethod
    def _prepare_destination(destination: Path) -> None:
        if destination.exists():
            raise WalS3PathError(f"download destination already exists: {destination}")
        if destination.is_symlink():
            raise WalS3PathError(f"download destination must not be a symbolic link: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.parent.is_symlink() or not destination.parent.is_dir():
            raise WalS3PathError(f"download parent is not a real directory: {destination.parent}")

    def _download_key(
        self,
        key: str,
        destination: Path,
        *,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
    ) -> WalObjectInfo:
        self._prepare_destination(destination)
        info = self._head(key)
        authoritative_digest = (
            expected_sha256.lower() if expected_sha256 is not None else info.sha256
        )
        if info.sha256 != authoritative_digest or (
            expected_size is not None and info.size != expected_size
        ):
            raise WalS3IntegrityError(
                f"S3 object {key!r} metadata does not match download expectations"
            )

        partial = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.partial")
        body = self._open_remote_body(key)
        digest = hashlib.sha256()
        size = 0
        try:
            with partial.open("xb") as handle:
                while True:
                    try:
                        chunk = body.read(1024 * 1024)
                    except Exception as exc:
                        raise self._provider_error(f"read {key}", exc) from None
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise WalS3ProviderError(f"S3 read {key} returned non-byte content")
                    size += len(chunk)
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if size != info.size or digest.hexdigest() != authoritative_digest:
                raise WalS3IntegrityError(f"downloaded S3 object {key!r} failed byte verification")
            os.replace(partial, destination)
            _fsync_directory(destination.parent)
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    partial.unlink(missing_ok=True)
                    raise self._provider_error(f"close {key}", exc) from None
            partial.unlink(missing_ok=True)
        return info

    def download_wal(
        self,
        filename: str,
        destination: str | Path,
    ) -> WalObjectInfo:
        return self._download_key(
            self.wal_key(filename),
            Path(destination),
        )

    def download_base_backup(
        self,
        base_backup_id: str,
        destination_root: str | Path,
    ) -> Path:
        safe_id = self._base_backup_id(base_backup_id)
        root = Path(destination_root)
        if root.exists() and (not root.is_dir() or root.is_symlink()):
            raise WalS3PathError(f"base download root is not a real directory: {root}")
        root.mkdir(parents=True, exist_ok=True)
        target = root / safe_id
        self._prepare_destination(target)
        marker, manifest, _objects = self._inspect_base(
            safe_id,
            verify_payload=False,
        )
        partial = root / f".{safe_id}.{uuid.uuid4().hex}.partial"
        partial.mkdir()
        try:
            for artifact in manifest.artifacts:
                self._download_key(
                    self.base_object_key(safe_id, artifact.path),
                    partial / Path(*PurePosixPath(artifact.path).parts),
                    expected_sha256=artifact.sha256,
                    expected_size=artifact.size,
                )
            self._download_key(
                marker.key,
                partial / BASE_COMPLETION_MARKER,
                expected_sha256=marker.sha256,
                expected_size=marker.size,
            )
            downloaded_manifest = self._parse_base_manifest(
                (partial / BASE_COMPLETION_MARKER).read_bytes(),
                safe_id,
            )
            if downloaded_manifest.artifacts != manifest.artifacts:
                raise WalS3IntegrityError("downloaded base manifest changed during retrieval")
            _fsync_tree(partial)
            os.replace(partial, target)
            _fsync_directory(root)
        except Exception:
            shutil.rmtree(partial, ignore_errors=True)
            raise
        return target

    def _lease_document(
        self,
        *,
        lease_id: str,
        owner: str,
        purpose: str,
        generation: int,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "generation": generation,
            "lease_id": lease_id,
            "owner": owner,
            "project": self.project,
            "purpose": purpose,
            "schema_version": 1,
            "system_identifier": self.system_identifier,
            "ttl_seconds": ttl_seconds,
            "type": "odooctl-pitr-coordination-lease",
        }

    @staticmethod
    def _strict_json_object(
        payload: bytes,
        *,
        label: str,
        error_type: type[WalS3Error],
    ) -> dict[str, Any]:
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise error_type(f"{label} is not valid UTF-8 JSON") from None
        if not isinstance(document, dict):
            raise error_type(f"{label} must be a JSON object")
        if payload != _canonical_json(document):
            raise error_type(f"{label} is not canonical JSON")
        return document

    def _lease_from_payload(
        self,
        head: _CoordinationHead,
        payload: bytes,
        *,
        allow_expired: bool,
    ) -> PitrCoordinationLease:
        info = head.info
        if (
            info is None
            or info.etag is None
            or not isinstance(
                info.last_modified,
                datetime,
            )
        ):
            raise WalS3LeaseError("PITR coordination lease disappeared")
        document = self._strict_json_object(
            payload,
            label="PITR coordination lease",
            error_type=WalS3LeaseError,
        )
        expected_fields = {
            "cluster_id",
            "generation",
            "lease_id",
            "owner",
            "project",
            "purpose",
            "schema_version",
            "system_identifier",
            "ttl_seconds",
            "type",
        }
        if set(document) != expected_fields:
            raise WalS3LeaseError("PITR coordination lease has an unexpected schema")
        if (
            document["schema_version"] != 1
            or document["type"] != "odooctl-pitr-coordination-lease"
            or document["project"] != self.project
            or document["cluster_id"] != self.cluster_id
            or str(document["system_identifier"]) != self.system_identifier
        ):
            raise WalS3LeaseError("PITR coordination lease does not match this archive scope")
        lease_id = document["lease_id"]
        if not isinstance(lease_id, str) or not _LEASE_ID_RE.fullmatch(lease_id):
            raise WalS3LeaseError("PITR coordination lease has an invalid lease_id")
        try:
            owner = self._printable_label(document["owner"], "lease owner")
            purpose = self._printable_label(
                document["purpose"],
                "lease purpose",
            )
        except WalS3ConfigurationError as exc:
            raise WalS3LeaseError(str(exc)) from None
        generation = document["generation"]
        ttl_seconds = document["ttl_seconds"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise WalS3LeaseError("PITR coordination lease has an invalid generation")
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not (
                MIN_COORDINATION_LEASE_TTL_SECONDS
                <= ttl_seconds
                <= MAX_COORDINATION_LEASE_TTL_SECONDS
            )
        ):
            raise WalS3LeaseError("PITR coordination lease has an invalid ttl_seconds")
        if payload != _canonical_json(
            self._lease_document(
                lease_id=lease_id,
                owner=owner,
                purpose=purpose,
                generation=generation,
                ttl_seconds=ttl_seconds,
            )
        ):
            raise WalS3LeaseError("PITR coordination lease has non-canonical field values")
        acquired_at = info.last_modified.astimezone(timezone.utc)
        expires_at = acquired_at + timedelta(seconds=ttl_seconds)
        lease = PitrCoordinationLease(
            key=info.key,
            lease_id=lease_id,
            owner=owner,
            purpose=purpose,
            generation=generation,
            ttl_seconds=ttl_seconds,
            etag=info.etag,
            acquired_at=acquired_at,
            expires_at=expires_at,
            observed_at=head.server_time,
        )
        if not allow_expired and lease.expired:
            raise WalS3LeaseExpiredError(
                "The PITR coordination lease is expired and cannot be stolen automatically"
            )
        return lease

    def inspect_coordination_lease(
        self,
        *,
        now: datetime | None = None,
    ) -> PitrCoordinationLease | None:
        """Inspect the exact lease, including an expired lease, without mutation."""

        initial = self._coordination_head(
            self.coordination_lease_key,
            now=now,
        )
        if initial.info is None:
            return None
        return self._read_coordination_lease(
            now=now,
            allow_expired=True,
        )

    def _read_coordination_lease(
        self,
        *,
        now: datetime | None,
        allow_expired: bool,
    ) -> PitrCoordinationLease:
        try:
            head, payload = self._read_stable_coordination_payload(
                self.coordination_lease_key,
                now=now,
                max_bytes=64 * 1024,
            )
        except _ObjectNotFound:
            raise WalS3LeaseError("PITR coordination lease is missing") from None
        except WalS3IntegrityError as exc:
            raise WalS3LeaseError("PITR coordination lease failed byte verification") from exc
        return self._lease_from_payload(
            head,
            payload,
            allow_expired=allow_expired,
        )

    @staticmethod
    def _same_lease(
        expected: PitrCoordinationLease,
        actual: PitrCoordinationLease,
    ) -> bool:
        return (
            expected.key,
            expected.lease_id,
            expected.owner,
            expected.purpose,
            expected.generation,
            expected.ttl_seconds,
            expected.etag,
            expected.acquired_at,
            expected.expires_at,
        ) == (
            actual.key,
            actual.lease_id,
            actual.owner,
            actual.purpose,
            actual.generation,
            actual.ttl_seconds,
            actual.etag,
            actual.acquired_at,
            actual.expires_at,
        )

    def assert_coordination_lease(
        self,
        lease: PitrCoordinationLease,
        *,
        now: datetime | None = None,
    ) -> PitrCoordinationLease:
        """Prove that ``lease`` is still the exact active remote lease."""

        current = self._read_coordination_lease(
            now=now,
            allow_expired=False,
        )
        if not self._same_lease(lease, current):
            raise WalS3LeaseError(
                "PITR coordination lease identity, generation, or issuance changed"
            )
        return current

    def acquire_coordination_lease(
        self,
        *,
        purpose: str,
        owner: str | None = None,
        ttl_seconds: int = DEFAULT_COORDINATION_LEASE_TTL_SECONDS,
        now: datetime | None = None,
    ) -> PitrCoordinationLease:
        """Acquire the scope-wide lease without ever stealing an expired one."""

        safe_purpose = self._printable_label(purpose, "lease purpose")
        safe_owner = self._printable_label(
            owner or f"odooctl-{uuid.uuid4().hex}",
            "lease owner",
        )
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not (
                MIN_COORDINATION_LEASE_TTL_SECONDS
                <= ttl_seconds
                <= MAX_COORDINATION_LEASE_TTL_SECONDS
            )
        ):
            raise ValueError(
                "ttl_seconds must be between "
                f"{MIN_COORDINATION_LEASE_TTL_SECONDS} and "
                f"{MAX_COORDINATION_LEASE_TTL_SECONDS}"
            )

        existing = self._coordination_head(
            self.coordination_lease_key,
            now=now,
        )
        if existing.info is not None:
            current = self._read_coordination_lease(
                now=now,
                allow_expired=True,
            )
            if current.expired:
                raise WalS3LeaseExpiredError(
                    "An expired PITR coordination lease blocks all mutations; "
                    "automatic lease stealing is unsafe"
                )
            raise WalS3LeaseBusyError(
                f"PITR coordination is already held by {current.owner!r} for {current.purpose!r}"
            )

        lease_id = uuid.uuid4().hex
        document = self._lease_document(
            lease_id=lease_id,
            owner=safe_owner,
            purpose=safe_purpose,
            generation=1,
            ttl_seconds=ttl_seconds,
        )
        payload = _canonical_json(document)
        digest = hashlib.sha256(payload).hexdigest()
        client = self._client()
        original_error: BaseException | None = None
        try:
            client.put_object(
                Bucket=self._bucket(),
                Key=self.coordination_lease_key,
                Body=io.BytesIO(payload),
                IfNoneMatch="*",
                Metadata={SHA256_METADATA_KEY: digest},
                **self._sse_args(),
            )
        except Exception as exc:
            original_error = exc

        try:
            head, remote_payload = self._read_stable_coordination_payload(
                self.coordination_lease_key,
                now=now,
                max_bytes=64 * 1024,
            )
        except _ObjectNotFound:
            if original_error is None:
                raise WalS3LeaseError("PITR coordination lease create was not durable") from None
            raise self._provider_error(
                f"create {self.coordination_lease_key}",
                original_error,
            ) from None

        if remote_payload == payload:
            return self._lease_from_payload(
                head,
                remote_payload,
                allow_expired=False,
            )

        current = self._lease_from_payload(
            head,
            remote_payload,
            allow_expired=True,
        )
        if current.expired:
            raise WalS3LeaseExpiredError(
                "An expired PITR coordination lease won the acquisition race; "
                "automatic lease stealing is unsafe"
            )
        if original_error is not None and not self._is_conditional_conflict(original_error):
            raise self._provider_error(
                f"create {self.coordination_lease_key}",
                original_error,
            ) from None
        raise WalS3LeaseBusyError("Another owner acquired PITR coordination first")

    def renew_coordination_lease(
        self,
        lease: PitrCoordinationLease,
        *,
        now: datetime | None = None,
    ) -> PitrCoordinationLease:
        """CAS-renew an active lease, returning its new generation and ETag."""

        current = self.assert_coordination_lease(lease, now=now)
        document = self._lease_document(
            lease_id=current.lease_id,
            owner=current.owner,
            purpose=current.purpose,
            generation=current.generation + 1,
            ttl_seconds=current.ttl_seconds,
        )
        payload = _canonical_json(document)
        digest = hashlib.sha256(payload).hexdigest()
        client = self._client()
        original_error: BaseException | None = None
        try:
            client.put_object(
                Bucket=self._bucket(),
                Key=current.key,
                Body=io.BytesIO(payload),
                IfMatch=self._if_match_etag(current.etag),
                Metadata={SHA256_METADATA_KEY: digest},
                **self._sse_args(),
            )
        except Exception as exc:
            original_error = exc

        try:
            head, remote_payload = self._read_stable_coordination_payload(
                current.key,
                now=now,
                max_bytes=64 * 1024,
            )
        except _ObjectNotFound:
            raise WalS3LeaseError("PITR coordination lease disappeared during renewal") from None
        if remote_payload == payload:
            return self._lease_from_payload(
                head,
                remote_payload,
                allow_expired=False,
            )
        if original_error is not None and not self._is_conditional_conflict(original_error):
            raise self._provider_error(
                f"renew {current.key}",
                original_error,
            ) from None
        raise WalS3LeaseError("PITR coordination lease CAS renewal lost ownership")

    def _conditional_delete(
        self,
        key: str,
        *,
        etag: str,
        now: datetime | None,
        label: str,
    ) -> None:
        if not etag:
            raise WalS3LeaseError(f"{label} has no ETag for conditional delete")
        client = self._client()
        original_error: BaseException | None = None
        try:
            client.delete_object(
                Bucket=self._bucket(),
                Key=self._assert_owned_key(key),
                IfMatch=self._if_match_etag(etag),
            )
        except Exception as exc:
            original_error = exc

        current = self._coordination_head(key, now=now)
        if current.info is None:
            return
        if current.info.etag != etag:
            raise WalS3LeaseError(f"{label} changed before its conditional delete completed")
        if original_error is None:
            raise WalS3ProviderError(f"S3 delete {key} reported success but the object remains")
        if self._is_conditional_conflict(original_error):
            raise WalS3LeaseError(f"{label} conditional delete lost its precondition") from None
        raise self._provider_error(f"delete {key}", original_error) from None

    def release_coordination_lease(
        self,
        lease: PitrCoordinationLease,
        *,
        now: datetime | None = None,
    ) -> None:
        """CAS-release this exact lease, including an expired lease we own."""

        current = self._read_coordination_lease(
            now=now,
            allow_expired=True,
        )
        if not self._same_lease(lease, current):
            raise WalS3LeaseError(
                "Refusing to release a PITR lease with different identity, generation, or issuance"
            )
        self._conditional_delete(
            current.key,
            etag=current.etag,
            now=now,
            label="PITR coordination lease",
        )

    def recover_expired_coordination_lease(
        self,
        *,
        confirm_lease_id: str,
        confirm_owner: str,
        confirm_purpose: str,
        confirm_owner_stopped: str,
        now: datetime | None = None,
    ) -> PitrCoordinationLease:
        """CAS-remove one explicitly confirmed expired crash lease.

        This is an administrative recovery primitive, not lease stealing: an
        active lease, a missing lease, clock ambiguity, or any confirmation
        mismatch leaves the remote object untouched.
        """

        current = self.inspect_coordination_lease(now=now)
        if current is None:
            raise WalS3LeaseError("There is no PITR coordination lease to recover")
        if not current.expired:
            raise WalS3LeaseBusyError("Refusing to recover an active PITR coordination lease")
        confirmations = (
            confirm_lease_id,
            confirm_owner,
            confirm_purpose,
        )
        expected = (
            current.lease_id,
            current.owner,
            current.purpose,
        )
        if confirmations != expected:
            raise WalS3LeaseError(
                "Expired PITR lease recovery confirmations do not exactly "
                "match lease_id, owner, and purpose"
            )
        expected_attestation = f"OWNER_STOPPED:{current.lease_id}"
        if confirm_owner_stopped != expected_attestation:
            raise WalS3LeaseError(
                "Expired PITR lease recovery requires the exact owner-stopped "
                f"attestation {expected_attestation!r}"
            )
        self._conditional_delete(
            current.key,
            etag=current.etag,
            now=now,
            label="Expired PITR coordination lease",
        )
        return current

    def _validated_pinned_wal(
        self,
        wal_segments: Iterable[PitrPinnedWal],
    ) -> tuple[PitrPinnedWal, ...]:
        validated: list[PitrPinnedWal] = []
        for item in wal_segments:
            if not isinstance(item, PitrPinnedWal):
                raise WalS3PinError("PITR pin WAL entries must be PitrPinnedWal values")
            filename = item.filename
            if not isinstance(filename, str) or not _WAL_SEGMENT_RE.fullmatch(filename):
                raise WalS3PinError("PITR pins may contain only full WAL segment filenames")
            digest = item.sha256
            if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
                raise WalS3PinError(f"PITR pin WAL {filename} has an invalid SHA-256")
            if isinstance(item.size, bool) or not isinstance(item.size, int) or item.size <= 0:
                raise WalS3PinError(f"PITR pin WAL {filename} has an invalid size")
            validated.append(
                PitrPinnedWal(
                    filename=filename,
                    sha256=digest,
                    size=item.size,
                )
            )
        if not validated:
            raise WalS3PinError("PITR pin must protect at least one exact WAL segment")
        filenames = [item.filename for item in validated]
        if filenames != sorted(set(filenames)):
            raise WalS3PinError("PITR pin WAL segments must be unique and strictly ordered")
        if len({filename[:8] for filename in filenames}) != 1:
            raise WalS3PinError("PITR pin WAL segments must belong to one timeline")
        return tuple(validated)

    def _pin_document(
        self,
        *,
        kind: Literal["plan", "restore"],
        pin_id: str,
        environment: str,
        base_backup_id: str,
        wal_segments: tuple[PitrPinnedWal, ...],
    ) -> dict[str, Any]:
        return {
            "base_backup_id": base_backup_id,
            "cluster_id": self.cluster_id,
            "environment": environment,
            "first_wal": wal_segments[0].filename,
            "kind": kind,
            "last_wal": wal_segments[-1].filename,
            "pin_id": pin_id,
            "project": self.project,
            "schema_version": 1,
            "system_identifier": self.system_identifier,
            "type": "odooctl-pitr-retention-pin",
            "wal_count": len(wal_segments),
            "wal_segments": [
                {
                    "filename": item.filename,
                    "sha256": item.sha256,
                    "size": item.size,
                }
                for item in wal_segments
            ],
        }

    def _pin_from_payload(
        self,
        head: _CoordinationHead,
        payload: bytes,
        *,
        expected_key: str,
    ) -> PitrRemotePin:
        info = head.info
        if info is None or info.etag is None or not isinstance(info.last_modified, datetime):
            raise WalS3PinError("Remote PITR pin disappeared")
        document = self._strict_json_object(
            payload,
            label="Remote PITR pin",
            error_type=WalS3PinError,
        )
        expected_fields = {
            "base_backup_id",
            "cluster_id",
            "environment",
            "first_wal",
            "kind",
            "last_wal",
            "pin_id",
            "project",
            "schema_version",
            "system_identifier",
            "type",
            "wal_count",
            "wal_segments",
        }
        if set(document) != expected_fields:
            raise WalS3PinError("Remote PITR pin has an unexpected schema")
        if (
            document["schema_version"] != 1
            or document["type"] != "odooctl-pitr-retention-pin"
            or document["project"] != self.project
            or document["cluster_id"] != self.cluster_id
            or str(document["system_identifier"]) != self.system_identifier
        ):
            raise WalS3PinError("Remote PITR pin does not match this archive scope")
        try:
            kind = self._pin_kind(document["kind"])
            pin_id = self._component(document["pin_id"], "pin_id")
            environment = self._component(
                document["environment"],
                "environment",
            )
            base_backup_id = self._base_backup_id(document["base_backup_id"])
        except (WalS3ConfigurationError, WalS3PathError) as exc:
            raise WalS3PinError(str(exc)) from None
        if self.pin_key(kind, pin_id) != expected_key:
            raise WalS3PinError("Remote PITR pin identity does not match its object key")
        raw_segments = document["wal_segments"]
        if not isinstance(raw_segments, list):
            raise WalS3PinError("Remote PITR pin wal_segments must be a list")
        segments: list[PitrPinnedWal] = []
        for raw in raw_segments:
            if not isinstance(raw, dict) or set(raw) != {
                "filename",
                "sha256",
                "size",
            }:
                raise WalS3PinError("Remote PITR pin contains an invalid WAL entry")
            segments.append(
                PitrPinnedWal(
                    filename=raw["filename"],
                    sha256=raw["sha256"],
                    size=raw["size"],
                )
            )
        validated = self._validated_pinned_wal(segments)
        if (
            isinstance(document["wal_count"], bool)
            or document["wal_count"] != len(validated)
            or document["first_wal"] != validated[0].filename
            or document["last_wal"] != validated[-1].filename
        ):
            raise WalS3PinError("Remote PITR pin WAL boundaries or count are inconsistent")
        if payload != _canonical_json(
            self._pin_document(
                kind=kind,
                pin_id=pin_id,
                environment=environment,
                base_backup_id=base_backup_id,
                wal_segments=validated,
            )
        ):
            raise WalS3PinError("Remote PITR pin has non-canonical field values")
        return PitrRemotePin(
            kind=kind,
            pin_id=pin_id,
            environment=environment,
            base_backup_id=base_backup_id,
            wal_segments=validated,
            key=info.key,
            etag=info.etag,
            sha256=info.sha256,
            last_modified=info.last_modified.astimezone(timezone.utc),
        )

    def _read_pitr_pin(
        self,
        key: str,
        *,
        now: datetime | None,
    ) -> PitrRemotePin:
        try:
            head, payload = self._read_stable_coordination_payload(
                key,
                now=now,
                max_bytes=MAX_PIN_BYTES,
            )
        except _ObjectNotFound:
            raise WalS3PinError(f"Remote PITR pin {key!r} is missing") from None
        except WalS3IntegrityError as exc:
            raise WalS3PinError(f"Remote PITR pin {key!r} failed byte verification") from exc
        return self._pin_from_payload(
            head,
            payload,
            expected_key=key,
        )

    def create_pitr_pin(
        self,
        *,
        kind: Literal["plan", "restore"],
        pin_id: str,
        environment: str,
        base_backup_id: str,
        wal_segments: Iterable[PitrPinnedWal],
        lease: PitrCoordinationLease,
        now: datetime | None = None,
    ) -> PitrRemotePin:
        """Create and byte-verify one immutable pin under the active lease."""

        self.assert_coordination_lease(lease, now=now)
        safe_kind = self._pin_kind(kind)
        safe_id = self._component(pin_id, "pin_id")
        safe_environment = self._component(environment, "environment")
        safe_base = self._base_backup_id(base_backup_id)
        safe_wal = self._validated_pinned_wal(wal_segments)
        document = self._pin_document(
            kind=safe_kind,
            pin_id=safe_id,
            environment=safe_environment,
            base_backup_id=safe_base,
            wal_segments=safe_wal,
        )
        payload = _canonical_json(document)
        if len(payload) > MAX_PIN_BYTES:
            raise WalS3PinError("Remote PITR pin exceeds the size limit")
        key = self.pin_key(safe_kind, safe_id)
        try:
            self._put_bytes_immutable(payload, key)
        except WalS3ConflictError as exc:
            raise WalS3PinError(
                f"Remote PITR pin {safe_id!r} conflicts with existing content"
            ) from exc
        return self._read_pitr_pin(key, now=now)

    def get_pitr_pin(
        self,
        kind: Literal["plan", "restore"],
        pin_id: str,
        *,
        lease: PitrCoordinationLease,
        now: datetime | None = None,
    ) -> PitrRemotePin:
        """Read one byte-verified pin while holding coordination."""

        self.assert_coordination_lease(lease, now=now)
        return self._read_pitr_pin(self.pin_key(kind, pin_id), now=now)

    def list_pitr_pins(
        self,
        *,
        lease: PitrCoordinationLease,
        environment: str | None = None,
        now: datetime | None = None,
    ) -> tuple[PitrRemotePin, ...]:
        """Validate every remote pin before returning the requested set."""

        self.assert_coordination_lease(lease, now=now)
        safe_environment = (
            self._component(environment, "environment") if environment is not None else None
        )
        prefix = f"{self.pins_prefix}/"
        pins: list[PitrRemotePin] = []
        for key, _size, _modified in self._list_objects(prefix):
            relative = key[len(prefix) :]
            parts = relative.split("/")
            if len(parts) != 2 or not parts[1].endswith(".json") or not parts[1][:-5]:
                raise WalS3PinError(f"Unexpected object in remote PITR pin namespace: {key!r}")
            try:
                expected = self.pin_key(parts[0], parts[1][:-5])
            except (WalS3ConfigurationError, WalS3PathError) as exc:
                raise WalS3PinError(
                    f"Invalid object in remote PITR pin namespace: {key!r}"
                ) from exc
            if expected != key:
                raise WalS3PinError(f"Remote PITR pin key is not canonical: {key!r}")
            pin = self._read_pitr_pin(key, now=now)
            if safe_environment is None or pin.environment == safe_environment:
                pins.append(pin)
        return tuple(
            sorted(
                pins,
                key=lambda pin: (pin.kind, pin.pin_id),
            )
        )

    def release_pitr_pin(
        self,
        pin: PitrRemotePin,
        *,
        lease: PitrCoordinationLease,
        now: datetime | None = None,
    ) -> None:
        """Conditionally release one exact pin under the active lease."""

        self.assert_coordination_lease(lease, now=now)
        expected_key = self.pin_key(pin.kind, pin.pin_id)
        if pin.key != expected_key:
            raise WalS3PinError("Refusing to release a PITR pin outside its canonical key")
        current = self._read_pitr_pin(expected_key, now=now)
        if current != pin:
            raise WalS3PinError("Refusing to release a PITR pin that changed remotely")
        self._conditional_delete(
            current.key,
            etag=current.etag,
            now=now,
            label="Remote PITR pin",
        )
