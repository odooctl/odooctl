"""Opt-in real S3-compatible object-filestore round trip.

Uses the portable-backup integration settings:

    ODOOCTL_TEST_S3_ENDPOINT=... \
    ODOOCTL_TEST_S3_BUCKET=... \
    ODOOCTL_TEST_S3_ACCESS_KEY=... \
    ODOOCTL_TEST_S3_SECRET_KEY=... \
    pytest -m integration tests/integration/test_filestore_s3.py

The bucket must exist. Each run owns a UUID-scoped prefix and cleanup deletes
only the exact objects returned beneath that prefix.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from odooctl.adapters.object_filestore import ObjectFilestoreAdapter
from odooctl.config import FilestoreObjectStoreConfig
from odooctl.metadata.models import FilestoreMigrationManifest
from odooctl.services.filestore_storage import scan_filestore

pytestmark = pytest.mark.integration

_REQUIRED_SETTINGS = (
    "ODOOCTL_TEST_S3_ENDPOINT",
    "ODOOCTL_TEST_S3_BUCKET",
    "ODOOCTL_TEST_S3_ACCESS_KEY",
    "ODOOCTL_TEST_S3_SECRET_KEY",
)


def _require_settings() -> None:
    missing = [name for name in _REQUIRED_SETTINGS if not os.getenv(name)]
    if missing:
        pytest.skip(
            "filestore S3 integration is opt-in; missing: "
            + ", ".join(missing)
        )


def test_real_s3_object_filestore_round_trip(tmp_path: Path) -> None:
    _require_settings()
    pytest.importorskip(
        "boto3",
        reason="install odooctl[s3] to run the filestore S3 integration test",
    )
    run_id = uuid.uuid4().hex
    source = tmp_path / "source"
    (source / "ab").mkdir(parents=True)
    (source / "ab" / "attachment").write_bytes(
        b"filestore-integration-" + run_id.encode()
    )
    (source / "duplicate").write_bytes(
        (source / "ab" / "attachment").read_bytes()
    )
    entries, inventory, total_size = scan_filestore(source)
    config = FilestoreObjectStoreConfig(
        bucket=os.environ["ODOOCTL_TEST_S3_BUCKET"],
        region=os.getenv("ODOOCTL_TEST_S3_REGION", "us-east-1"),
        prefix=f"odooctl-filestore-integration/{run_id}",
        endpoint_env="ODOOCTL_TEST_S3_ENDPOINT",
        access_key_env="ODOOCTL_TEST_S3_ACCESS_KEY",
        secret_key_env="ODOOCTL_TEST_S3_SECRET_KEY",
        session_token_env=(
            "ODOOCTL_TEST_S3_SESSION_TOKEN"
            if os.getenv("ODOOCTL_TEST_S3_SESSION_TOKEN")
            else None
        ),
    )
    adapter = ObjectFilestoreAdapter(
        config,
        project=f"odooctl-filestore-it-{run_id[:12]}",
        environment="production",
        state_dir=tmp_path / ".odooctl",
    )
    first_id = f"production_filestore_{run_id}"
    first = FilestoreMigrationManifest(
        migration_id=first_id,
        project=adapter.project,
        environment="production",
        source_backend="local",
        target_backend="object_mirror",
        source_location=str(source),
        target_location=adapter.uri_for_migration(first_id),
        entries=entries,
        inventory_sha256=inventory,
        total_size=total_size,
    )
    primary_failure = False

    try:
        uploaded = adapter.upload_inventory(source, first)
        assert uploaded.object_count == len(entries)
        assert uploaded.total_size == total_size
        active = adapter.publish_active(
            first,
            expected_previous_migration_id=None,
        )
        assert active.migration_id == first_id

        downloaded = adapter.download_inventory(
            first,
            tmp_path / "downloaded",
        )
        assert scan_filestore(downloaded) == (
            entries,
            inventory,
            total_size,
        )

        second_id = f"{first_id}_next"
        second = first.model_copy(
            update={
                "migration_id": second_id,
                "target_location": adapter.uri_for_migration(second_id),
                "previous_active_migration_id": first_id,
            }
        )
        adapter.upload_inventory(source, second)
        assert adapter.publish_active(
            second,
            expected_previous_migration_id=first_id,
        ).migration_id == second_id
        adapter.delete_migration_manifest(
            first_id,
            confirm_not_active=True,
        )
        # Marker deletion never removes the shared content-addressed object.
        adapter.s3.verify_object_content(
            adapter.object_key(entries[0].sha256),
            expected_sha256=entries[0].sha256,
        )
    except BaseException:
        primary_failure = True
        raise
    finally:
        try:
            for item in adapter.s3.list_objects():
                adapter.s3._provider_call(
                    f"delete integration object {item.key}",
                    adapter.s3._client().delete_object,
                    Bucket=adapter.bucket,
                    Key=item.key,
                )
            assert adapter.s3.list_objects() == []
        except Exception:
            if not primary_failure:
                raise
