"""Opt-in real S3-compatible WAL/base-backup round trip.

Uses the same settings as the portable-backup integration test:

    ODOOCTL_TEST_S3_ENDPOINT=... \
    ODOOCTL_TEST_S3_BUCKET=... \
    ODOOCTL_TEST_S3_ACCESS_KEY=... \
    ODOOCTL_TEST_S3_SECRET_KEY=... \
    pytest -m integration tests/integration/test_pitr_s3.py

The bucket must exist. Every run uses a UUID-scoped project/prefix and removes
only the exact WAL/base objects it created.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

import pytest

from odooctl.adapters.wal_s3 import WalS3Adapter
from odooctl.config import WalArchiveS3Config
from odooctl.metadata.models import PitrBaseBackupManifest

pytestmark = pytest.mark.integration

_REQUIRED_SETTINGS = (
    "ODOOCTL_TEST_S3_ENDPOINT",
    "ODOOCTL_TEST_S3_BUCKET",
    "ODOOCTL_TEST_S3_ACCESS_KEY",
    "ODOOCTL_TEST_S3_SECRET_KEY",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _require_settings() -> None:
    missing = [name for name in _REQUIRED_SETTINGS if not os.getenv(name)]
    if missing:
        pytest.skip(
            "PITR S3 integration is opt-in; missing: "
            + ", ".join(missing)
        )


def test_real_s3_wal_and_physical_base_round_trip(tmp_path: Path) -> None:
    _require_settings()
    pytest.importorskip(
        "boto3",
        reason="install odooctl[s3] to run the PITR S3 integration test",
    )
    run_id = uuid.uuid4().hex
    project = f"odooctl-pitr-it-{run_id[:12]}"
    cluster_id = "primary"
    system_identifier = "7429384729384729"
    config = WalArchiveS3Config(
        bucket=os.environ["ODOOCTL_TEST_S3_BUCKET"],
        region=os.getenv("ODOOCTL_TEST_S3_REGION", "us-east-1"),
        prefix=f"odooctl-pitr-integration/{run_id}",
        endpoint_env="ODOOCTL_TEST_S3_ENDPOINT",
        access_key_env="ODOOCTL_TEST_S3_ACCESS_KEY",
        secret_key_env="ODOOCTL_TEST_S3_SECRET_KEY",
    )
    adapter = WalS3Adapter(
        config,
        project,
        cluster_id,
        system_identifier,
    )
    wal_name = "000000010000000000000010"
    wal_path = tmp_path / wal_name
    wal_path.write_bytes(b"pitr-wal-" + run_id.encode())
    base_id = f"production_base_{run_id}"
    base_root = tmp_path / base_id
    pgdata = base_root / "pgdata"
    pgdata.mkdir(parents=True)
    (pgdata / "PG_VERSION").write_text("17\n")
    (pgdata / "backup_manifest").write_text(
        '{"PostgreSQL-Backup-Manifest-Version": 2, "Files": [], '
        '"WAL-Ranges": [{"Timeline": 1, "Start-LSN": "0/10000000", '
        '"End-LSN": "0/10000010"}]}'
    )
    artifacts = ["pgdata/PG_VERSION", "pgdata/backup_manifest"]
    checksums = {
        artifact: _sha256(base_root / artifact)
        for artifact in artifacts
    }
    sizes = {
        artifact: (base_root / artifact).stat().st_size
        for artifact in artifacts
    }
    remote_uri = (
        f"s3://{config.bucket}/{adapter.base_prefix(base_id)}"
    )
    manifest = PitrBaseBackupManifest(
        base_backup_id=base_id,
        project=project,
        environment="production",
        cluster_id=cluster_id,
        system_identifier=system_identifier,
        postgres_major=17,
        postgres_image="postgres:17.6",
        timeline=1,
        wal_segment_size=16 * 1024 * 1024,
        started_at="2026-07-30T10:00:00Z",
        completed_at="2026-07-30T10:01:00Z",
        start_lsn="0/10000000",
        end_lsn="0/10000010",
        start_wal=wal_name,
        end_wal=wal_name,
        artifact_paths=artifacts,
        checksums=checksums,
        sizes=sizes,
        remote_uri=remote_uri,
        status="complete",
        verified_at="2026-07-30T10:01:00Z",
    )
    (base_root / "manifest.json").write_text(
        manifest.model_dump_json(indent=2)
    )
    primary_failure = False

    try:
        archived = adapter.archive_wal(wal_path, wal_name)
        assert archived.filename == wal_name
        assert adapter.verify_wal(wal_name).sha256 == archived.sha256

        uploaded = adapter.upload_base_backup(base_id, base_root)
        assert uploaded.uri == remote_uri
        assert adapter.read_base_manifest(base_id) == manifest.model_dump(
            mode="json"
        )
        adapter.verify_base_backup(base_id)

        downloaded_wal = tmp_path / "downloaded-wal"
        adapter.download_wal(wal_name, downloaded_wal)
        assert downloaded_wal.read_bytes() == wal_path.read_bytes()
        downloaded = adapter.download_base_backup(
            base_id,
            tmp_path / "downloaded-base",
        )
        assert (downloaded / "pgdata" / "PG_VERSION").read_text() == "17\n"
        assert PitrBaseBackupManifest.model_validate_json(
            (downloaded / "manifest.json").read_text()
        ) == manifest
    except BaseException:
        primary_failure = True
        raise
    finally:
        try:
            adapter.delete_base_backup(base_id)
            adapter.delete_wal(wal_name)
            assert adapter.list_base_backups() == []
            assert adapter.list_wal() == []
        except Exception:
            if not primary_failure:
                raise
