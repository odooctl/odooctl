"""Structured result models returned by service functions."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, Literal, TypeVar

T = TypeVar("T")


@dataclass
class ServiceResult(Generic[T]):
    ok: bool
    value: T | None = None
    error: str | None = None

    @classmethod
    def success(cls, value: T) -> "ServiceResult[T]":
        return cls(ok=True, value=value)

    @classmethod
    def failure(cls, error: str) -> "ServiceResult[T]":
        return cls(ok=False, error=error)


@dataclass
class EnvironmentSummary:
    name: str
    url: str
    branch: str
    commit: str
    image: str
    odoo_status: str
    postgres_status: str
    latest_backup: str
    last_deployment: str
    last_deployment_backup: str
    last_deployment_message: str | None
    health_check: str
    health_check_url: str


@dataclass
class StatusReport:
    project: str
    git_commit: str
    environments: list[EnvironmentSummary] = field(default_factory=list)
    raw_compose_output: str = ""


@dataclass
class DoctorReport:
    project: str
    root: str
    config_path: str
    ok: bool
    checks: list


@dataclass
class BackupResult:
    backup_id: str


@dataclass
class RestoreResult:
    backup_id: str


@dataclass
class CloneResult:
    url: str
    native_neutralization: str | None = None


@dataclass
class DeployResult:
    environment: str
    status: str
    backup_id: str | None = None


@dataclass
class ProjectSummary:
    name: str
    root: str
    config_path: str
    odoo_version: str
    git_commit: str | None


@dataclass
class BranchStatus:
    environment: str
    tier: str | None
    branch: str
    current_commit: str | None
    last_deployed_commit: str | None
    ahead: int | None
    behind: int | None
    drift: Literal["clean", "ahead", "behind", "diverged", "unknown"]


@dataclass
class PromoteResult:
    source: str
    target: str
    status: str  # "success" | "failed" | "preview"
    backup_id: str | None = None
    rolled_back: bool = False
    message: str | None = None
