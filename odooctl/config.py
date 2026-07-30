from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
import click
from pydantic import (
    AliasChoices,
    BaseModel,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

# Defense-in-depth input validation (audit findings C3/F8). These values flow
# into subprocess argv, container paths, docker volume names, and Traefik YAML,
# so they are constrained at the config boundary even though shell sinks were
# already removed.
IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
IDENTIFIER_MAX_LENGTH = 64
_IDENTIFIER_RE = re.compile(IDENTIFIER_PATTERN)

HOSTNAME_PATTERN = r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)*$"
HOSTNAME_MAX_LENGTH = 253
_HOSTNAME_RE = re.compile(HOSTNAME_PATTERN)


def _redacted(value: object) -> str:
    return str(value)[:32]


def validate_identifier(value: str, field_name: str) -> str:
    """Validate a docker/compose/database identifier; return it unchanged.

    Raises ValueError when *value* is not a safe identifier (alphanumeric
    start, then alphanumerics/dots/underscores/hyphens, no '..', max 64 chars).
    """
    if (
        not isinstance(value, str)
        or len(value) > IDENTIFIER_MAX_LENGTH
        or ".." in value
        or not _IDENTIFIER_RE.fullmatch(value)
    ):
        raise ValueError(
            f"{field_name} {_redacted(value)!r} is invalid: must match {IDENTIFIER_PATTERN} "
            f"with no '..', max {IDENTIFIER_MAX_LENGTH} characters"
        )
    return value


def validate_hostname(value: str, field_name: str) -> str:
    """Validate a DNS hostname; return it normalized to lowercase.

    Raises ValueError when *value* is not a valid DNS hostname (labels of
    alphanumerics and hyphens, dot-separated, max 253 chars; no wildcards).
    """
    normalized = value.lower() if isinstance(value, str) else value
    if (
        not isinstance(normalized, str)
        or len(normalized) > HOSTNAME_MAX_LENGTH
        or not _HOSTNAME_RE.fullmatch(normalized)
    ):
        raise ValueError(
            f"{field_name} {_redacted(value)!r} is invalid: must be a valid DNS hostname "
            f"matching {HOSTNAME_PATTERN}, max {HOSTNAME_MAX_LENGTH} characters"
        )
    return normalized


class ProjectConfig(BaseModel):
    name: str = "my-odoo-project"
    odoo_version: str = "19.0"


class RuntimeConfig(BaseModel):
    type: Literal["docker_compose"] = "docker_compose"
    compose_file: str = "docker-compose.yml"
    reverse_proxy: str = "traefik"
    execution_mode: Literal["docker", "host"] = "host"


class EnvironmentConfig(BaseModel):
    stack: str = "default"
    tier: Literal["production", "staging", "development", "qa"] | None = None
    protected: bool | None = None
    branch: str
    scheme: Literal["http", "https"] = "https"
    domain: str
    port: int | None = None
    db_name: str
    filestore_path: str
    filestore_volume: str | None = None
    db_selector: bool = False
    clone_from: str | None = None
    sanitize: bool = False
    update_modules: list[str] = Field(default_factory=list)
    promotes_to: str | None = None
    auto_deploy: bool = False
    last_deployed_commit: str | None = None

    @field_validator("db_name", "filestore_volume")
    @classmethod
    def identifier_fields_must_be_safe(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return value
        return validate_identifier(value, info.field_name)

    @field_validator("domain")
    @classmethod
    def domain_must_be_valid_hostname(cls, value: str, info: ValidationInfo) -> str:
        return validate_hostname(value, info.field_name)

    @field_validator("filestore_path")
    @classmethod
    def filestore_path_must_be_safe(cls, value: str, info: ValidationInfo) -> str:
        # ``filestore_path`` reaches ``rm -rf``/``shutil.rmtree``/``cp -a`` and,
        # for the Docker backend, is reduced to its basename. A value with an
        # empty basename (``/``, trailing slash only) or ``..`` components can
        # target the filestore root or escape it. Accept both relative
        # (``filestore/odoo_prod``) and absolute (``/var/lib/odoo/...``) paths.
        from pathlib import PurePosixPath

        display = str(value)[:64]
        if not value or not value.strip():
            raise ValueError(f"{info.field_name} must not be empty")
        parts = PurePosixPath(value).parts
        if ".." in parts:
            raise ValueError(
                f"{info.field_name} {display!r} must not contain '..' path segments"
            )
        if not PurePosixPath(value).name:
            raise ValueError(
                f"{info.field_name} {display!r} must reference a named directory, not a root path"
            )
        return value


class PostgresConfig(BaseModel):
    host: str = "localhost"
    port: int = 5432
    user: str = "odoo"
    password_env: str = "ODOO_DB_PASSWORD"
    service: str = "postgres"
    internal_host: str | None = None
    service_user: str | None = None
    service_password_env: str | None = None

    @field_validator("service")
    @classmethod
    def service_must_be_safe(cls, value: str, info: ValidationInfo) -> str:
        return validate_identifier(value, info.field_name)

    @model_validator(mode="after")
    def fill_container_defaults(self) -> "PostgresConfig":
        if self.internal_host is None:
            self.internal_host = self.service
        if self.service_user is None:
            self.service_user = self.user
        if self.service_password_env is None:
            self.service_password_env = self.password_env
        return self

    def password(self) -> str:
        value = os.getenv(self.password_env)
        if not value:
            raise RuntimeError(f"Missing required environment variable: {self.password_env}")
        return value

    def service_password(self) -> str:
        env_name = self.service_password_env or self.password_env
        value = os.getenv(env_name)
        if not value:
            raise RuntimeError(f"Missing required environment variable: {env_name}")
        return value


class OdooConfig(BaseModel):
    image: str
    cli_command: str = "odoo"
    config_path: str = "/etc/odoo/odoo.conf"
    addons_paths: list[str] = Field(default_factory=list)
    service: str = "odoo"
    db_host: str | None = None
    db_user: str | None = None
    db_password_env: str | None = None
    filestore_container_path: str = "/var/lib/odoo"
    without_demo: str = "True"

    @field_validator("service")
    @classmethod
    def service_must_be_safe(cls, value: str, info: ValidationInfo) -> str:
        return validate_identifier(value, info.field_name)

    @field_validator("cli_command")
    @classmethod
    def cli_command_must_be_nonempty(cls, value: str, info: ValidationInfo) -> str:
        if not value or not value.strip() or any(ch.isspace() for ch in value):
            raise ValueError(f"{info.field_name} must be one executable path without whitespace")
        return value


class RemoteBackupConfig(BaseModel):
    type: Literal["s3"] = "s3"
    bucket: str | None = None
    region: str | None = None
    prefix: str = ""
    endpoint_env: str | None = None
    access_key_env: str | None = None
    secret_key_env: str | None = None
    region_env: str | None = None
    encryption_algorithm: str | None = None
    encryption_key_env: str | None = None
    policy: Literal["required", "best_effort", "disabled"] = "best_effort"
    verify_after_upload: bool = True
    orphan_grace_hours: int = Field(default=24, ge=1)

    @field_validator("bucket")
    @classmethod
    def bucket_must_be_safe(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        if (
            not normalized
            or normalized in {".", ".."}
            or "/" in normalized
            or "\\" in normalized
            or any(ord(ch) < 32 for ch in normalized)
        ):
            raise ValueError(f"{info.field_name} must be a non-empty S3 bucket name without path separators")
        return normalized

    @field_validator("prefix")
    @classmethod
    def prefix_must_be_safe(cls, value: str, info: ValidationInfo) -> str:
        from pathlib import PurePosixPath

        normalized = value.strip("/")
        if "\\" in value or any(ord(ch) < 32 for ch in value):
            raise ValueError(f"{info.field_name} must be a safe POSIX object-key prefix")
        if normalized:
            parts = PurePosixPath(normalized).parts
            if value.startswith("/") or any(part in {".", ".."} for part in parts):
                raise ValueError(f"{info.field_name} must be relative and must not contain '.' or '..' segments")
        return normalized

    @model_validator(mode="after")
    def active_remote_requires_bucket(self) -> "RemoteBackupConfig":
        if self.policy != "disabled" and not self.bucket:
            raise ValueError("remote backup bucket is required unless policy is disabled")
        if bool(self.access_key_env) != bool(self.secret_key_env):
            raise ValueError(
                "remote backup access_key_env and secret_key_env must be configured together"
            )
        if self.encryption_key_env and not self.encryption_algorithm:
            raise ValueError(
                "remote backup encryption_key_env requires encryption_algorithm"
            )
        return self


class RetentionConfig(BaseModel):
    daily: int = Field(default=7, ge=0)
    weekly: int = Field(default=4, ge=0)
    monthly: int = Field(default=6, ge=0)
    grace_hours: int = Field(default=1, ge=1)


class BackupsConfig(BaseModel):
    local_path: str = "./backups"
    remote: RemoteBackupConfig | None = None
    retention: RetentionConfig = Field(default_factory=RetentionConfig)


class WalArchiveS3Config(BaseModel):
    """Independent S3-compatible destination for PostgreSQL WAL archives."""

    type: Literal["s3"] = "s3"
    bucket: str
    region: str | None = None
    prefix: str = ""
    endpoint_env: str | None = None
    access_key_env: str | None = None
    secret_key_env: str | None = None
    session_token_env: str | None = None
    region_env: str | None = None
    encryption_algorithm: Literal["AES256", "aws:kms"] | None = None
    encryption_key_env: str | None = None

    @field_validator("bucket")
    @classmethod
    def bucket_must_be_safe(cls, value: str, info: ValidationInfo) -> str:
        normalized = value.strip()
        if (
            not normalized
            or normalized in {".", ".."}
            or "/" in normalized
            or "\\" in normalized
            or any(ord(ch) < 32 for ch in normalized)
        ):
            raise ValueError(
                f"{info.field_name} must be a non-empty S3 bucket name "
                "without path separators"
            )
        return normalized

    @field_validator("prefix")
    @classmethod
    def prefix_must_be_safe(cls, value: str, info: ValidationInfo) -> str:
        from pathlib import PurePosixPath

        normalized = value.strip("/")
        if "\\" in value or any(ord(ch) < 32 for ch in value):
            raise ValueError(f"{info.field_name} must be a safe POSIX object-key prefix")
        if normalized:
            parts = PurePosixPath(normalized).parts
            if value.startswith("/") or any(part in {".", ".."} for part in parts):
                raise ValueError(
                    f"{info.field_name} must be relative and must not contain "
                    "'.' or '..' segments"
                )
        return normalized

    @field_validator(
        "endpoint_env",
        "access_key_env",
        "secret_key_env",
        "session_token_env",
        "region_env",
        "encryption_key_env",
    )
    @classmethod
    def env_references_must_be_names(
        cls,
        value: str | None,
        info: ValidationInfo,
    ) -> str | None:
        if value is None:
            return value
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError(f"{info.field_name} must be an environment variable name")
        return value

    @model_validator(mode="after")
    def credentials_and_encryption_must_be_complete(self) -> "WalArchiveS3Config":
        if bool(self.access_key_env) != bool(self.secret_key_env):
            raise ValueError(
                "WAL archive access_key_env and secret_key_env must be configured together"
            )
        if self.session_token_env and not self.access_key_env:
            raise ValueError(
                "WAL archive session_token_env requires access_key_env and secret_key_env"
            )
        if self.encryption_key_env and self.encryption_algorithm != "aws:kms":
            raise ValueError(
                "WAL archive encryption_key_env requires encryption_algorithm 'aws:kms'"
            )
        return self


class PitrRetentionConfig(BaseModel):
    """Retention floor for recoverable physical backups and their WAL graph."""

    base_backups: int = Field(default=2, ge=0)
    grace_hours: int = Field(default=24, ge=0)


class PitrConfig(BaseModel):
    """PostgreSQL physical backup/PITR settings, disabled unless opted in."""

    enabled: bool = False
    environment: str = "production"
    cluster_id: str | None = None
    system_identifier: str | None = None
    destination: WalArchiveS3Config | None = None
    replication_user: str | None = None
    replication_password_env: str | None = None
    recovery_image: str | None = None
    filestore_policy: Literal["database_only"] | None = None
    retention: PitrRetentionConfig = Field(default_factory=PitrRetentionConfig)

    @field_validator("environment", "cluster_id", "replication_user")
    @classmethod
    def identifiers_must_be_safe(
        cls,
        value: str | None,
        info: ValidationInfo,
    ) -> str | None:
        if value is None:
            return value
        return validate_identifier(value, f"pitr.{info.field_name}")

    @field_validator("system_identifier")
    @classmethod
    def system_identifier_must_be_decimal(
        cls,
        value: str | None,
        info: ValidationInfo,
    ) -> str | None:
        if value is not None and not re.fullmatch(r"[1-9][0-9]{0,19}", value):
            raise ValueError(
                f"{info.field_name} must be a decimal PostgreSQL system identifier"
            )
        return value

    @field_validator("replication_password_env")
    @classmethod
    def replication_password_must_be_an_env_name(
        cls,
        value: str | None,
        info: ValidationInfo,
    ) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError(f"{info.field_name} must be an environment variable name")
        return value

    @field_validator("recovery_image")
    @classmethod
    def recovery_image_must_be_safe(
        cls,
        value: str | None,
        info: ValidationInfo,
    ) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        if (
            not normalized
            or len(normalized) > 512
            or any(ch.isspace() or ord(ch) < 32 for ch in normalized)
            or not re.fullmatch(
                r"(?:[^@\s]+@)?sha256:[0-9a-fA-F]{64}",
                normalized,
            )
        ):
            raise ValueError(
                f"{info.field_name} must be an immutable sha256 image reference"
            )
        return normalized

    @model_validator(mode="after")
    def enabled_pitr_must_be_explicit(self) -> "PitrConfig":
        if not self.enabled:
            return self
        if not self.cluster_id:
            raise ValueError("pitr.cluster_id is required when PITR is enabled")
        if self.destination is None:
            raise ValueError("pitr.destination is required when PITR is enabled")
        if self.system_identifier is None:
            raise ValueError(
                "pitr.system_identifier is required when PITR is enabled "
                "so a fresh recovery host can locate the archive"
            )
        if self.recovery_image is None:
            raise ValueError(
                "pitr.recovery_image is required when PITR is enabled"
            )
        if self.filestore_policy != "database_only":
            raise ValueError(
                "pitr.filestore_policy must explicitly be 'database_only' "
                "when PITR is enabled"
            )
        if self.retention.base_backups < 1:
            raise ValueError(
                "pitr.retention.base_backups must be at least 1 when PITR is enabled"
            )
        return self


def _validate_executable(value: str, field_name: str) -> str:
    if not value or not value.strip() or any(ch.isspace() for ch in value):
        raise ValueError(f"{field_name} must be one executable path without whitespace")
    return value


class AwsEbsSnapshotConfig(BaseModel):
    instance_id: str
    region: str
    recovery_availability_zone: str = Field(
        validation_alias=AliasChoices(
            "recovery_availability_zone",
            "availability_zone",
        )
    )
    profile: str | None = None
    include_root_volume: bool = True
    completion_timeout_seconds: int = Field(default=600, ge=0, le=86400)
    poll_interval_seconds: float = Field(default=15.0, gt=0, le=300)
    cli_command: str = "aws"

    @field_validator("instance_id")
    @classmethod
    def instance_id_must_be_safe(cls, value: str, info: ValidationInfo) -> str:
        if not re.fullmatch(r"i-[0-9a-fA-F]{8,32}", value):
            raise ValueError(f"{info.field_name} must be an EC2 instance id")
        return value

    @field_validator("region", "recovery_availability_zone", "profile")
    @classmethod
    def cloud_identifiers_must_be_safe(
        cls,
        value: str | None,
        info: ValidationInfo,
    ) -> str | None:
        if value is None:
            return value
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value):
            raise ValueError(f"{info.field_name} contains unsupported characters")
        return value

    @field_validator("cli_command")
    @classmethod
    def cli_command_must_be_nonempty(cls, value: str, info: ValidationInfo) -> str:
        return _validate_executable(value, info.field_name)

    @model_validator(mode="after")
    def recovery_zone_must_belong_to_region(self) -> "AwsEbsSnapshotConfig":
        suffix = self.recovery_availability_zone[len(self.region) :]
        if (
            not self.recovery_availability_zone.startswith(self.region)
            or not suffix
            or not (suffix[0].isalpha() or suffix[0] == "-")
        ):
            raise ValueError(
                "recovery_availability_zone must belong to the configured AWS region"
            )
        return self


class HetznerSnapshotConfig(BaseModel):
    server: str
    recovery_server_type: str
    recovery_location: str
    recovery_network: str
    context: str | None = None
    token_env: str = "HCLOUD_TOKEN"
    completion_timeout_seconds: int = Field(default=600, ge=0, le=86400)
    poll_interval_seconds: float = Field(default=10.0, gt=0, le=300)
    cli_command: str = "hcloud"

    @field_validator(
        "server",
        "recovery_server_type",
        "recovery_location",
        "recovery_network",
        "context",
    )
    @classmethod
    def resource_identifiers_must_be_safe(
        cls,
        value: str | None,
        info: ValidationInfo,
    ) -> str | None:
        if value is None:
            return value
        return validate_identifier(value, info.field_name)

    @field_validator("token_env")
    @classmethod
    def token_env_must_be_safe(cls, value: str, info: ValidationInfo) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError(f"{info.field_name} must be an environment variable name")
        return value

    @field_validator("cli_command")
    @classmethod
    def cli_command_must_be_nonempty(cls, value: str, info: ValidationInfo) -> str:
        return _validate_executable(value, info.field_name)


class SnapshotsConfig(BaseModel):
    provider: Literal["none", "aws_ebs", "hetzner_cloud"] = "none"
    environment: str = "production"
    pre_deploy: Literal["disabled", "preferred", "required"] = "disabled"
    aws_ebs: AwsEbsSnapshotConfig | None = None
    hetzner_cloud: HetznerSnapshotConfig | None = None

    @field_validator("environment")
    @classmethod
    def environment_must_be_safe(cls, value: str, info: ValidationInfo) -> str:
        return validate_identifier(value, info.field_name)

    @model_validator(mode="after")
    def provider_settings_must_match(self) -> "SnapshotsConfig":
        if self.provider == "none":
            if self.pre_deploy != "disabled":
                raise ValueError(
                    "snapshots.pre_deploy must be disabled when snapshots.provider is none"
                )
            return self
        if self.provider == "aws_ebs" and self.aws_ebs is None:
            raise ValueError("snapshots.aws_ebs is required for the aws_ebs provider")
        if self.provider == "hetzner_cloud" and self.hetzner_cloud is None:
            raise ValueError(
                "snapshots.hetzner_cloud is required for the hetzner_cloud provider"
            )
        return self


class SanitizationConfig(BaseModel):
    native_neutralize: Literal["required", "preferred", "disabled"] = "preferred"
    sql_files: list[str] = Field(default_factory=list)
    disable_mail_servers: bool = True
    disable_fetchmail: bool = True
    disable_crons: bool = True
    rewrite_base_url: bool = True
    disable_payment_providers: bool = True
    disable_queue_jobs: bool = True
    purge_mail_queue: bool = True
    temp_db_suffix: str = "_incoming"

    @field_validator("temp_db_suffix")
    @classmethod
    def temp_db_suffix_must_be_safe(cls, value: str, info: ValidationInfo) -> str:
        if not value:
            raise ValueError(f"{info.field_name} must not be empty")
        validate_identifier(f"x{value}", info.field_name)
        return value


class RedactionConfig(BaseModel):
    min_secret_length: int = 6
    ignore_values: list[str] = Field(default_factory=lambda: ["odoo", "admin", "postgres", "password", "secret", "changeme"])


class HealthcheckConfig(BaseModel):
    path: str = "/web/health"
    scheme: Literal["http", "https"] | None = None
    timeout_seconds: int = 5
    retries: int = 12
    interval_seconds: int = 5


class OdooCtlConfig(BaseModel):
    project: ProjectConfig
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    environments: dict[str, EnvironmentConfig]
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    odoo: OdooConfig
    backups: BackupsConfig = Field(default_factory=BackupsConfig)
    pitr: PitrConfig = Field(default_factory=PitrConfig)
    snapshots: SnapshotsConfig = Field(default_factory=SnapshotsConfig)
    sanitization: SanitizationConfig = Field(default_factory=SanitizationConfig)
    healthcheck: HealthcheckConfig = Field(default_factory=HealthcheckConfig)
    redaction: RedactionConfig = Field(default_factory=RedactionConfig)

    @field_validator("environments")
    @classmethod
    def must_have_environments(cls, value: dict[str, EnvironmentConfig]) -> dict[str, EnvironmentConfig]:
        if not value:
            raise ValueError("at least one environment is required")
        for name in value:
            validate_identifier(name, "environment name")
        return value

    @model_validator(mode="after")
    def validate_environment_graph(self) -> "OdooCtlConfig":
        if self.odoo.db_host is None:
            self.odoo.db_host = self.postgres.internal_host
        if self.odoo.db_user is None:
            self.odoo.db_user = self.postgres.user
        if self.odoo.db_password_env is None:
            self.odoo.db_password_env = self.postgres.password_env

        seen_db_names: dict[str, str] = {}
        seen_filestores: dict[str, str] = {}
        seen_domains: dict[str, str] = {}
        seen_branches: dict[str, str] = {}

        if self.pitr.enabled and self.pitr.environment not in self.environments:
            known = ", ".join(sorted(self.environments))
            raise ValueError(
                f"pitr.environment {self.pitr.environment!r} is not defined. "
                f"Known: {known}"
            )

        if (
            self.snapshots.provider != "none"
            and self.snapshots.environment not in self.environments
        ):
            known = ", ".join(sorted(self.environments))
            raise ValueError(
                f"snapshots.environment {self.snapshots.environment!r} is not "
                f"defined. Known: {known}"
            )
        if (
            self.snapshots.provider != "none"
            and self.snapshots.pre_deploy != "disabled"
            and not self.is_protected(self.snapshots.environment)
        ):
            raise ValueError(
                "snapshots.pre_deploy requires snapshots.environment to be a "
                "protected environment"
            )

        for name, env in self.environments.items():
            if name == "production" and env.clone_from:
                raise ValueError(
                    "Environment 'production' cannot be a clone target; "
                    "cloning drops and recreates the target database without a backup"
                )
            if env.clone_from and env.clone_from not in self.environments:
                known = ", ".join(sorted(self.environments))
                raise ValueError(f"Environment '{name}' clone_from '{env.clone_from}' is not defined. Known: {known}")
            if env.clone_from == name:
                raise ValueError(f"Environment '{name}' cannot clone_from itself")

            if env.db_name in seen_db_names:
                first_env = seen_db_names[env.db_name]
                raise ValueError(
                    f"Environments '{first_env}' and '{name}' cannot share db_name '{env.db_name}'; "
                    "clone and rollback operations drop and recreate target databases"
                )
            seen_db_names[env.db_name] = name

            filestore_identity = (
                f"volume:{env.filestore_volume}:{env.filestore_path}"
                if env.filestore_volume
                else f"path:{env.filestore_path}"
            )
            if filestore_identity in seen_filestores:
                first_env = seen_filestores[filestore_identity]
                raise ValueError(
                    f"Environments '{first_env}' and '{name}' cannot share filestore '{filestore_identity}'; "
                    "clone and rollback operations replace target filestores"
                )
            seen_filestores[filestore_identity] = name

            if env.domain in seen_domains:
                first_env = seen_domains[env.domain]
                first = self.environments[first_env]
                shared_multidb_stack = first.stack == env.stack and first.db_selector and env.db_selector
                if not shared_multidb_stack:
                    raise ValueError(
                        f"Environments '{first_env}' and '{name}' cannot share domain '{env.domain}'; "
                        "deploy and rollback healthchecks would target the wrong instance unless both use db_selector in the same stack"
                    )
            seen_domains[env.domain] = name

            if env.branch in seen_branches:
                first_env = seen_branches[env.branch]
                raise ValueError(
                    f"Environments '{first_env}' and '{name}' cannot share branch '{env.branch}'; "
                    "branch-to-environment mapping must be unique for deploy and rollback to target the right instance"
                )
            seen_branches[env.branch] = name

            if env.promotes_to and env.promotes_to not in self.environments:
                known = ", ".join(sorted(self.environments))
                raise ValueError(
                    f"Environment '{name}' promotes_to '{env.promotes_to}' is not defined. Known: {known}"
                )
            if env.promotes_to == name:
                raise ValueError(f"Environment '{name}' cannot promotes_to itself")

        # A clone/restore/rehearsal restores into ``<db_name><temp_db_suffix>``
        # and then drops/renames it. If that temp name equals another env's live
        # db_name, promoting one environment would silently DROP another's
        # database. Reject the collision at load time.
        suffix = self.sanitization.temp_db_suffix
        for name, env in self.environments.items():
            temp_db = env.db_name + suffix
            owner = seen_db_names.get(temp_db)
            if owner is not None and owner != name:
                raise ValueError(
                    f"Environment '{name}' temp database '{temp_db}' "
                    f"(db_name + temp_db_suffix '{suffix}') collides with the live "
                    f"db_name of environment '{owner}'; a clone or restore into "
                    f"'{name}' would drop '{owner}'. Change the db_name or temp_db_suffix."
                )
        return self

    def is_protected(self, name: str) -> bool:
        env = self.env(name)
        if env.protected is not None:
            return env.protected
        return name == "production" or env.tier == "production"

    def env(self, name: str) -> EnvironmentConfig:
        try:
            return self.environments[name]
        except KeyError as exc:
            known = ", ".join(sorted(self.environments))
            raise KeyError(f"Unknown environment '{name}'. Known: {known}") from exc

    def referenced_env_vars(
        self,
        *,
        include_snapshot: bool = False,
    ) -> list[str]:
        refs = {self.postgres.password_env}
        if self.postgres.service_password_env:
            refs.add(self.postgres.service_password_env)
        if self.odoo.db_password_env:
            refs.add(self.odoo.db_password_env)
        if (
            self.backups.remote
            and self.backups.remote.policy != "disabled"
        ):
            remote = self.backups.remote
            for value in (
                remote.endpoint_env,
                remote.access_key_env,
                remote.secret_key_env,
                remote.region_env,
                remote.encryption_key_env,
            ):
                if value:
                    refs.add(value)
        if self.pitr.enabled:
            if self.pitr.replication_password_env:
                refs.add(self.pitr.replication_password_env)
            assert self.pitr.destination is not None
            for value in (
                self.pitr.destination.endpoint_env,
                self.pitr.destination.access_key_env,
                self.pitr.destination.secret_key_env,
                self.pitr.destination.session_token_env,
                self.pitr.destination.region_env,
                self.pitr.destination.encryption_key_env,
            ):
                if value:
                    refs.add(value)
        if (
            include_snapshot
            and
            self.snapshots.provider == "hetzner_cloud"
            and self.snapshots.hetzner_cloud is not None
            and not self.snapshots.hetzner_cloud.context
        ):
            refs.add(self.snapshots.hetzner_cloud.token_env)
        return sorted(refs)

    def snapshot_referenced_env_vars(self) -> list[str]:
        if (
            self.snapshots.provider == "hetzner_cloud"
            and self.snapshots.hetzner_cloud is not None
            and not self.snapshots.hetzner_cloud.context
        ):
            return [self.snapshots.hetzner_cloud.token_env]
        return []

    def missing_env_vars(
        self,
        *,
        include_snapshot: bool = False,
    ) -> list[str]:
        return [
            name
            for name in self.referenced_env_vars(
                include_snapshot=include_snapshot,
            )
            if not os.getenv(name)
        ]

    def missing_snapshot_env_vars(self) -> list[str]:
        return [
            name for name in self.snapshot_referenced_env_vars()
            if not os.getenv(name)
        ]


def load_config(path: str | Path = "odooctl.yml") -> OdooCtlConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise click.ClickException(f"Config file not found: {config_path}")
    data = yaml.safe_load(config_path.read_text())
    return OdooCtlConfig.model_validate(data)


def example_config() -> str:
    return """project:
  name: demo-odoo-project
  odoo_version: "19.0"

runtime:
  type: docker_compose
  compose_file: docker-compose.yml
  reverse_proxy: traefik

postgres:
  host: localhost
  port: 5432
  user: odoo
  password_env: ODOO_DB_PASSWORD

backups:
  local_path: backups
  retention:
    daily: 7
    weekly: 4
    monthly: 6
    grace_hours: 1
  remote:
    type: s3
    bucket: demo-odoo-backups
    policy: required
    verify_after_upload: true
    orphan_grace_hours: 24
    endpoint_env: ODOO_S3_ENDPOINT
    access_key_env: ODOO_S3_ACCESS_KEY
    secret_key_env: ODOO_S3_SECRET_KEY
    region: eu-central-1
    prefix: demo-odoo

pitr:
  enabled: false
  environment: production

snapshots:
  provider: none
  pre_deploy: disabled

redaction:
  min_secret_length: 6
  ignore_values:
    - odoo
    - admin
    - postgres

odoo:
  image: registry.example.com/odoo:19.0
  cli_command: odoo
  config_path: /etc/odoo/odoo.conf
  service: odoo
  addons_paths:
    - /mnt/extra-addons
    - /opt/odoo/custom-addons

environments:
  production:
    branch: main
    domain: odoo.example.com
    db_name: odoo_prod
    filestore_path: /var/lib/odoo/filestore/odoo_prod
    update_modules:
      - sale
      - stock
  staging:
    branch: staging
    domain: staging.odoo.example.com
    db_name: odoo_staging
    filestore_path: /var/lib/odoo/filestore/odoo_staging
    clone_from: production
    sanitize: true
    update_modules:
      - sale
      - stock
      - custom_module

sanitization:
  native_neutralize: preferred
  sql_files:
    - .sanitize/staging.sql
    - .sanitize/disable_connectors.sql
  disable_mail_servers: true
  disable_fetchmail: true
  disable_crons: true
  rewrite_base_url: true
  disable_payment_providers: true
  disable_queue_jobs: true
  purge_mail_queue: true
  temp_db_suffix: _incoming

healthcheck:
  path: /web/health
  timeout_seconds: 5
  retries: 12
  interval_seconds: 5
"""
