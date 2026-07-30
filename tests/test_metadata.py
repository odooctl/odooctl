import pytest

from odooctl.metadata.models import (
    BackupManifest,
    DeploymentMetadata,
    PitrBaseBackupManifest,
    PitrRecoveryPlan,
    PitrRestoreMetadata,
    SnapshotManifest,
    SnapshotResource,
    SnapshotRestoreMetadata,
    WalReceipt,
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


def _pitr_base_manifest(
    base_backup_id: str = "base-20260730-deadbeef",
    *,
    completed_at: str = "2026-07-30T12:05:00Z",
) -> PitrBaseBackupManifest:
    return PitrBaseBackupManifest(
        base_backup_id=base_backup_id,
        project="demo project",
        environment="production",
        cluster_id="primary-eu-1",
        system_identifier="7623400000000000001",
        postgres_major=17,
        postgres_image="postgres@sha256:" + ("1" * 64),
        timeline=1,
        wal_segment_size=16 * 1024 * 1024,
        started_at="2026-07-30T12:00:00Z",
        completed_at=completed_at,
        start_lsn="0/01000000",
        end_lsn="0/03000000",
        start_wal="000000010000000000000001",
        end_wal="000000010000000000000003",
        artifact_paths=["base.tar", "backup_manifest"],
        checksums={
            "base.tar": "a" * 64,
            "backup_manifest": "b" * 64,
        },
        sizes={
            "base.tar": 1024,
            "backup_manifest": 512,
        },
        remote_uri=(
            "s3://demo-pitr/projects/demo/clusters/primary-eu-1/"
            "7623400000000000001/base/base-20260730-deadbeef"
        ),
        status="complete",
        verified_at="2026-07-30T12:06:00Z",
    )


def _wal_receipt(
    *,
    filename: str = "000000010000000000000003",
    digest: str = "c" * 64,
    archived_at: str = "2026-07-30T12:06:00Z",
) -> WalReceipt:
    return WalReceipt(
        project="demo project",
        environment="production",
        cluster_id="primary-eu-1",
        system_identifier="7623400000000000001",
        filename=filename,
        timeline=1,
        sha256=digest,
        size=16 * 1024 * 1024,
        archived_at=archived_at,
        remote_uri=(
            "s3://demo-pitr/projects/demo/clusters/primary-eu-1/"
            f"7623400000000000001/wal/00000001/{filename}"
        ),
    )


def _pitr_plan(plan_id: str = "plan-deadbeef") -> PitrRecoveryPlan:
    return PitrRecoveryPlan(
        plan_id=plan_id,
        project="demo project",
        environment="production",
        cluster_id="primary-eu-1",
        system_identifier="7623400000000000001",
        base_backup_id="base-20260730-deadbeef",
        database="odoo_prod",
        new_database="odoo_prod_pitr_deadbeef",
        target_time="2026-07-30T12:10:00Z",
        target_timeline=1,
        first_wal="000000010000000000000001",
        last_wal="000000010000000000000004",
        wal_count=4,
        wal_bytes=64 * 1024 * 1024,
        recovery_image="postgres@sha256:" + ("1" * 64),
    )


def _pitr_restore(restore_id: str = "restore-deadbeef") -> PitrRestoreMetadata:
    return PitrRestoreMetadata(
        restore_id=restore_id,
        plan_id="plan-deadbeef",
        base_backup_id="base-20260730-deadbeef",
        project="demo project",
        environment="production",
        cluster_id="primary-eu-1",
        system_identifier="7623400000000000001",
        database="odoo_prod",
        new_database="odoo_prod_pitr_deadbeef",
        target_time="2026-07-30T12:10:00Z",
        target_timeline=1,
    )


def test_pitr_store_creates_separate_deterministic_directories(tmp_path):
    store = MetadataStore(tmp_path / ".odooctl")

    assert (store.root / "pitr" / "base").is_dir()
    assert (store.root / "pitr" / "wal").is_dir()
    assert (store.root / "pitr" / "plans").is_dir()
    assert (store.root / "pitr" / "restores").is_dir()
    assert not any((store.root / "pitr").glob("*.json"))


def test_pitr_base_manifest_round_trips_and_lists_newest_first(tmp_path):
    store = MetadataStore(tmp_path / ".odooctl")
    older = _pitr_base_manifest()
    newer = _pitr_base_manifest(
        "base-20260730-feedface",
        completed_at="2026-07-30T13:05:00Z",
    ).model_copy(
        update={
            "remote_uri": (
                "s3://demo-pitr/projects/demo/clusters/primary-eu-1/"
                "7623400000000000001/base/base-20260730-feedface"
            )
        }
    )

    path = store.save_pitr_base_manifest(older)
    store.save_pitr_base_manifest(newer)

    assert path == store.root / "pitr" / "base" / f"{older.base_backup_id}.json"
    assert store.get_pitr_base_manifest(older.base_backup_id) == older
    assert store.list_pitr_base_manifests(environment="production") == [newer, older]
    assert store.list_pitr_base_manifests(cluster_id="another-cluster") == []


def test_pitr_base_store_rejects_filename_payload_identity_mismatch(tmp_path):
    store = MetadataStore(tmp_path / ".odooctl")
    manifest = _pitr_base_manifest("base-payload")
    path = store.root / "pitr" / "base" / "base-filename.json"
    path.write_text(manifest.model_dump_json())

    with pytest.raises(RuntimeError, match="identity mismatch"):
        store.get_pitr_base_manifest("base-filename")
    with pytest.raises(RuntimeError, match="identity mismatch"):
        store.list_pitr_base_manifests()


def test_wal_receipt_is_immutable_but_exact_retry_is_idempotent(tmp_path):
    store = MetadataStore(tmp_path / ".odooctl")
    receipt = _wal_receipt()

    path = store.save_wal_receipt(receipt)
    retry_path = store.save_wal_receipt(receipt)

    assert retry_path == path
    assert path == (
        store.root
        / "pitr"
        / "wal"
        / receipt.cluster_id
        / receipt.system_identifier
        / f"{receipt.filename}.json"
    )
    assert store.get_wal_receipt(
        receipt.cluster_id,
        receipt.system_identifier,
        receipt.filename,
    ) == receipt
    assert store.list_wal_receipts(environment="production") == [receipt]
    assert not list(path.parent.glob(".*.tmp"))


def test_conflicting_wal_receipt_never_overwrites_first_writer(tmp_path):
    store = MetadataStore(tmp_path / ".odooctl")
    original = _wal_receipt()
    conflicting = _wal_receipt(digest="d" * 64)
    store.save_wal_receipt(original)

    with pytest.raises(RuntimeError, match="Conflicting WAL receipt"):
        store.save_wal_receipt(conflicting)

    assert store.get_wal_receipt(
        original.cluster_id,
        original.system_identifier,
        original.filename,
    ) == original


def test_wal_receipt_rejects_timeline_mismatch_and_credential_uri():
    with pytest.raises(ValueError, match="timeline"):
        _wal_receipt(filename="000000020000000000000003")

    payload = _wal_receipt().model_dump()
    payload["remote_uri"] = "s3://access:secret@demo-pitr/archive/wal"
    with pytest.raises(ValueError, match="credential-free"):
        WalReceipt.model_validate(payload)


def test_pitr_metadata_requires_timezone_aware_timestamps():
    payload = _pitr_plan().model_dump()
    payload["target_time"] = "2026-07-30T12:10:00"

    with pytest.raises(ValueError, match="timezone-aware"):
        PitrRecoveryPlan.model_validate(payload)

    normalized = _pitr_plan().model_copy(
        update={"target_time": "2026-07-30T15:10:00+03:00"}
    )
    normalized = PitrRecoveryPlan.model_validate(normalized.model_dump())
    assert normalized.target_time == "2026-07-30T12:10:00Z"


def test_pitr_plan_and_restore_round_trip_with_lifecycle_updates(tmp_path):
    store = MetadataStore(tmp_path / ".odooctl")
    plan = _pitr_plan()
    restore = _pitr_restore()

    plan_path = store.save_pitr_recovery_plan(plan)
    restore_path = store.save_pitr_restore(restore)
    verified = PitrRestoreMetadata.model_validate(
        {
            **restore.model_dump(),
            "status": "verified",
            "verified": True,
            "recovered_at": "2026-07-30T12:10:00Z",
            "recovered_lsn": "0/04000000",
        }
    )
    store.save_pitr_restore(verified)

    assert plan_path == store.root / "pitr" / "plans" / f"{plan.plan_id}.json"
    assert restore_path == store.root / "pitr" / "restores" / f"{restore.restore_id}.json"
    assert store.get_pitr_recovery_plan(plan.plan_id) == plan
    assert store.list_pitr_recovery_plans(environment="production") == [plan]
    assert store.get_pitr_restore(restore.restore_id) == verified
    assert store.list_pitr_restores(environment="production") == [verified]


def test_pitr_store_rejects_unsafe_ids_and_identity_rebinding(tmp_path):
    store = MetadataStore(tmp_path / ".odooctl")

    with pytest.raises(ValueError, match="plan_id"):
        store.get_pitr_recovery_plan("../../escape")

    restore = _pitr_restore()
    store.save_pitr_restore(restore)
    rebound = PitrRestoreMetadata.model_validate(
        {
            **restore.model_dump(),
            "plan_id": "plan-other",
        }
    )
    with pytest.raises(RuntimeError, match="identity conflict"):
        store.save_pitr_restore(rebound)
