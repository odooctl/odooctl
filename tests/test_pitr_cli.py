from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from typer.testing import CliRunner

from odooctl.adapters.wal_s3 import PitrCoordinationLease
from odooctl.main import app


CONFIG = """\
project:
  name: demo
  odoo_version: "19.0"
runtime:
  compose_file: docker-compose.yml
postgres:
  password_env: ODOO_DB_PASSWORD
odoo:
  image: odoo:19.0
environments:
  production:
    branch: main
    domain: odoo.example.test
    db_name: odoo_prod
    filestore_path: ./filestore/production
pitr:
  enabled: true
  environment: production
  cluster_id: primary-cluster
  system_identifier: "7429384729384729"
  recovery_image: postgres@sha256:1111111111111111111111111111111111111111111111111111111111111111
  filestore_policy: database_only
  destination:
    bucket: pitr-archive
    prefix: demo/pitr
"""

runner = CliRunner()


def _config(tmp_path):
    path = tmp_path / "odooctl.yml"
    path.write_text(CONFIG)
    return path


def test_pitr_archive_config_is_registered_and_secret_free(
    tmp_path,
    monkeypatch,
):
    config = _config(tmp_path)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-render")

    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "pitr",
            "archive-config",
            "production",
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "archive_command = " in result.output
    assert "pitr wal push production" in result.output
    assert '--path "%p" --name "%f"' in result.output
    assert "must-not-render" not in result.output


def test_pitr_base_create_cli_records_dedicated_operation(
    tmp_path,
    monkeypatch,
):
    config = _config(tmp_path)
    monkeypatch.setattr(
        "odooctl.services.pitr.create_base_backup",
        lambda ctx, environment: SimpleNamespace(
            base_backup_id="production_base_123",
            system_identifier="7429384729384729",
            end_wal="000000010000000000000010",
            remote_uri="s3://pitr-archive/base/123",
        ),
    )

    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "pitr",
            "base",
            "create",
            "production",
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "production_base_123" in result.output
    operation_files = list(
        (tmp_path / ".odooctl" / "operations").glob(
            "*/operation.json"
        )
    )
    assert any(
        '"kind": "pitr_base_create"' in path.read_text()
        for path in operation_files
    )


def test_pitr_restore_cutover_cli_forwards_all_confirmations(
    tmp_path,
    monkeypatch,
):
    config = _config(tmp_path)
    observed = {}

    def fake_cutover(
        ctx,
        environment,
        restore_id,
        **kwargs,
    ):
        observed.update(
            environment=environment,
            restore_id=restore_id,
            **kwargs,
        )
        return SimpleNamespace(
            restore_id=restore_id,
            database="odoo_prod",
            filestore_consistency="not_included",
        )

    monkeypatch.setattr(
        "odooctl.services.pitr.cutover_restore",
        fake_cutover,
    )
    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "pitr",
            "restore",
            "cutover",
            "production",
            "--restore",
            "production_pitr_restore_123",
            "--confirm-environment",
            "production",
            "--confirm-database",
            "odoo_prod",
            "--accept-database-only",
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 0, result.output
    assert observed == {
        "environment": "production",
        "restore_id": "production_pitr_restore_123",
        "confirm_environment": "production",
        "confirm_database": "odoo_prod",
        "accept_database_only": True,
    }
    assert "filestore was not changed" in result.output


def test_pitr_lease_inspect_and_expired_recovery_cli(
    tmp_path,
    monkeypatch,
):
    config = _config(tmp_path)
    now = datetime.now(timezone.utc)
    lease = PitrCoordinationLease(
        key="demo/coordination/lease.json",
        lease_id="lease-123",
        owner="host-a",
        purpose="pitr-retention",
        generation=1,
        ttl_seconds=60,
        etag="etag-1",
        acquired_at=now - timedelta(minutes=2),
        expires_at=now - timedelta(minutes=1),
        observed_at=now,
    )
    observed = {}
    monkeypatch.setattr(
        "odooctl.services.pitr.inspect_coordination_lease",
        lambda ctx, environment: lease,
    )

    inspected = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "pitr",
            "lease",
            "inspect",
            "production",
            "--config",
            str(config),
        ],
    )

    assert inspected.exit_code == 0, inspected.output
    assert "lease-123" in inspected.output
    assert "expired" in inspected.output

    def recover(ctx, environment, **kwargs):
        observed.update(environment=environment, **kwargs)
        return lease

    monkeypatch.setattr(
        "odooctl.services.pitr.recover_expired_coordination_lease",
        recover,
    )
    recovered = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "pitr",
            "lease",
            "recover-expired",
            "production",
            "--confirm-lease-id",
            "lease-123",
            "--confirm-owner",
            "host-a",
            "--confirm-purpose",
            "pitr-retention",
            "--confirm-owner-stopped",
            "OWNER_STOPPED:lease-123",
            "--config",
            str(config),
        ],
    )

    assert recovered.exit_code == 0, recovered.output
    assert observed == {
        "environment": "production",
        "confirm_lease_id": "lease-123",
        "confirm_owner": "host-a",
        "confirm_purpose": "pitr-retention",
        "confirm_owner_stopped": "OWNER_STOPPED:lease-123",
    }
