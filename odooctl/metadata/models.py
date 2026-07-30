from __future__ import annotations
from datetime import datetime, timezone
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
