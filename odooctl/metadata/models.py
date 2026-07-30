from __future__ import annotations
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


_SAFE_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SYSTEM_IDENTIFIER_RE = re.compile(r"[1-9][0-9]{0,19}")
_WAL_SEGMENT_RE = re.compile(r"[0-9A-F]{24}")
_WAL_ARCHIVE_NAME_RE = re.compile(
    r"(?:[0-9A-F]{24}(?:\.[0-9A-F]{8}\.backup)?|[0-9A-F]{8}\.history)"
)
_LSN_RE = re.compile(r"[0-9A-F]{1,8}/[0-9A-F]{8}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_IMMUTABLE_IMAGE_RE = re.compile(
    r"(?:[^@\s]+@)?sha256:[0-9a-fA-F]{64}"
)


def _safe_component(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or ".." in value
        or not _SAFE_COMPONENT_RE.fullmatch(value)
    ):
        raise ValueError(f"{label} contains unsupported characters")
    return value


def _safe_label(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 256
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{label} must be non-empty and contain no control characters")
    return value


def _utc_timestamp(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{label} must be a valid ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _wal_segment_name(value: str, label: str) -> str:
    normalized = value.upper() if isinstance(value, str) else value
    if not isinstance(normalized, str) or not _WAL_SEGMENT_RE.fullmatch(normalized):
        raise ValueError(f"{label} must be a 24-character PostgreSQL WAL segment name")
    return normalized


def _wal_archive_name(value: str, label: str) -> str:
    normalized = value.upper() if isinstance(value, str) else value
    if not isinstance(normalized, str) or not _WAL_ARCHIVE_NAME_RE.fullmatch(normalized):
        raise ValueError(f"{label} is not a supported PostgreSQL WAL archive filename")
    return normalized


def _lsn(value: str, label: str) -> str:
    normalized = value.upper() if isinstance(value, str) else value
    if not isinstance(normalized, str) or not _LSN_RE.fullmatch(normalized):
        raise ValueError(f"{label} must be a PostgreSQL LSN")
    return normalized


def _sha256(value: str, label: str) -> str:
    normalized = value.lower() if isinstance(value, str) else value
    if not isinstance(normalized, str) or not _SHA256_RE.fullmatch(normalized):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return normalized


def _remote_uri(value: str, label: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "s3"
        or not parsed.hostname
        or not parsed.path
        or parsed.path == "/"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{label} must be a credential-free s3:// bucket/object URI")
    return value


def _artifact_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("artifact path must be a safe relative POSIX path")
    parts = PurePosixPath(value).parts
    if (
        any(part in {"", ".", ".."} for part in parts)
        or PurePosixPath(*parts).as_posix() != value
    ):
        raise ValueError("artifact path must be a canonical relative POSIX path")
    return value


class BackupManifest(BaseModel):
    schema_version: int = 1
    backup_id: str
    project: str
    environment: str
    timestamp: str = Field(default_factory=now_utc)
    db_name: str
    filestore_path: str | None = None
    artifact_paths: list[str] = Field(default_factory=list)
    db_dump: str = "db.dump"
    filestore: str = "filestore.tar"
    git_commit: str | None = None
    docker_image: str | None = None
    odoo_version: str
    backup_mode: str = "full"
    checksums: dict[str, str] = Field(default_factory=dict)
    encryption: dict[str, str] | None = None
    remote_uri: str | None = None
    remote_status: Literal[
        "disabled",
        "pending",
        "complete",
        "degraded",
        "failed",
    ] = "disabled"
    remote_verified_at: str | None = None
    remote_error: str | None = None
    status: str = "complete"


class DeploymentMetadata(BaseModel):
    project: str
    environment: str
    timestamp: str = Field(default_factory=now_utc)
    branch: str
    commit: str | None = None
    docker_image: str | None = None
    backup: str | None = None
    snapshot: str | None = None
    snapshot_status: str | None = None
    modules_updated: list[str] = Field(default_factory=list)
    status: str
    health_check_url: str | None = None
    message: str | None = None


class SanitizationMetadata(BaseModel):
    schema_version: int = 1
    project: str
    source_environment: str
    target_environment: str
    database: str
    timestamp: str = Field(default_factory=now_utc)
    policy: str
    native_status: str
    profile: str
    extension_statements: int
    custom_sql_files: int
    verification_checks: list[str] = Field(default_factory=list)
    verified: bool = True


class SnapshotResource(BaseModel):
    snapshot_resource_id: str | None = None
    source_resource_id: str
    kind: str
    state: str = "completed"
    location: str | None = None
    size_gib: int | None = None
    device_name: str | None = None
    root_device: bool = False
    volume_type: str | None = None
    iops: int | None = None
    throughput_mibps: int | None = None
    encrypted: bool | None = None
    kms_key_id: str | None = None


class SnapshotManifest(BaseModel):
    schema_version: int = 1
    snapshot_id: str
    project: str
    environment: str
    timestamp: str = Field(default_factory=now_utc)
    provider: Literal["aws_ebs", "hetzner_cloud"]
    source_resource_id: str
    resources: list[SnapshotResource] = Field(default_factory=list)
    scope: list[str] = Field(default_factory=list)
    consistency: Literal[
        "unknown",
        "crash_consistent",
        "application_consistent",
        "powered_off_consistent",
        "live_unverified",
    ] = "unknown"
    provider_scope: dict[str, str] = Field(default_factory=dict)
    provider_metadata: dict[
        str,
        str | int | float | bool | None,
    ] = Field(default_factory=dict)
    trigger: Literal["explicit", "pre_deploy"] = "explicit"
    portable_backup_id: str | None = None
    description: str | None = None
    recovery_notes: list[str] = Field(default_factory=list)
    status: Literal["requested", "pending", "complete", "failed"] = "complete"
    completed_at: str | None = None
    last_error: str | None = None


class SnapshotRestoreMetadata(BaseModel):
    schema_version: int = 1
    snapshot_id: str
    project: str
    environment: str
    provider: Literal["aws_ebs", "hetzner_cloud"]
    source_resource_id: str
    timestamp: str = Field(default_factory=now_utc)
    executed: bool
    restored_resource_ids: list[str] = Field(default_factory=list)
    status: Literal["planned", "pending", "complete", "failed"]
    message: str | None = None


class PitrBaseBackupManifest(BaseModel):
    """Completion metadata for one physical PostgreSQL base backup."""

    schema_version: Literal[1] = 1
    base_backup_id: str
    project: str
    environment: str
    cluster_id: str
    system_identifier: str
    postgres_major: int = Field(ge=10, le=99)
    postgres_image: str
    timeline: int = Field(ge=1, le=0xFFFFFFFF)
    wal_segment_size: int = Field(ge=1024 * 1024, le=1024 * 1024 * 1024)
    started_at: str
    completed_at: str | None = None
    start_lsn: str
    end_lsn: str
    start_wal: str
    end_wal: str
    artifact_paths: list[str] = Field(default_factory=list)
    checksums: dict[str, str] = Field(default_factory=dict)
    sizes: dict[str, int] = Field(default_factory=dict)
    remote_uri: str | None = None
    filestore_consistency: Literal["not_included"] = "not_included"
    status: Literal["pending", "complete", "failed"] = "pending"
    verified_at: str | None = None
    last_error: str | None = None

    @field_validator(
        "base_backup_id",
        "environment",
        "cluster_id",
    )
    @classmethod
    def components_must_be_safe(cls, value: str, info) -> str:
        return _safe_component(value, info.field_name)

    @field_validator("project")
    @classmethod
    def project_must_be_safe(cls, value: str, info) -> str:
        return _safe_label(value, info.field_name)

    @field_validator("system_identifier")
    @classmethod
    def system_identifier_must_be_decimal(cls, value: str, info) -> str:
        if not _SYSTEM_IDENTIFIER_RE.fullmatch(value):
            raise ValueError(f"{info.field_name} must be a decimal PostgreSQL system identifier")
        return value

    @field_validator("postgres_image")
    @classmethod
    def postgres_image_must_be_safe(cls, value: str, info) -> str:
        if (
            not value
            or len(value) > 512
            or value.strip() != value
            or any(character.isspace() or ord(character) < 32 for character in value)
            or not _IMMUTABLE_IMAGE_RE.fullmatch(value)
        ):
            raise ValueError(
                f"{info.field_name} must be an immutable sha256 image reference"
            )
        return value

    @field_validator("wal_segment_size")
    @classmethod
    def wal_segment_size_must_be_a_power_of_two(cls, value: int) -> int:
        if value & (value - 1):
            raise ValueError("wal_segment_size must be a power of two")
        return value

    @field_validator("started_at", "completed_at", "verified_at")
    @classmethod
    def timestamps_must_be_utc(cls, value: str | None, info) -> str | None:
        return None if value is None else _utc_timestamp(value, info.field_name)

    @field_validator("start_lsn", "end_lsn")
    @classmethod
    def lsns_must_be_safe(cls, value: str, info) -> str:
        return _lsn(value, info.field_name)

    @field_validator("start_wal", "end_wal")
    @classmethod
    def wal_boundaries_must_be_segments(cls, value: str, info) -> str:
        return _wal_segment_name(value, info.field_name)

    @field_validator("artifact_paths")
    @classmethod
    def artifact_paths_must_be_safe(cls, value: list[str]) -> list[str]:
        normalized = [_artifact_path(path) for path in value]
        if len(set(normalized)) != len(normalized):
            raise ValueError("artifact_paths must not contain duplicates")
        return normalized

    @field_validator("checksums")
    @classmethod
    def checksums_must_be_sha256(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            _artifact_path(path): _sha256(digest, f"checksums[{path!r}]")
            for path, digest in value.items()
        }

    @field_validator("sizes")
    @classmethod
    def sizes_must_be_nonnegative(cls, value: dict[str, int]) -> dict[str, int]:
        normalized: dict[str, int] = {}
        for path, size in value.items():
            safe_path = _artifact_path(path)
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise ValueError(f"sizes[{path!r}] must be a nonnegative integer")
            normalized[safe_path] = size
        return normalized

    @field_validator("remote_uri")
    @classmethod
    def remote_uri_must_be_safe(cls, value: str | None, info) -> str | None:
        return None if value is None else _remote_uri(value, info.field_name)

    @model_validator(mode="after")
    def completed_backups_must_be_recoverable(self) -> "PitrBaseBackupManifest":
        for value, label in (
            (self.start_wal, "start_wal"),
            (self.end_wal, "end_wal"),
        ):
            if int(value[:8], 16) != self.timeline:
                raise ValueError(f"{label} timeline does not match timeline")
        if self.completed_at is not None:
            started = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
            completed = datetime.fromisoformat(self.completed_at.replace("Z", "+00:00"))
            if completed < started:
                raise ValueError("completed_at must not be earlier than started_at")
        if self.status == "complete":
            if self.completed_at is None or self.verified_at is None or self.remote_uri is None:
                raise ValueError(
                    "complete PITR base backups require completed_at, verified_at, "
                    "and remote_uri"
                )
            artifacts = set(self.artifact_paths)
            if not artifacts:
                raise ValueError("complete PITR base backups require artifact_paths")
            if artifacts != set(self.checksums) or artifacts != set(self.sizes):
                raise ValueError(
                    "complete PITR base backup artifact_paths, checksums, and sizes "
                    "must identify the same artifacts"
                )
        return self


class WalReceipt(BaseModel):
    """Immutable receipt proving one WAL archive object was durably published."""

    schema_version: Literal[1] = 1
    project: str
    environment: str
    cluster_id: str
    system_identifier: str
    filename: str
    timeline: int = Field(ge=1, le=0xFFFFFFFF)
    sha256: str
    size: int = Field(gt=0)
    archived_at: str = Field(default_factory=now_utc)
    remote_uri: str
    status: Literal["complete"] = "complete"

    @field_validator("environment", "cluster_id")
    @classmethod
    def components_must_be_safe(cls, value: str, info) -> str:
        return _safe_component(value, info.field_name)

    @field_validator("project")
    @classmethod
    def project_must_be_safe(cls, value: str, info) -> str:
        return _safe_label(value, info.field_name)

    @field_validator("system_identifier")
    @classmethod
    def system_identifier_must_be_decimal(cls, value: str, info) -> str:
        if not _SYSTEM_IDENTIFIER_RE.fullmatch(value):
            raise ValueError(f"{info.field_name} must be a decimal PostgreSQL system identifier")
        return value

    @field_validator("filename")
    @classmethod
    def filename_must_be_a_wal_archive_name(cls, value: str, info) -> str:
        return _wal_archive_name(value, info.field_name)

    @field_validator("sha256")
    @classmethod
    def digest_must_be_sha256(cls, value: str, info) -> str:
        return _sha256(value, info.field_name)

    @field_validator("archived_at")
    @classmethod
    def archived_at_must_be_utc(cls, value: str, info) -> str:
        return _utc_timestamp(value, info.field_name)

    @field_validator("remote_uri")
    @classmethod
    def remote_uri_must_be_safe(cls, value: str, info) -> str:
        return _remote_uri(value, info.field_name)

    @model_validator(mode="after")
    def filename_timeline_must_match(self) -> "WalReceipt":
        filename_timeline = int(self.filename[:8], 16)
        if filename_timeline != self.timeline:
            raise ValueError("WAL receipt timeline does not match its filename")
        return self


class PitrRecoveryPlan(BaseModel):
    """Validated, non-destructive plan for one point-in-time recovery."""

    schema_version: Literal[1] = 1
    plan_id: str
    project: str
    environment: str
    cluster_id: str
    system_identifier: str
    base_backup_id: str
    database: str
    new_database: str
    target_time: str
    target_timeline: int = Field(ge=1, le=0xFFFFFFFF)
    first_wal: str
    last_wal: str
    wal_count: int = Field(ge=1)
    wal_bytes: int = Field(ge=1)
    recovery_image: str
    filestore_policy: Literal["database_only"] = "database_only"
    created_at: str = Field(default_factory=now_utc)
    status: Literal["planned", "executing", "verified", "failed"] = "planned"

    @field_validator(
        "plan_id",
        "environment",
        "cluster_id",
        "base_backup_id",
        "database",
        "new_database",
    )
    @classmethod
    def components_must_be_safe(cls, value: str, info) -> str:
        return _safe_component(value, info.field_name)

    @field_validator("project")
    @classmethod
    def project_must_be_safe(cls, value: str, info) -> str:
        return _safe_label(value, info.field_name)

    @field_validator("system_identifier")
    @classmethod
    def system_identifier_must_be_decimal(cls, value: str, info) -> str:
        if not _SYSTEM_IDENTIFIER_RE.fullmatch(value):
            raise ValueError(f"{info.field_name} must be a decimal PostgreSQL system identifier")
        return value

    @field_validator("target_time", "created_at")
    @classmethod
    def timestamps_must_be_utc(cls, value: str, info) -> str:
        return _utc_timestamp(value, info.field_name)

    @field_validator("first_wal", "last_wal")
    @classmethod
    def wal_boundaries_must_be_segments(cls, value: str, info) -> str:
        return _wal_segment_name(value, info.field_name)

    @field_validator("recovery_image")
    @classmethod
    def recovery_image_must_be_safe(cls, value: str, info) -> str:
        if (
            not value
            or len(value) > 512
            or value.strip() != value
            or any(character.isspace() or ord(character) < 32 for character in value)
            or not _IMMUTABLE_IMAGE_RE.fullmatch(value)
        ):
            raise ValueError(
                f"{info.field_name} must be an immutable sha256 image reference"
            )
        return value

    @model_validator(mode="after")
    def target_database_must_be_new(self) -> "PitrRecoveryPlan":
        if self.new_database == self.database:
            raise ValueError("new_database must differ from the live database")
        if int(self.last_wal[:8], 16) != self.target_timeline:
            raise ValueError("last_wal timeline does not match target_timeline")
        return self


class PitrRestoreMetadata(BaseModel):
    """Durable record of recovery verification and optional database cutover."""

    schema_version: Literal[1] = 1
    restore_id: str
    plan_id: str
    base_backup_id: str
    project: str
    environment: str
    cluster_id: str
    system_identifier: str
    database: str
    new_database: str
    target_time: str
    target_timeline: int = Field(ge=1, le=0xFFFFFFFF)
    timestamp: str = Field(default_factory=now_utc)
    recovered_at: str | None = None
    recovered_lsn: str | None = None
    verified: bool = False
    cutover: bool = False
    cutover_finalized: bool = False
    cutover_aside_database: str | None = None
    cutover_incoming_oid: int | None = Field(default=None, ge=1, le=0xFFFFFFFF)
    cutover_target_oid: int | None = Field(default=None, ge=1, le=0xFFFFFFFF)
    cutover_started_at: str | None = None
    cutover_completed_at: str | None = None
    filestore_consistency: Literal["not_included"] = "not_included"
    status: Literal["pending", "recovering", "verified", "cutover", "failed"] = "pending"
    last_error: str | None = None

    @field_validator(
        "restore_id",
        "plan_id",
        "base_backup_id",
        "environment",
        "cluster_id",
        "database",
        "new_database",
    )
    @classmethod
    def components_must_be_safe(cls, value: str, info) -> str:
        return _safe_component(value, info.field_name)

    @field_validator("cutover_aside_database")
    @classmethod
    def optional_cutover_database_must_be_safe(
        cls,
        value: str | None,
        info,
    ) -> str | None:
        return None if value is None else _safe_component(value, info.field_name)

    @field_validator("project")
    @classmethod
    def project_must_be_safe(cls, value: str, info) -> str:
        return _safe_label(value, info.field_name)

    @field_validator("system_identifier")
    @classmethod
    def system_identifier_must_be_decimal(cls, value: str, info) -> str:
        if not _SYSTEM_IDENTIFIER_RE.fullmatch(value):
            raise ValueError(f"{info.field_name} must be a decimal PostgreSQL system identifier")
        return value

    @field_validator(
        "target_time",
        "timestamp",
        "recovered_at",
        "cutover_started_at",
        "cutover_completed_at",
    )
    @classmethod
    def timestamps_must_be_utc(cls, value: str | None, info) -> str | None:
        return None if value is None else _utc_timestamp(value, info.field_name)

    @field_validator("recovered_lsn")
    @classmethod
    def recovered_lsn_must_be_safe(cls, value: str | None, info) -> str | None:
        return None if value is None else _lsn(value, info.field_name)

    @model_validator(mode="after")
    def lifecycle_flags_must_match_status(self) -> "PitrRestoreMetadata":
        if self.new_database == self.database:
            raise ValueError("new_database must differ from the live database")
        has_cutover_plan = self.cutover_aside_database is not None
        if has_cutover_plan != (self.cutover_incoming_oid is not None):
            raise ValueError(
                "cutover_aside_database and cutover_incoming_oid must be recorded together"
            )
        if has_cutover_plan != (self.cutover_started_at is not None):
            raise ValueError(
                "cutover plan identity and cutover_started_at must be recorded together"
            )
        if self.status in {"verified", "cutover"} and not self.verified:
            raise ValueError(f"{self.status} PITR restore metadata must be verified")
        if self.status == "cutover" and not self.cutover:
            raise ValueError("cutover PITR restore metadata must set cutover=true")
        if self.cutover and self.status != "cutover":
            raise ValueError("cutover=true requires status='cutover'")
        if self.cutover and not has_cutover_plan:
            raise ValueError("cutover=true requires durable cutover plan identity")
        if self.cutover != (self.cutover_completed_at is not None):
            raise ValueError(
                "cutover and cutover_completed_at must be recorded together"
            )
        if self.cutover_finalized and not self.cutover:
            raise ValueError("cutover_finalized=true requires cutover=true")
        return self
