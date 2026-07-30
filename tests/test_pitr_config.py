import pytest
from pydantic import ValidationError

from odooctl.config import (
    OdooCtlConfig,
    PitrConfig,
    PitrRetentionConfig,
    WalArchiveS3Config,
)


def _base_config() -> dict:
    return {
        "project": {"name": "demo", "odoo_version": "19.0"},
        "odoo": {"image": "odoo:19.0"},
        "environments": {
            "production": {
                "branch": "main",
                "domain": "odoo.example.com",
                "db_name": "odoo_prod",
                "filestore_path": "/srv/odoo/filestore/odoo_prod",
            },
            "staging": {
                "branch": "staging",
                "domain": "staging.example.com",
                "db_name": "odoo_staging",
                "filestore_path": "/srv/odoo/filestore/odoo_staging",
            },
        },
    }


def _enabled_pitr(**updates) -> dict:
    config = {
        "enabled": True,
        "environment": "production",
        "cluster_id": "primary-eu-1",
        "system_identifier": "7623400000000000001",
        "recovery_image": "postgres@sha256:" + ("1" * 64),
        "filestore_policy": "database_only",
        "destination": {
            "bucket": "demo-pitr",
            "prefix": "postgres/wal",
        },
    }
    config.update(updates)
    return config


def test_pitr_is_disabled_by_default_and_preserves_existing_configs():
    cfg = OdooCtlConfig.model_validate(_base_config())

    assert cfg.pitr == PitrConfig()
    assert cfg.pitr.enabled is False
    assert cfg.pitr.destination is None


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("cluster_id", None, "cluster_id is required"),
        ("system_identifier", None, "system_identifier is required"),
        ("recovery_image", None, "recovery_image is required"),
        ("destination", None, "destination is required"),
        ("filestore_policy", None, "filestore_policy must explicitly"),
    ],
)
def test_enabled_pitr_requires_explicit_recovery_contract(
    field: str,
    value: object,
    message: str,
):
    pitr = _enabled_pitr(**{field: value})

    with pytest.raises(ValidationError, match=message):
        OdooCtlConfig.model_validate({**_base_config(), "pitr": pitr})


def test_enabled_pitr_environment_must_exist():
    with pytest.raises(ValidationError, match="pitr.environment 'missing' is not defined"):
        OdooCtlConfig.model_validate(
            {
                **_base_config(),
                "pitr": _enabled_pitr(environment="missing"),
            }
        )


@pytest.mark.parametrize("cluster_id", ["../primary", "primary/cluster", "bad cluster", ""])
def test_pitr_cluster_id_must_be_one_safe_component(cluster_id: str):
    with pytest.raises(ValidationError, match="cluster_id"):
        OdooCtlConfig.model_validate(
            {
                **_base_config(),
                "pitr": _enabled_pitr(cluster_id=cluster_id),
            }
        )


def test_pitr_system_identifier_is_a_required_decimal_recovery_pin():
    cfg = OdooCtlConfig.model_validate(
        {
            **_base_config(),
            "pitr": _enabled_pitr(system_identifier="7623400000000000001"),
        }
    )

    assert cfg.pitr.system_identifier == "7623400000000000001"


@pytest.mark.parametrize(
    "system_identifier",
    ["", "0", "0123", "-1", "not-a-system-id", "1" * 21],
)
def test_pitr_system_identifier_rejects_unsafe_values(system_identifier: str):
    with pytest.raises(ValidationError, match="decimal PostgreSQL system identifier"):
        OdooCtlConfig.model_validate(
            {
                **_base_config(),
                "pitr": _enabled_pitr(system_identifier=system_identifier),
            }
        )


def test_pitr_destination_is_independent_of_portable_backup_destination():
    cfg = OdooCtlConfig.model_validate(
        {
            **_base_config(),
            "backups": {
                "remote": {
                    "policy": "disabled",
                    "endpoint_env": "IGNORED_BACKUP_ENDPOINT",
                }
            },
            "pitr": _enabled_pitr(
                destination={
                    "bucket": "wal-archive",
                    "endpoint_env": "PITR_ENDPOINT",
                    "access_key_env": "PITR_ACCESS_KEY",
                    "secret_key_env": "PITR_SECRET_KEY",
                    "session_token_env": "PITR_SESSION_TOKEN",
                    "region_env": "PITR_REGION",
                    "encryption_algorithm": "aws:kms",
                    "encryption_key_env": "PITR_KMS_KEY",
                },
                replication_password_env="PITR_REPLICATION_PASSWORD",
            ),
        }
    )

    refs = set(cfg.referenced_env_vars())
    assert {
        "PITR_ENDPOINT",
        "PITR_ACCESS_KEY",
        "PITR_SECRET_KEY",
        "PITR_SESSION_TOKEN",
        "PITR_REGION",
        "PITR_KMS_KEY",
        "PITR_REPLICATION_PASSWORD",
    } <= refs
    assert "IGNORED_BACKUP_ENDPOINT" not in refs


def test_disabled_pitr_does_not_require_or_report_destination_credentials():
    cfg = OdooCtlConfig.model_validate(
        {
            **_base_config(),
            "pitr": {
                "enabled": False,
                "destination": {
                    "bucket": "unused",
                    "endpoint_env": "UNUSED_PITR_ENDPOINT",
                },
                "replication_password_env": "UNUSED_REPLICATION_PASSWORD",
            },
        }
    )

    assert "UNUSED_PITR_ENDPOINT" not in cfg.referenced_env_vars()
    assert "UNUSED_REPLICATION_PASSWORD" not in cfg.referenced_env_vars()


def test_wal_archive_static_credentials_require_both_env_references():
    with pytest.raises(ValidationError, match="must be configured together"):
        WalArchiveS3Config(
            bucket="wal-archive",
            access_key_env="PITR_ACCESS_KEY",
        )


def test_wal_archive_session_token_requires_explicit_key_pair():
    with pytest.raises(ValidationError, match="session_token_env requires"):
        WalArchiveS3Config(
            bucket="wal-archive",
            session_token_env="PITR_SESSION_TOKEN",
        )


def test_wal_archive_kms_key_requires_kms_encryption():
    with pytest.raises(ValidationError, match="requires encryption_algorithm 'aws:kms'"):
        WalArchiveS3Config(
            bucket="wal-archive",
            encryption_algorithm="AES256",
            encryption_key_env="PITR_KMS_KEY",
        )


@pytest.mark.parametrize("prefix", ["/absolute", "../escape", "safe/../escape", "bad\\key"])
def test_wal_archive_prefix_rejects_unsafe_paths(prefix: str):
    with pytest.raises(ValidationError, match="prefix"):
        WalArchiveS3Config(bucket="wal-archive", prefix=prefix)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_backups": -1},
        {"grace_hours": -1},
    ],
)
def test_pitr_retention_rejects_unsafe_values(kwargs: dict):
    with pytest.raises(ValidationError):
        PitrRetentionConfig(**kwargs)


def test_disabled_pitr_can_disable_retention_but_enabled_pitr_keeps_one_base():
    assert PitrRetentionConfig(base_backups=0).base_backups == 0

    with pytest.raises(ValidationError, match="base_backups must be at least 1"):
        OdooCtlConfig.model_validate(
            {
                **_base_config(),
                "pitr": _enabled_pitr(retention={"base_backups": 0}),
            }
        )


def test_pitr_retention_defaults_keep_a_recoverable_floor():
    retention = PitrRetentionConfig()

    assert retention.base_backups == 2
    assert retention.grace_hours == 24


def test_pitr_temporary_credentials_are_only_env_references():
    cfg = OdooCtlConfig.model_validate(
        {
            **_base_config(),
            "pitr": _enabled_pitr(
                destination={
                    "bucket": "wal-archive",
                    "access_key_env": "PITR_ACCESS_KEY",
                    "secret_key_env": "PITR_SECRET_KEY",
                },
                replication_password_env="PITR_REPLICATION_PASSWORD",
            ),
        }
    )
    payload = cfg.pitr.model_dump(mode="json")

    assert payload["replication_password_env"] == "PITR_REPLICATION_PASSWORD"
    assert payload["destination"]["secret_key_env"] == "PITR_SECRET_KEY"
    assert "password" not in payload["destination"]
