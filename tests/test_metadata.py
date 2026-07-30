import pytest

from odooctl.metadata.models import (
    BackupManifest,
    DeploymentMetadata,
    SnapshotManifest,
    SnapshotResource,
    SnapshotRestoreMetadata,
)
from odooctl.metadata.store import MetadataStore


def test_backup_manifest_round_trip_includes_artifacts_and_version():
    manifest = BackupManifest(
        backup_id="production_1",
        project="p",
        environment="production",
        db_name="odoo",
        odoo_version="19.0",
        filestore_path="/srv/filestore/odoo",
        artifact_paths=["db.dump", "filestore.tar"],
        checksums={"db.dump": "abc"},
        backup_mode="full",
    )

    data = manifest.model_dump()
    restored = BackupManifest.model_validate(data)

    assert restored.backup_id == "production_1"
    assert restored.schema_version == 1
    assert restored.filestore_path == "/srv/filestore/odoo"
    assert restored.backup_mode == "full"
    assert restored.artifact_paths == ["db.dump", "filestore.tar"]
    assert restored.checksums == {"db.dump": "abc"}


def test_metadata_store_writes_latest_files(tmp_path):
    store = MetadataStore(tmp_path / ".odooctl")
    manifest = BackupManifest(
        backup_id="production_1",
        project="p",
        environment="production",
        db_name="odoo",
        odoo_version="19.0",
    )
    store.save_backup_manifest("production_1", manifest)
    assert store.latest_backup("production")["db_name"] == "odoo"
    dep = DeploymentMetadata(project="p", environment="staging", branch="staging", status="success")
    store.save_deployment(dep)
    assert store.latest_deployment("staging")["status"] == "success"


def test_updating_older_backup_manifest_does_not_move_latest_pointer(tmp_path):
    store = MetadataStore(tmp_path / ".odooctl")
    older = BackupManifest(
        backup_id="production_1",
        project="p",
        environment="production",
        db_name="odoo",
        odoo_version="19.0",
    )
    newer = older.model_copy(
        update={
            "backup_id": "production_2",
            "timestamp": "2026-07-30T12:00:00Z",
        }
    )
    store.save_backup_manifest(older.backup_id, older)
    store.save_backup_manifest(newer.backup_id, newer)

    store.update_backup_manifest(
        older.model_copy(
            update={
                "remote_status": "complete",
                "remote_verified_at": "2026-07-30T13:00:00Z",
            }
        )
    )

    assert store.latest_backup("production")["backup_id"] == newer.backup_id
    updated = (store.root / "backups" / f"{older.backup_id}.json").read_text()
    assert BackupManifest.model_validate_json(updated).remote_status == "complete"


def test_snapshot_manifest_uses_its_own_index_and_round_trips(tmp_path):
    store = MetadataStore(tmp_path / ".odooctl")
    manifest = SnapshotManifest(
        snapshot_id="production-20260730-deadbeef",
        project="p",
        environment="production",
        provider="aws_ebs",
        source_resource_id="i-0123456789abcdef0",
        resources=[
            SnapshotResource(
                snapshot_resource_id="snap-123",
                source_resource_id="vol-123",
                kind="ebs_volume",
                state="completed",
                location="us-east-1a",
            )
        ],
        scope=["ec2_instance_all_attached_ebs_volumes"],
        consistency="crash_consistent",
    )

    path = store.save_snapshot_manifest(manifest)

    assert path.parent.name == "snapshots"
    assert store.get_snapshot(manifest.snapshot_id) == manifest
    assert store.list_snapshots("production") == [manifest]
    assert not (tmp_path / ".odooctl" / "backups" / path.name).exists()


def test_snapshot_store_rejects_filename_payload_identity_mismatch(tmp_path):
    store = MetadataStore(tmp_path / ".odooctl")
    manifest = SnapshotManifest(
        snapshot_id="bar",
        project="p",
        environment="production",
        provider="aws_ebs",
        source_resource_id="i-123",
    )
    swapped = tmp_path / ".odooctl" / "snapshots" / "foo.json"
    swapped.write_text(manifest.model_dump_json())

    with pytest.raises(RuntimeError, match="identity mismatch"):
        store.get_snapshot("foo")
    with pytest.raises(RuntimeError, match="identity mismatch"):
        store.list_snapshots()


def test_snapshot_store_rejects_unsafe_filename_components(tmp_path):
    store = MetadataStore(tmp_path / ".odooctl")
    manifest = SnapshotManifest(
        snapshot_id="safe-id",
        project="p",
        environment="../production",
        provider="aws_ebs",
        source_resource_id="i-123",
    )
    with pytest.raises(ValueError, match="environment"):
        store.save_snapshot_manifest(manifest)

    restore = SnapshotRestoreMetadata(
        snapshot_id="../unsafe",
        project="p",
        environment="production",
        provider="aws_ebs",
        source_resource_id="i-123",
        executed=True,
        status="pending",
    )
    with pytest.raises(ValueError, match="snapshot_id"):
        store.save_snapshot_restore(restore)


def test_previous_successful_deployment_uses_success_before_current_failure(tmp_path):
    store = MetadataStore(tmp_path / ".odooctl")
    store.save_deployment(
        DeploymentMetadata(
            project="p",
            environment="production",
            timestamp="2026-01-01T00:00:00Z",
            branch="main",
            commit="old",
            status="success",
        )
    )
    store.save_deployment(
        DeploymentMetadata(
            project="p",
            environment="production",
            timestamp="2026-01-02T00:00:00Z",
            branch="main",
            commit="bad",
            status="failed",
        )
    )
    store.save_deployment(
        DeploymentMetadata(
            project="p",
            environment="staging",
            timestamp="2026-01-03T00:00:00Z",
            branch="staging",
            commit="stage",
            status="success",
        )
    )

    previous = store.previous_successful_deployment("production")
    assert previous is not None
    assert previous["commit"] == "old"
    assert store.previous_successful_deployment("development") is None


def test_previous_successful_deployment_uses_success_before_current_success(tmp_path):
    store = MetadataStore(tmp_path / ".odooctl")
    store.save_deployment(
        DeploymentMetadata(
            project="p",
            environment="production",
            timestamp="2026-01-01T00:00:00Z",
            branch="main",
            commit="good",
            status="success",
        )
    )
    store.save_deployment(
        DeploymentMetadata(
            project="p",
            environment="production",
            timestamp="2026-01-02T00:00:00Z",
            branch="main",
            commit="bad-but-healthy",
            status="success",
        )
    )

    previous = store.previous_successful_deployment("production")
    assert previous is not None
    assert previous["commit"] == "good"


def test_previous_successful_deployment_requires_prior_success(tmp_path):
    store = MetadataStore(tmp_path / ".odooctl")
    store.save_deployment(
        DeploymentMetadata(
            project="p",
            environment="production",
            timestamp="2026-01-01T00:00:00Z",
            branch="main",
            commit="current",
            status="success",
        )
    )

    assert store.previous_successful_deployment("production") is None


def test_previous_successful_deployment_ignores_prefix_sibling_environment(tmp_path):
    store = MetadataStore(tmp_path / ".odooctl")
    store.save_deployment(
        DeploymentMetadata(
            project="p",
            environment="prod",
            timestamp="2026-01-01T00:00:00Z",
            branch="main",
            commit="prod-current",
            status="success",
        )
    )
    store.save_deployment(
        DeploymentMetadata(
            project="p",
            environment="prod-eu",
            timestamp="2026-01-02T00:00:00Z",
            branch="main",
            commit="prod-eu-old",
            status="success",
        )
    )
    store.save_deployment(
        DeploymentMetadata(
            project="p",
            environment="prod-eu",
            timestamp="2026-01-03T00:00:00Z",
            branch="main",
            commit="prod-eu-current",
            status="success",
        )
    )

    assert store.previous_successful_deployment("prod") is None


def test_previous_successful_deployment_prefers_same_environment_over_prefix_sibling(tmp_path):
    store = MetadataStore(tmp_path / ".odooctl")
    store.save_deployment(
        DeploymentMetadata(
            project="p",
            environment="prod",
            timestamp="2026-01-01T00:00:00Z",
            branch="main",
            commit="prod-previous",
            status="success",
        )
    )
    store.save_deployment(
        DeploymentMetadata(
            project="p",
            environment="prod",
            timestamp="2026-01-02T00:00:00Z",
            branch="main",
            commit="prod-current",
            status="failed",
        )
    )
    store.save_deployment(
        DeploymentMetadata(
            project="p",
            environment="prod-eu",
            timestamp="2026-01-03T00:00:00Z",
            branch="main",
            commit="prod-eu-current",
            status="success",
        )
    )

    previous = store.previous_successful_deployment("prod")

    assert previous is not None
    assert previous["environment"] == "prod"
    assert previous["commit"] == "prod-previous"
