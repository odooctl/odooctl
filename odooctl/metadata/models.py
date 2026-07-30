from __future__ import annotations
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
