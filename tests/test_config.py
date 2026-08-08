from pathlib import Path

import pytest

from odooctl.commands.backup import redact_config_snapshot
from odooctl.config import OdooCtlConfig, RemoteBackupConfig, example_config, load_config


EXAMPLE_PATH = Path(__file__).resolve().parents[1] / "examples" / "odooctl.yml"
WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "odooctl-deploy.yml"


def test_example_config_loads(tmp_path: Path):
    path = tmp_path / "odooctl.yml"
    path.write_text(example_config())
    cfg = load_config(path)
    assert cfg.project.name == "demo-odoo-project"
    assert cfg.env("staging").sanitize is True
    assert cfg.postgres.password_env == "ODOO_DB_PASSWORD"


def test_config_uses_env_reference_not_secret_value():
    text = example_config()
    assert "password_env: ODOO_DB_PASSWORD" in text
    assert "password:" not in text


def test_example_file_matches_generated_config():
    assert EXAMPLE_PATH.read_text() == example_config()


def test_workflow_example_contains_deploy_command():
    workflow = WORKFLOW_PATH.read_text()
    assert "workflow_dispatch" in workflow
    assert "odooctl deploy ${{ inputs.environment }} --branch ${{ inputs.branch }}" in workflow
    assert "actions/setup-python@v" in workflow
    assert "expected_branch=" in workflow
    assert "is not allowed for environment" in workflow


def test_redact_config_snapshot_masks_sensitive_values():
    raw = (
        "admin_passwd = admin-secret\n"
        "db_password = db-secret\n"
        "# old admin_passwd = commented-secret\n"
        "; api_token = retired-token\n"
        "# old admin_passwd: colon-secret\n"
        "; password was free-text-secret\n"
        "# ordinary comment = unchanged\n"
        "xmlrpc_port = 8069\n"
    )
    redacted = redact_config_snapshot(raw)
    assert "admin-secret" not in redacted
    assert "db-secret" not in redacted
    assert "commented-secret" not in redacted
    assert "retired-token" not in redacted
    assert "colon-secret" not in redacted
    assert "free-text-secret" not in redacted
    assert "admin_passwd = ***REDACTED***" in redacted
    assert "# old admin_passwd = ***REDACTED***" in redacted
    assert "; api_token = ***REDACTED***" in redacted
    assert "# ***REDACTED***" in redacted
    assert "; ***REDACTED***" in redacted
    assert "# ordinary comment = unchanged" in redacted
    assert "xmlrpc_port = 8069" in redacted


def test_remote_backup_policy_defaults_to_best_effort_with_verification():
    remote = RemoteBackupConfig(bucket="odoo-backups")

    assert remote.policy == "best_effort"
    assert remote.verify_after_upload is True
    assert remote.orphan_grace_hours == 24


def test_remote_backup_orphan_grace_must_leave_active_upload_window():
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        RemoteBackupConfig(
            bucket="odoo-backups",
            orphan_grace_hours=0,
        )


def test_retention_grace_must_leave_concurrent_publish_window():
    from odooctl.config import RetentionConfig

    retention = RetentionConfig()
    assert retention.grace_hours == 1
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        RetentionConfig(grace_hours=0)


def test_active_remote_backup_requires_bucket():
    with pytest.raises(ValueError, match="bucket is required"):
        RemoteBackupConfig()


def test_disabled_remote_backup_can_omit_bucket():
    remote = RemoteBackupConfig(policy="disabled")

    assert remote.bucket is None


def test_disabled_remote_backup_does_not_require_credential_envs():
    cfg = OdooCtlConfig.model_validate(
        {
            **_base_config(),
            "backups": {
                "remote": {
                    "policy": "disabled",
                    "endpoint_env": "ODOO_S3_ENDPOINT",
                    "access_key_env": "ODOO_S3_ACCESS_KEY",
                    "secret_key_env": "ODOO_S3_SECRET_KEY",
                    "encryption_algorithm": "aws:kms",
                    "encryption_key_env": "ODOO_S3_KMS_KEY",
                }
            },
        }
    )

    assert not {
        "ODOO_S3_ENDPOINT",
        "ODOO_S3_ACCESS_KEY",
        "ODOO_S3_SECRET_KEY",
        "ODOO_S3_KMS_KEY",
    } & set(cfg.referenced_env_vars())


def test_remote_backup_static_credentials_require_both_env_references():
    with pytest.raises(ValueError, match="configured together"):
        RemoteBackupConfig(
            bucket="odoo-backups",
            access_key_env="ODOO_S3_ACCESS_KEY",
        )


def test_remote_backup_encryption_key_requires_algorithm():
    with pytest.raises(ValueError, match="requires encryption_algorithm"):
        RemoteBackupConfig(
            bucket="odoo-backups",
            encryption_key_env="ODOO_BACKUP_KMS_KEY_ID",
        )


@pytest.mark.parametrize("prefix", ["/absolute", "../escape", "safe/../escape", "safe\\escape"])
def test_remote_backup_prefix_rejects_unsafe_paths(prefix: str):
    with pytest.raises(ValueError, match="prefix"):
        RemoteBackupConfig(bucket="odoo-backups", prefix=prefix)


@pytest.mark.parametrize("tier", ["daily", "weekly", "monthly"])
def test_backup_retention_rejects_negative_tiers(tier: str):
    with pytest.raises(ValueError):
        OdooCtlConfig.model_validate(
            {
                **_base_config(),
                "backups": {"retention": {tier: -1}},
            }
        )


def test_missing_env_vars_reports_only_referenced_values(monkeypatch):
    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")
    monkeypatch.setenv("ODOO_S3_ENDPOINT", "https://s3.example.com")
    cfg = load_config(EXAMPLE_PATH)
    assert cfg.referenced_env_vars() == ["ODOO_DB_PASSWORD", "ODOO_S3_ACCESS_KEY", "ODOO_S3_ENDPOINT", "ODOO_S3_SECRET_KEY"]
    assert cfg.missing_env_vars() == ["ODOO_S3_ACCESS_KEY", "ODOO_S3_SECRET_KEY"]


def test_environment_clone_from_must_reference_known_environment(tmp_path: Path):
    path = tmp_path / "odooctl.yml"
    path.write_text(
        """project:\n  name: demo\n  odoo_version: \"19.0\"\nruntime:\n  compose_file: docker-compose.yml\nhealthcheck:\n  path: /web/health\n  timeout_seconds: 10\n  retries: 3\n  interval_seconds: 1\nodoo:\n  image: registry/odoo:latest\n  service: odoo\nenvironments:\n  production:\n    branch: main\n    domain: odoo.example.com\n    db_name: odoo_prod\n    filestore_path: /srv/filestore/prod\n  staging:\n    branch: staging\n    domain: staging.example.com\n    db_name: odoo_staging\n    filestore_path: /srv/filestore/staging\n    clone_from: preview\n"""
    )

    with pytest.raises(ValueError) as exc_info:
        load_config(path)

    assert "clone_from 'preview' is not defined" in str(exc_info.value)


def test_environment_clone_from_can_be_omitted(tmp_path: Path):
    path = tmp_path / "odooctl.yml"
    path.write_text(
        """project:\n  name: demo\n  odoo_version: \"19.0\"\nruntime:\n  compose_file: docker-compose.yml\nhealthcheck:\n  path: /web/health\n  timeout_seconds: 10\n  retries: 3\n  interval_seconds: 1\nodoo:\n  image: registry/odoo:latest\n  service: odoo\nenvironments:\n  production:\n    branch: main\n    domain: odoo.example.com\n    db_name: odoo_prod\n    filestore_path: /srv/filestore/prod\n  qa:\n    branch: qa\n    domain: qa.example.com\n    db_name: odoo_qa\n    filestore_path: /srv/filestore/qa\n"""
    )

    cfg = load_config(path)

    assert cfg.env("production").clone_from is None
    assert cfg.env("qa").clone_from is None


def test_environment_clone_from_cannot_reference_itself(tmp_path: Path):
    path = tmp_path / "odooctl.yml"
    path.write_text(
        """project:\n  name: demo\n  odoo_version: \"19.0\"\nruntime:\n  compose_file: docker-compose.yml\nhealthcheck:\n  path: /web/health\n  timeout_seconds: 10\n  retries: 3\n  interval_seconds: 1\nodoo:\n  image: registry/odoo:latest\n  service: odoo\nenvironments:\n  staging:\n    branch: staging\n    domain: staging.example.com\n    db_name: odoo_staging\n    filestore_path: /srv/filestore/staging\n    clone_from: staging\n"""
    )

    with pytest.raises(ValueError) as exc_info:
        load_config(path)

    assert "cannot clone_from itself" in str(exc_info.value)


def test_production_cannot_be_a_clone_target(tmp_path: Path):
    path = tmp_path / "odooctl.yml"
    path.write_text(
        """project:\n  name: demo\n  odoo_version: \"19.0\"\nruntime:\n  compose_file: docker-compose.yml\nhealthcheck:\n  path: /web/health\n  timeout_seconds: 10\n  retries: 3\n  interval_seconds: 1\nodoo:\n  image: registry/odoo:latest\n  service: odoo\nenvironments:\n  staging:\n    branch: staging\n    domain: staging.example.com\n    db_name: odoo_staging\n    filestore_path: /srv/filestore/staging\n  production:\n    branch: main\n    domain: odoo.example.com\n    db_name: odoo_prod\n    filestore_path: /srv/filestore/prod\n    clone_from: staging\n"""
    )

    with pytest.raises(ValueError) as exc_info:
        load_config(path)

    assert "Environment 'production' cannot be a clone target" in str(exc_info.value)


def test_environments_cannot_share_db_name(tmp_path: Path):
    path = tmp_path / "odooctl.yml"
    path.write_text(
        """project:\n  name: demo\n  odoo_version: \"19.0\"\nruntime:\n  compose_file: docker-compose.yml\nhealthcheck:\n  path: /web/health\n  timeout_seconds: 10\n  retries: 3\n  interval_seconds: 1\nodoo:\n  image: registry/odoo:latest\n  service: odoo\nenvironments:\n  production:\n    branch: main\n    domain: odoo.example.com\n    db_name: odoo_shared\n    filestore_path: /srv/filestore/prod\n  staging:\n    branch: staging\n    domain: staging.example.com\n    db_name: odoo_shared\n    filestore_path: /srv/filestore/staging\n    clone_from: production\n"""
    )

    with pytest.raises(ValueError) as exc_info:
        load_config(path)

    message = str(exc_info.value)
    assert "Environments 'production' and 'staging' cannot share db_name 'odoo_shared'" in message


def test_environments_cannot_share_filestore_path(tmp_path: Path):
    path = tmp_path / "odooctl.yml"
    path.write_text(
        """project:\n  name: demo\n  odoo_version: \"19.0\"\nruntime:\n  compose_file: docker-compose.yml\nhealthcheck:\n  path: /web/health\n  timeout_seconds: 10\n  retries: 3\n  interval_seconds: 1\nodoo:\n  image: registry/odoo:latest\n  service: odoo\nenvironments:\n  production:\n    branch: main\n    domain: odoo.example.com\n    db_name: odoo_prod\n    filestore_path: /srv/filestore/shared\n  staging:\n    branch: staging\n    domain: staging.example.com\n    db_name: odoo_staging\n    filestore_path: /srv/filestore/shared\n    clone_from: production\n"""
    )

    with pytest.raises(ValueError) as exc_info:
        load_config(path)

    message = str(exc_info.value)
    assert "Environments 'production' and 'staging' cannot share filestore 'path:/srv/filestore/shared'" in message


def test_environments_cannot_share_domain(tmp_path: Path):
    path = tmp_path / "odooctl.yml"
    path.write_text(
        """project:\n  name: demo\n  odoo_version: \"19.0\"\nruntime:\n  compose_file: docker-compose.yml\nhealthcheck:\n  path: /web/health\n  timeout_seconds: 10\n  retries: 3\n  interval_seconds: 1\nodoo:\n  image: registry/odoo:latest\n  service: odoo\nenvironments:\n  production:\n    branch: main\n    domain: odoo.example.com\n    db_name: odoo_prod\n    filestore_path: /srv/filestore/prod\n  staging:\n    branch: staging\n    domain: odoo.example.com\n    db_name: odoo_staging\n    filestore_path: /srv/filestore/staging\n    clone_from: production\n"""
    )

    with pytest.raises(ValueError) as exc_info:
        load_config(path)

    message = str(exc_info.value)
    assert "Environments 'production' and 'staging' cannot share domain 'odoo.example.com'" in message


def test_same_stack_db_selector_environments_can_share_domain(tmp_path: Path):
    path = tmp_path / "odooctl.yml"
    path.write_text(
        """project:\n  name: demo\n  odoo_version: \"19.0\"\nruntime:\n  compose_file: docker-compose.yml\nhealthcheck:\n  path: /web/health\n  timeout_seconds: 10\n  retries: 3\n  interval_seconds: 1\nodoo:\n  image: registry/odoo:latest\n  service: odoo\nenvironments:\n  qa:\n    stack: dev\n    branch: qa\n    domain: dev.example.com\n    db_name: odoo_qa\n    filestore_path: qa\n    db_selector: true\n  staging:\n    stack: dev\n    branch: staging\n    domain: dev.example.com\n    db_name: odoo_staging\n    filestore_path: staging\n    db_selector: true\n"""
    )

    cfg = load_config(path)

    assert cfg.env("qa").domain == cfg.env("staging").domain


def test_same_stack_environments_cannot_share_domain_without_db_selector(tmp_path: Path):
    path = tmp_path / "odooctl.yml"
    path.write_text(
        """project:\n  name: demo\n  odoo_version: \"19.0\"\nruntime:\n  compose_file: docker-compose.yml\nhealthcheck:\n  path: /web/health\n  timeout_seconds: 10\n  retries: 3\n  interval_seconds: 1\nodoo:\n  image: registry/odoo:latest\n  service: odoo\nenvironments:\n  qa:\n    stack: dev\n    branch: qa\n    domain: dev.example.com\n    db_name: odoo_qa\n    filestore_path: qa\n  staging:\n    stack: dev\n    branch: staging\n    domain: dev.example.com\n    db_name: odoo_staging\n    filestore_path: staging\n"""
    )

    with pytest.raises(ValueError) as exc_info:
        load_config(path)

    assert "unless both use db_selector in the same stack" in str(exc_info.value)


def test_named_volume_filestore_identity_includes_volume_name(tmp_path: Path):
    path = tmp_path / "odooctl.yml"
    path.write_text(
        """project:\n  name: demo\n  odoo_version: \"19.0\"\nruntime:\n  compose_file: docker-compose.yml\nhealthcheck:\n  path: /web/health\n  timeout_seconds: 10\n  retries: 3\n  interval_seconds: 1\nodoo:\n  image: registry/odoo:latest\n  service: odoo\nenvironments:\n  qa:\n    stack: dev\n    branch: qa\n    domain: qa.example.com\n    db_name: odoo_qa\n    filestore_path: odoo\n    filestore_volume: qa-data\n  staging:\n    stack: stage\n    branch: staging\n    domain: staging.example.com\n    db_name: odoo_staging\n    filestore_path: odoo\n    filestore_volume: staging-data\n"""
    )

    cfg = load_config(path)

    assert cfg.env("qa").filestore_volume == "qa-data"


def test_environments_cannot_share_named_volume_filestore_identity(tmp_path: Path):
    path = tmp_path / "odooctl.yml"
    path.write_text(
        """project:\n  name: demo\n  odoo_version: \"19.0\"\nruntime:\n  compose_file: docker-compose.yml\nhealthcheck:\n  path: /web/health\n  timeout_seconds: 10\n  retries: 3\n  interval_seconds: 1\nodoo:\n  image: registry/odoo:latest\n  service: odoo\nenvironments:\n  qa:\n    stack: dev\n    branch: qa\n    domain: qa.example.com\n    db_name: odoo_qa\n    filestore_path: odoo\n    filestore_volume: shared-data\n  staging:\n    stack: stage\n    branch: staging\n    domain: staging.example.com\n    db_name: odoo_staging\n    filestore_path: odoo\n    filestore_volume: shared-data\n"""
    )

    with pytest.raises(ValueError) as exc_info:
        load_config(path)

    assert "cannot share filestore 'volume:shared-data:odoo'" in str(exc_info.value)


def test_environments_cannot_share_branch(tmp_path: Path):
    path = tmp_path / "odooctl.yml"
    path.write_text(
        """project:\n  name: demo\n  odoo_version: \"19.0\"\nruntime:\n  compose_file: docker-compose.yml\nhealthcheck:\n  path: /web/health\n  timeout_seconds: 10\n  retries: 3\n  interval_seconds: 1\nodoo:\n  image: registry/odoo:latest\n  service: odoo\nenvironments:\n  production:\n    branch: main\n    domain: odoo.example.com\n    db_name: odoo_prod\n    filestore_path: /srv/filestore/prod\n  staging:\n    branch: main\n    domain: staging.example.com\n    db_name: odoo_staging\n    filestore_path: /srv/filestore/staging\n    clone_from: production\n"""
    )

    with pytest.raises(ValueError) as exc_info:
        load_config(path)

    message = str(exc_info.value)
    assert "Environments 'production' and 'staging' cannot share branch 'main'" in message


# ---------------------------------------------------------------------------
# C3/F8 — config-boundary input validation (identifiers and hostnames)
# ---------------------------------------------------------------------------

INJECTION_NAMES = [
    "foo; rm -rf /",
    "foo$(x)",
    "foo|bar",
    "foo bar",
    "foo/../bar",
    "foo`x`",
    "foo\nbar",
    "a" * 300,
]


def _base_config(env_name: str = "production", **env_overrides) -> dict:
    env = {
        "branch": "main",
        "domain": "odoo.example.com",
        "db_name": "odoo_prod",
        "filestore_path": "/srv/filestore/prod",
    }
    env.update(env_overrides)
    return {
        "project": {"name": "demo", "odoo_version": "19.0"},
        "odoo": {"image": "registry/odoo:latest"},
        "environments": {env_name: env},
    }


def test_valid_identifier_and_hostname_values_pass():
    cfg = OdooCtlConfig.model_validate(
        _base_config(
            env_name="pr-123.hotfix_2",
            db_name="odoo_prod-1.2",
            filestore_volume="qa-data",
            domain="staging-1.odoo.example.com",
        )
    )
    env = cfg.env("pr-123.hotfix_2")
    assert env.db_name == "odoo_prod-1.2"
    assert env.filestore_volume == "qa-data"
    assert env.domain == "staging-1.odoo.example.com"


@pytest.mark.parametrize("bad", INJECTION_NAMES)
def test_environment_name_rejects_injection(bad: str):
    with pytest.raises(ValueError) as exc_info:
        OdooCtlConfig.model_validate(_base_config(env_name=bad))
    message = str(exc_info.value)
    assert "environment name" in message
    assert repr(bad[:32]) in message
    if len(bad) > 32:
        assert repr(bad) not in message


@pytest.mark.parametrize("bad", INJECTION_NAMES)
def test_db_name_rejects_injection(bad: str):
    with pytest.raises(ValueError) as exc_info:
        OdooCtlConfig.model_validate(_base_config(db_name=bad))
    assert "db_name" in str(exc_info.value)


@pytest.mark.parametrize("bad", INJECTION_NAMES)
def test_filestore_volume_rejects_injection(bad: str):
    with pytest.raises(ValueError) as exc_info:
        OdooCtlConfig.model_validate(_base_config(filestore_volume=bad))
    assert "filestore_volume" in str(exc_info.value)


@pytest.mark.parametrize("bad", INJECTION_NAMES + ["*.example.com", "foo_bar.example.com", '{"a"}', "'quoted'"])
def test_domain_rejects_injection_and_invalid_hostnames(bad: str):
    with pytest.raises(ValueError) as exc_info:
        OdooCtlConfig.model_validate(_base_config(domain=bad))
    assert "domain" in str(exc_info.value)


@pytest.mark.parametrize("bad", INJECTION_NAMES)
def test_compose_service_names_reject_injection(bad: str):
    data = _base_config()
    data["odoo"]["service"] = bad
    with pytest.raises(ValueError):
        OdooCtlConfig.model_validate(data)

    data = _base_config()
    data["postgres"] = {"service": bad}
    with pytest.raises(ValueError):
        OdooCtlConfig.model_validate(data)


def test_identifier_rejects_dot_dot_without_slash():
    with pytest.raises(ValueError):
        OdooCtlConfig.model_validate(_base_config(db_name="foo..bar"))


def test_domain_normalized_to_lowercase():
    cfg = OdooCtlConfig.model_validate(_base_config(domain="ODOO.Example.COM"))
    assert cfg.env("production").domain == "odoo.example.com"


def test_invalid_value_is_redacted_to_32_chars_in_error():
    bad = "x" * 300
    with pytest.raises(ValueError) as exc_info:
        OdooCtlConfig.model_validate(_base_config(db_name=bad))
    message = str(exc_info.value)
    assert "x" * 32 in message
    assert "x" * 33 not in message


def test_example_configs_still_validate():
    load_config(EXAMPLE_PATH)
    multidb = EXAMPLE_PATH.parent / "multidb" / "odooctl.yml"
    if multidb.exists():
        load_config(multidb)


# --- Re-scan M1: cross-environment temp-DB collision ---

_COLLISION_CONFIG = """project:
  name: demo
  odoo_version: "19.0"
runtime:
  compose_file: docker-compose.yml
sanitization:
  temp_db_suffix: _incoming
environments:
  production:
    branch: main
    domain: prod.example.com
    db_name: odoo
    filestore_path: filestore/odoo
  staging:
    branch: staging
    domain: staging.example.com
    db_name: odoo_incoming
    filestore_path: filestore/odoo_incoming
odoo:
  image: registry/odoo:19
"""


def test_temp_db_collision_is_rejected(tmp_path: Path):
    # staging.db_name (odoo_incoming) == production.db_name + _incoming, so a
    # clone/restore into production would drop staging's live DB.
    path = tmp_path / "odooctl.yml"
    path.write_text(_COLLISION_CONFIG)
    with pytest.raises(ValueError, match="collides with the live db_name"):
        load_config(path)


def test_normal_two_env_config_still_validates(tmp_path: Path):
    path = tmp_path / "odooctl.yml"
    path.write_text(example_config())
    cfg = load_config(path)
    assert cfg.env("production").db_name != cfg.env("staging").db_name


# --- Re-scan M2: filestore_path validation ---


def _cfg_with_filestore_path(fp: str) -> str:
    return f"""project:
  name: demo
  odoo_version: "19.0"
runtime:
  compose_file: docker-compose.yml
environments:
  production:
    branch: main
    domain: prod.example.com
    db_name: odoo_prod
    filestore_path: "{fp}"
odoo:
  image: registry/odoo:19
"""


@pytest.mark.parametrize("bad", ["/", "", "foo/../bar", "filestore/../../etc"])
def test_filestore_path_rejects_dangerous_values(tmp_path: Path, bad: str):
    path = tmp_path / "odooctl.yml"
    path.write_text(_cfg_with_filestore_path(bad))
    with pytest.raises(ValueError):
        load_config(path)


@pytest.mark.parametrize(
    "good",
    ["filestore/odoo_prod", "/var/lib/odoo/filestore/odoo_prod", "/srv/filestore/prod"],
)
def test_filestore_path_accepts_valid_values(tmp_path: Path, good: str):
    path = tmp_path / "odooctl.yml"
    path.write_text(_cfg_with_filestore_path(good))
    cfg = load_config(path)
    assert cfg.env("production").filestore_path == good
