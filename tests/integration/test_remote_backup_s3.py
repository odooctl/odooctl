"""Opt-in S3-compatible remote-backup round-trip integration test.

The target bucket must already exist. Run this test explicitly with:

    ODOOCTL_TEST_S3_ENDPOINT=... \
    ODOOCTL_TEST_S3_BUCKET=... \
    ODOOCTL_TEST_S3_ACCESS_KEY=... \
    ODOOCTL_TEST_S3_SECRET_KEY=... \
    pytest -m integration tests/integration/test_remote_backup_s3.py

``ODOOCTL_TEST_S3_REGION`` is optional and defaults to ``us-east-1``. Every
run writes beneath a UUID-scoped prefix and removes only that exact backup
prefix during teardown. The test never creates or deletes the configured
bucket.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from pathlib import Path

import pytest

from odooctl.config import OdooCtlConfig
from odooctl.context import ProjectContext
from odooctl.metadata.models import BackupManifest
from odooctl.metadata.store import MetadataStore
from odooctl.services.context import ServiceContext
from odooctl.services.remote_backup import (
    download_remote_backup,
    make_remote_adapter,
    publish_remote_backup,
    verify_remote_backup,
)
from odooctl.services.restore import validate_backup_dir

pytestmark = pytest.mark.integration

_REQUIRED_SETTINGS = (
    "ODOOCTL_TEST_S3_ENDPOINT",
    "ODOOCTL_TEST_S3_BUCKET",
    "ODOOCTL_TEST_S3_ACCESS_KEY",
    "ODOOCTL_TEST_S3_SECRET_KEY",
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _require_s3_settings() -> None:
    missing = [name for name in _REQUIRED_SETTINGS if not os.getenv(name)]
    if missing:
        pytest.skip(
            "S3 integration test is opt-in; missing environment variables: "
            + ", ".join(missing)
        )


def _service_context(tmp_path: Path, run_id: str) -> ServiceContext:
    config = OdooCtlConfig.model_validate(
        {
            "project": {
                "name": f"odooctl-s3-it-{run_id[:12]}",
                "odoo_version": "19.0",
            },
            "odoo": {"image": "odoo:19.0"},
            "environments": {
                "production": {
                    "branch": "main",
                    "domain": "odoo.example.test",
                    "db_name": "odoo_prod",
                    "filestore_path": "./filestore/odoo_prod",
                },
            },
            "backups": {
                "local_path": "backups",
                "remote": {
                    "bucket": os.environ["ODOOCTL_TEST_S3_BUCKET"],
                    "region": os.getenv("ODOOCTL_TEST_S3_REGION", "us-east-1"),
                    "prefix": f"odooctl-integration/{run_id}",
                    "endpoint_env": "ODOOCTL_TEST_S3_ENDPOINT",
                    "access_key_env": "ODOOCTL_TEST_S3_ACCESS_KEY",
                    "secret_key_env": "ODOOCTL_TEST_S3_SECRET_KEY",
                    "policy": "required",
                    "verify_after_upload": True,
                },
            },
        }
    )
    project = ProjectContext(
        root=tmp_path,
        config_path=tmp_path / "odooctl.yml",
        config=config,
    )
    return ServiceContext(project=project)


def _create_local_backup(
    ctx: ServiceContext,
    *,
    backup_id: str,
    db_payload: bytes,
    filestore_payload: bytes,
) -> tuple[Path, BackupManifest]:
    backup_dir = ctx.project.backups_dir / backup_id
    backup_dir.mkdir(parents=True)
    (backup_dir / "db.dump").write_bytes(db_payload)
    (backup_dir / "filestore.tar").write_bytes(filestore_payload)
    manifest = BackupManifest(
        backup_id=backup_id,
        project=ctx.project.config.project.name,
        environment="production",
        timestamp="2026-07-30T12:00:00Z",
        db_name="odoo_prod",
        filestore_path="./filestore/odoo_prod",
        artifact_paths=["db.dump", "filestore.tar"],
        odoo_version=ctx.project.config.project.odoo_version,
        checksums={
            "db_dump": _sha256(db_payload),
            "filestore": _sha256(filestore_payload),
        },
    )
    (backup_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2))
    MetadataStore(ctx.project.state_dir).save_backup_manifest(backup_id, manifest)
    return backup_dir, manifest


def test_real_s3_remote_backup_round_trip(tmp_path: Path) -> None:
    _require_s3_settings()
    pytest.importorskip("boto3", reason="install odooctl[s3] to run the S3 integration test")

    run_id = uuid.uuid4().hex
    backup_id = f"production_2026-07-30_120000_{run_id[:12]}"
    db_payload = b"odooctl-s3-integration-database-" + run_id.encode()
    filestore_payload = b"odooctl-s3-integration-filestore-" + run_id.encode()
    ctx = _service_context(tmp_path, run_id)
    backup_dir, manifest = _create_local_backup(
        ctx,
        backup_id=backup_id,
        db_payload=db_payload,
        filestore_payload=filestore_payload,
    )
    adapter = make_remote_adapter(ctx)
    primary_failure = False

    try:
        published = publish_remote_backup(
            ctx,
            backup_dir,
            manifest,
            adapter=adapter,
        )
        assert published.status == "complete"
        assert published.manifest.remote_verified_at is not None

        verified = verify_remote_backup(
            ctx,
            "production",
            backup_id,
            adapter=adapter,
        )
        assert verified.backup_id == backup_id
        assert verified.object_count == 3
        assert verified.manifest.checksums == manifest.checksums

        shutil.rmtree(backup_dir)
        assert not backup_dir.exists()

        downloaded = download_remote_backup(
            ctx,
            "production",
            backup_id,
            adapter=adapter,
        )
        downloaded_manifest = validate_backup_dir(
            downloaded,
            expected_project=ctx.project.config.project.name,
            expected_environment="production",
        )
        assert downloaded_manifest["backup_id"] == backup_id
        assert downloaded_manifest["checksums"] == manifest.checksums
        assert (downloaded / "db.dump").read_bytes() == db_payload
        assert (downloaded / "filestore.tar").read_bytes() == filestore_payload
    except BaseException:
        primary_failure = True
        raise
    finally:
        try:
            adapter.delete_backup(backup_id)
            assert adapter.list_objects() == []
        except Exception:
            if not primary_failure:
                raise
