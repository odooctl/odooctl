from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
import click
from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

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
    type: str = "s3"
    bucket: str | None = None
    region: str | None = None
    prefix: str = ""
    endpoint_env: str | None = None
    access_key_env: str | None = None
    secret_key_env: str | None = None
    region_env: str | None = None
    encryption_algorithm: str | None = None
    encryption_key_env: str | None = None


class RetentionConfig(BaseModel):
    daily: int = 7
    weekly: int = 4
    monthly: int = 6


class BackupsConfig(BaseModel):
    local_path: str = "./backups"
    remote: RemoteBackupConfig | None = None
    retention: RetentionConfig = Field(default_factory=RetentionConfig)


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

    def referenced_env_vars(self) -> list[str]:
        refs = {self.postgres.password_env}
        if self.postgres.service_password_env:
            refs.add(self.postgres.service_password_env)
        if self.odoo.db_password_env:
            refs.add(self.odoo.db_password_env)
        if self.backups.remote:
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
        return sorted(refs)

    def missing_env_vars(self) -> list[str]:
        return [name for name in self.referenced_env_vars() if not os.getenv(name)]


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
  remote:
    type: s3
    bucket: demo-odoo-backups
    endpoint_env: ODOO_S3_ENDPOINT
    access_key_env: ODOO_S3_ACCESS_KEY
    secret_key_env: ODOO_S3_SECRET_KEY
    region: eu-central-1
    prefix: demo-odoo

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
