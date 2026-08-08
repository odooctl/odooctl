"""M12 runner tests — queue, worker, token verification, nonce tracking."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from odooctl.api.queue import OperationQueue, QueueEntry
from odooctl.security import tokens

TEST_KEY = "test-runner-key-xyz-0123456789abcdef"

MINIMAL_CONFIG = """\
project:
  name: test-project
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
    domain: prod.test.local
    port: 8069
    db_name: test_prod
    filestore_path: ./filestore/production
"""

MINIMAL_CONFIG_PROTECTED_STAGING = """\
project:
  name: test-project
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
    domain: prod.test.local
    port: 8069
    db_name: test_prod
    filestore_path: ./filestore/production
  secure_staging:
    branch: secure-staging
    domain: secure-staging.test.local
    port: 8070
    db_name: test_secure_staging
    filestore_path: ./filestore/secure_staging
    clone_from: production
    protected: true
"""


def _make_entry(
    op_id: str = "abc123",
    kind: str = "backup",
    project: str = "test-project",
    env: str = "production",
    nonce: str | None = None,
    roles: list[str] | None = None,
    params: dict | None = None,
) -> QueueEntry:
    extra: dict = {"roles": roles if roles is not None else ["operator"]}
    token = tokens.mint(
        TEST_KEY,
        action=kind,
        environment=env,
        project=project,
        ttl_seconds=300,
        nonce=nonce,
        **extra,
    )
    return QueueEntry.create(
        op_id=op_id,
        kind=kind,
        project=project,
        environment=env,
        actor="api-client",
        params_redacted=params or {},
        token=token,
    )


@pytest.fixture
def project_dir(tmp_path):
    (tmp_path / "odooctl.yml").write_text(MINIMAL_CONFIG)
    return tmp_path


@pytest.fixture
def fake_registry(project_dir):
    from odooctl.registry import Registry, RegisteredProject

    return Registry(
        path=project_dir / "registry.toml",
        active="test-project",
        projects={
            "test-project": RegisteredProject(
                name="test-project",
                path=project_dir,
                config="odooctl.yml",
            )
        },
    )


def _pre_create_op(project_dir: Path, op_id: str) -> None:
    from odooctl.operations.models import Operation, OperationKind
    from odooctl.operations.store import OperationStore

    store = OperationStore(project_dir / ".odooctl")
    op = Operation.create(OperationKind.BACKUP, "test-project", "production", "api-client", {})
    op.id = op_id
    store.save(op)


def _pre_create_dr_drill_op(project_dir: Path, op_id: str) -> None:
    from odooctl.operations.models import Operation, OperationKind
    from odooctl.operations.store import OperationStore

    store = OperationStore(project_dir / ".odooctl")
    op = Operation.create(OperationKind.DR_DRILL, "test-project", "production", "api-client", {})
    op.id = op_id
    store.save(op)


def _make_backup_result(project_dir: Path = None, backup_id: str = "bk001"):
    from odooctl.services.models import BackupResult

    return BackupResult(backup_id=backup_id)


# --- Queue tests ---


def test_queue_enqueue_creates_file(tmp_path):
    q = OperationQueue(tmp_path)
    entry = _make_entry()
    q.enqueue(entry)
    assert (tmp_path / "queue" / "abc123.json").exists()


def test_queue_claim_returns_entry(tmp_path):
    q = OperationQueue(tmp_path)
    q.enqueue(_make_entry())
    claimed = q.claim_next()
    assert claimed is not None
    assert claimed.op_id == "abc123"
    assert claimed.kind == "backup"


def test_queue_claim_atomic_rename(tmp_path):
    q = OperationQueue(tmp_path)
    q.enqueue(_make_entry())
    q.claim_next()
    assert not (tmp_path / "queue" / "abc123.json").exists()
    assert (tmp_path / "queue" / "abc123.running").exists()


def test_queue_claim_returns_none_when_empty(tmp_path):
    q = OperationQueue(tmp_path)
    assert q.claim_next() is None


def test_queue_complete_removes_file(tmp_path):
    q = OperationQueue(tmp_path)
    q.enqueue(_make_entry())
    q.claim_next()
    q.complete("abc123")
    assert not (tmp_path / "queue" / "abc123.running").exists()


def test_queue_fail_renames_to_failed(tmp_path):
    q = OperationQueue(tmp_path)
    q.enqueue(_make_entry())
    q.claim_next()
    q.fail("abc123")
    assert (tmp_path / "queue" / "abc123.failed").exists()
    assert not (tmp_path / "queue" / "abc123.running").exists()


def test_queue_entry_roundtrip(tmp_path):
    entry = _make_entry()
    q = OperationQueue(tmp_path)
    q.enqueue(entry)
    claimed = q.claim_next()
    assert claimed is not None
    assert claimed.op_id == entry.op_id
    assert claimed.kind == entry.kind
    assert claimed.token == entry.token


def test_queue_cancel_removes_json_file(tmp_path):
    """cancel() must remove the pending .json queue entry."""
    q = OperationQueue(tmp_path)
    q.enqueue(_make_entry())
    assert (tmp_path / "queue" / "abc123.json").exists()
    q.cancel("abc123")
    assert not (tmp_path / "queue" / "abc123.json").exists()


def test_queue_cancel_noop_if_already_claimed(tmp_path):
    """cancel() on a claimed (running) entry must not raise."""
    q = OperationQueue(tmp_path)
    q.enqueue(_make_entry())
    q.claim_next()
    q.cancel("abc123")
    assert (tmp_path / "queue" / "abc123.running").exists()


def test_queue_cancel_noop_if_already_gone(tmp_path):
    """cancel() on an already-absent entry must not raise."""
    q = OperationQueue(tmp_path)
    q.cancel("nonexistent")


def test_claim_next_skips_corrupt_json_entry(tmp_path):
    """claim_next() must tolerate corrupt JSON by moving the entry to .corrupt."""
    q = OperationQueue(tmp_path)
    (tmp_path / "queue" / "corrupt001.json").write_text("{invalid json}")
    result = q.claim_next()
    assert result is None
    assert not (tmp_path / "queue" / "corrupt001.json").exists()
    assert not (tmp_path / "queue" / "corrupt001.running").exists()
    assert (tmp_path / "queue" / "corrupt001.corrupt").exists()


def test_claim_next_skips_corrupt_entry_and_returns_valid(tmp_path):
    """claim_next() must skip corrupt entries and still return a valid one."""
    q = OperationQueue(tmp_path)
    import time as _time
    (tmp_path / "queue" / "bad000.json").write_text("!!not json!!")
    _time.sleep(0.01)  # ensure ordering
    q.enqueue(_make_entry(op_id="good001"))
    result = q.claim_next()
    assert result is not None
    assert result.op_id == "good001"


# --- Runner worker tests ---


def test_runner_claims_and_executes_backup(project_dir, fake_registry):
    from odooctl.operations.models import OperationStatus
    from odooctl.operations.store import OperationStore
    from odooctl.runner.worker import RunnerWorker

    op_id = "runner001"
    _pre_create_op(project_dir, op_id)
    entry = _make_entry(op_id=op_id)
    OperationQueue(project_dir / ".odooctl").enqueue(entry)

    worker = RunnerWorker(registry=fake_registry, api_key=TEST_KEY)
    with patch("odooctl.runner.worker.run_backup", return_value=_make_backup_result()):
        did_work = worker.claim_and_run()

    assert did_work is True
    store = OperationStore(project_dir / ".odooctl")
    assert store.load(op_id).status == OperationStatus.SUCCEEDED


def test_runner_rejects_tampered_token(project_dir, fake_registry):
    from odooctl.operations.models import OperationStatus
    from odooctl.operations.store import OperationStore
    from odooctl.runner.worker import RunnerWorker

    op_id = "runner002"
    _pre_create_op(project_dir, op_id)

    good = tokens.mint(TEST_KEY, action="backup", environment="production", project="test-project", ttl_seconds=300)
    tampered = good[:-5] + "XXXXX"
    entry = QueueEntry.create(
        op_id=op_id,
        kind="backup",
        project="test-project",
        environment="production",
        actor="api-client",
        params_redacted={},
        token=tampered,
    )
    OperationQueue(project_dir / ".odooctl").enqueue(entry)

    worker = RunnerWorker(registry=fake_registry, api_key=TEST_KEY)
    did_work = worker.claim_and_run()

    assert did_work is True
    store = OperationStore(project_dir / ".odooctl")
    assert store.load(op_id).status == OperationStatus.FAILED


def test_runner_records_nonce_as_consumed(project_dir, fake_registry):
    from odooctl.runner.worker import NonceStore, RunnerWorker

    op_id = "runner003"
    _pre_create_op(project_dir, op_id)
    entry = _make_entry(op_id=op_id)
    OperationQueue(project_dir / ".odooctl").enqueue(entry)

    worker = RunnerWorker(registry=fake_registry, api_key=TEST_KEY)
    with patch("odooctl.runner.worker.run_backup", return_value=_make_backup_result(backup_id="bk003")):
        worker.claim_and_run()

    nonce = tokens.decode_unverified(entry.token)["nonce"]
    assert NonceStore(project_dir / ".odooctl").is_consumed(nonce)


def test_runner_rejects_replayed_nonce(project_dir, fake_registry):
    from odooctl.operations.models import OperationStatus
    from odooctl.operations.store import OperationStore
    from odooctl.runner.worker import NonceStore, RunnerWorker

    nonce = "premarked_nonce_xyz"
    NonceStore(project_dir / ".odooctl").mark_consumed(nonce)

    op_id = "runner004"
    _pre_create_op(project_dir, op_id)
    entry = _make_entry(op_id=op_id, nonce=nonce)
    OperationQueue(project_dir / ".odooctl").enqueue(entry)

    worker = RunnerWorker(registry=fake_registry, api_key=TEST_KEY)
    did_work = worker.claim_and_run()

    assert did_work is True
    store = OperationStore(project_dir / ".odooctl")
    op = store.load(op_id)
    assert op.status == OperationStatus.FAILED
    assert "nonce" in (op.error or "")


def test_runner_returns_false_when_queue_empty(project_dir, fake_registry):
    from odooctl.runner.worker import RunnerWorker

    worker = RunnerWorker(registry=fake_registry, api_key=TEST_KEY)
    assert worker.claim_and_run() is False


def test_runner_marks_operation_failed_on_service_error(project_dir, fake_registry):
    from odooctl.operations.models import OperationStatus
    from odooctl.operations.store import OperationStore
    from odooctl.runner.worker import RunnerWorker

    op_id = "runner_fail"
    _pre_create_op(project_dir, op_id)
    entry = _make_entry(op_id=op_id)
    OperationQueue(project_dir / ".odooctl").enqueue(entry)

    worker = RunnerWorker(registry=fake_registry, api_key=TEST_KEY)
    with patch("odooctl.runner.worker.run_backup", side_effect=RuntimeError("disk full")):
        did_work = worker.claim_and_run()

    assert did_work is True
    store = OperationStore(project_dir / ".odooctl")
    op = store.load(op_id)
    assert op.status == OperationStatus.FAILED
    assert "disk full" in (op.error or "")


def test_runner_skips_cancelled_operation_after_claim(project_dir, fake_registry):
    """Runner must skip execution when operation status is CANCELLED (post-claim race)."""
    from odooctl.operations.models import OperationStatus
    from odooctl.operations.store import OperationStore
    from odooctl.runner.worker import RunnerWorker

    op_id = "cancel001"
    _pre_create_op(project_dir, op_id)

    store = OperationStore(project_dir / ".odooctl")
    store.update_status(op_id, OperationStatus.CANCELLED)

    entry = _make_entry(op_id=op_id)
    OperationQueue(project_dir / ".odooctl").enqueue(entry)

    call_count = []

    def mock_backup(*a, **kw):
        call_count.append(1)
        return _make_backup_result()

    worker = RunnerWorker(registry=fake_registry, api_key=TEST_KEY)
    with patch("odooctl.runner.worker.run_backup", side_effect=mock_backup):
        did_work = worker.claim_and_run()

    assert did_work is True
    assert not call_count
    assert store.load(op_id).status == OperationStatus.CANCELLED


def test_runner_claims_and_executes_dr_drill(project_dir, fake_registry):
    from odooctl.operations.models import OperationStatus
    from odooctl.operations.store import OperationStore
    from odooctl.runner.worker import RunnerWorker
    from odooctl.services.dr import DrDrillResult

    op_id = "drill001"
    _pre_create_dr_drill_op(project_dir, op_id)
    entry = _make_entry(op_id=op_id, kind="dr_drill", roles=["admin"])
    OperationQueue(project_dir / ".odooctl").enqueue(entry)

    worker = RunnerWorker(registry=fake_registry, api_key=TEST_KEY)
    with patch(
        "odooctl.runner.worker.run_dr_drill",
        return_value=DrDrillResult(status="success", environment="production", backup_id="bk-drill"),
    ) as drill:
        did_work = worker.claim_and_run()

    assert did_work is True
    drill.assert_called_once()
    drill_kwargs = drill.call_args.kwargs
    assert drill_kwargs["expected_project"] == "test-project"
    assert "db_adapter" not in drill_kwargs
    assert "fs_adapter" not in drill_kwargs
    assert callable(drill_kwargs["prepare_runtime_fn"])
    assert callable(drill_kwargs["restore_database_fn"])
    assert callable(drill_kwargs["restore_filestore_fn"])
    store = OperationStore(project_dir / ".odooctl")
    op = store.load(op_id)
    assert op.status == OperationStatus.SUCCEEDED
    events = store.load_events(op_id)
    assert any("DR drill complete: bk-drill" in e.message for e in events)


def test_runner_claims_and_executes_snapshot_create(project_dir, fake_registry):
    from odooctl.operations.models import Operation, OperationKind, OperationStatus
    from odooctl.operations.store import OperationStore
    from odooctl.runner.worker import RunnerWorker

    op_id = "snapcreate1"
    store = OperationStore(project_dir / ".odooctl")
    op = Operation.create(
        OperationKind.SNAPSHOT_CREATE,
        "test-project",
        "production",
        "api-client",
        {},
    )
    op.id = op_id
    store.save(op)
    OperationQueue(project_dir / ".odooctl").enqueue(
        _make_entry(op_id=op_id, kind="snapshot_create")
    )

    worker = RunnerWorker(registry=fake_registry, api_key=TEST_KEY)
    with patch(
        "odooctl.runner.worker.run_snapshot_create",
        return_value=SimpleNamespace(
            snapshot_id="production-snapshot-1",
            provider="aws_ebs",
            status="complete",
            source_resource_id="i-0123456789abcdef0",
        ),
    ) as create:
        assert worker.claim_and_run() is True

    create.assert_called_once()
    assert store.load(op_id).status == OperationStatus.SUCCEEDED


def test_runner_snapshot_reconcile_forwards_environment_lock(
    project_dir,
    fake_registry,
):
    from odooctl.operations.models import Operation, OperationKind, OperationStatus
    from odooctl.operations.store import OperationStore
    from odooctl.runner.worker import RunnerWorker

    op_id = "snapreconcile1"
    snapshot_id = "production-20260730-deadbeef"
    store = OperationStore(project_dir / ".odooctl")
    op = Operation.create(
        OperationKind.SNAPSHOT_RECONCILE,
        "test-project",
        "production",
        "api-client",
        {"snapshot_id": snapshot_id},
    )
    op.id = op_id
    store.save(op)
    OperationQueue(project_dir / ".odooctl").enqueue(
        _make_entry(
            op_id=op_id,
            kind="snapshot_reconcile",
            params={"snapshot_id": snapshot_id},
        )
    )

    worker = RunnerWorker(registry=fake_registry, api_key=TEST_KEY)
    with patch(
        "odooctl.runner.worker.run_snapshot_reconcile",
        return_value=SimpleNamespace(
            snapshot_id=snapshot_id,
            status="complete",
            source_resource_id="i-0123456789abcdef0",
        ),
    ) as reconcile:
        assert worker.claim_and_run() is True

    assert store.load(op_id).status == OperationStatus.SUCCEEDED
    reconcile.assert_called_once()
    assert reconcile.call_args.kwargs["expected_environment"] == "production"


def test_runner_snapshot_restore_forwards_typed_confirmation_and_lock(
    project_dir,
    fake_registry,
):
    from odooctl.operations.models import Operation, OperationKind, OperationStatus
    from odooctl.operations.store import OperationStore
    from odooctl.runner.worker import RunnerWorker

    op_id = "snaprestore1"
    snapshot_id = "production-20260730-deadbeef"
    store = OperationStore(project_dir / ".odooctl")
    op = Operation.create(
        OperationKind.SNAPSHOT_RESTORE,
        "test-project",
        "production",
        "api-client",
        {},
    )
    op.id = op_id
    store.save(op)
    OperationQueue(project_dir / ".odooctl").enqueue(
        _make_entry(
            op_id=op_id,
            kind="snapshot_restore",
            roles=["admin"],
            params={
                "snapshot_id": snapshot_id,
                "execute": True,
                "confirm_snapshot": snapshot_id,
                "confirm_resource": "i-0123456789abcdef0",
            },
        )
    )

    worker = RunnerWorker(registry=fake_registry, api_key=TEST_KEY)
    with patch(
        "odooctl.runner.worker.run_snapshot_restore",
        return_value=SimpleNamespace(
            executed=True,
            restored_resource_ids=("vol-new",),
            status="pending",
            message="volume creation is pending",
            plan=SimpleNamespace(
                provider="aws_ebs",
                source_resource_id="i-0123456789abcdef0",
                commands=(("aws", "ec2", "create-volume"),),
                destructive=False,
                notes=("unattached recovery volume",),
            ),
        ),
    ) as restore:
        assert worker.claim_and_run() is True

    assert store.load(op_id).status == OperationStatus.SUCCEEDED
    kwargs = restore.call_args.kwargs
    assert kwargs["confirm_snapshot_id"] == snapshot_id
    assert kwargs["confirm_resource_id"] == "i-0123456789abcdef0"
    assert kwargs["expected_environment"] == "production"
    result_event = next(
        event
        for event in store.load_events(op_id)
        if event.phase == "snapshot_restore"
    )
    assert result_event.message == "snapshot recovery pending"
    assert result_event.data["status"] == "pending"
    assert result_event.data["message"] == "volume creation is pending"
    assert result_event.data["source_resource_id"] == "i-0123456789abcdef0"
    assert result_event.data["plan"]["commands"] == [
        ["aws", "ec2", "create-volume"]
    ]


@pytest.mark.parametrize(
    ("kind", "service_name", "result"),
    [
        (
            "pitr_base_create",
            "create_base_backup",
            SimpleNamespace(
                base_backup_id="production_base_1",
                system_identifier="7429384729384729",
                end_wal="000000010000000000000010",
                remote_uri="s3://archive/base/1",
            ),
        ),
        (
            "pitr_reconcile",
            "reconcile_retention",
            SimpleNamespace(
                retained_base_backup_ids=("production_base_1",),
                deleted_base_backup_ids=(),
                deleted_wal_filenames=(),
            ),
        ),
    ],
)
def test_runner_dispatches_pitr_backup_operations(
    project_dir,
    fake_registry,
    kind,
    service_name,
    result,
):
    from odooctl.operations.models import (
        Operation,
        OperationKind,
        OperationStatus,
    )
    from odooctl.operations.store import OperationStore
    from odooctl.runner.worker import RunnerWorker

    op_id = f"{kind}1"
    store = OperationStore(project_dir / ".odooctl")
    operation = Operation.create(
        OperationKind(kind),
        "test-project",
        "production",
        "api-client",
        {},
    )
    operation.id = op_id
    store.save(operation)
    OperationQueue(project_dir / ".odooctl").enqueue(
        _make_entry(op_id=op_id, kind=kind)
    )

    worker = RunnerWorker(registry=fake_registry, api_key=TEST_KEY)
    with patch(
        f"odooctl.services.pitr.{service_name}",
        return_value=result,
    ) as service:
        assert worker.claim_and_run() is True

    service.assert_called_once()
    assert service.call_args.args[1] == "production"
    assert store.load(op_id).status == OperationStatus.SUCCEEDED


def test_runner_pitr_restore_forwards_plan_identity(
    project_dir,
    fake_registry,
):
    from odooctl.operations.models import (
        Operation,
        OperationKind,
        OperationStatus,
    )
    from odooctl.operations.store import OperationStore
    from odooctl.runner.worker import RunnerWorker

    op_id = "pitrrestore1"
    plan_id = "production_pitr_plan_123"
    store = OperationStore(project_dir / ".odooctl")
    operation = Operation.create(
        OperationKind.PITR_RESTORE,
        "test-project",
        "production",
        "api-client",
        {"plan_id": plan_id},
    )
    operation.id = op_id
    store.save(operation)
    OperationQueue(project_dir / ".odooctl").enqueue(
        _make_entry(
            op_id=op_id,
            kind="pitr_restore",
            roles=["admin"],
            params={"plan_id": plan_id},
        )
    )

    worker = RunnerWorker(registry=fake_registry, api_key=TEST_KEY)
    with patch(
        "odooctl.services.pitr.execute_restore",
        return_value=SimpleNamespace(
            restore_id="production_pitr_restore_123",
            new_database="test_prod_pitr_123",
            target_time="2026-07-30T10:00:00Z",
            recovered_lsn="0/5000000",
        ),
    ) as restore:
        assert worker.claim_and_run() is True

    restore.assert_called_once()
    assert restore.call_args.args[1:] == ("production", plan_id)
    assert store.load(op_id).status == OperationStatus.SUCCEEDED


def test_runner_pitr_cutover_forwards_typed_confirmation(
    project_dir,
    fake_registry,
):
    from odooctl.operations.models import (
        Operation,
        OperationKind,
        OperationStatus,
    )
    from odooctl.operations.store import OperationStore
    from odooctl.runner.worker import RunnerWorker

    op_id = "pitrcutover1"
    params = {
        "restore_id": "production_pitr_restore_123",
        "confirm_environment": "production",
        "confirm_database": "test_prod",
        "accept_database_only": True,
    }
    store = OperationStore(project_dir / ".odooctl")
    operation = Operation.create(
        OperationKind.PITR_CUTOVER,
        "test-project",
        "production",
        "api-client",
        params,
    )
    operation.id = op_id
    store.save(operation)
    OperationQueue(project_dir / ".odooctl").enqueue(
        _make_entry(
            op_id=op_id,
            kind="pitr_cutover",
            roles=["admin"],
            params=params,
        )
    )

    worker = RunnerWorker(registry=fake_registry, api_key=TEST_KEY)
    with patch(
        "odooctl.services.pitr.cutover_restore",
        return_value=SimpleNamespace(
            restore_id=params["restore_id"],
            database="test_prod",
            filestore_consistency="not_included",
        ),
    ) as cutover:
        assert worker.claim_and_run() is True

    assert cutover.call_args.args[1:] == (
        "production",
        params["restore_id"],
    )
    assert cutover.call_args.kwargs == {
        "confirm_environment": "production",
        "confirm_database": "test_prod",
        "accept_database_only": True,
    }
    assert store.load(op_id).status == OperationStatus.SUCCEEDED


def test_runner_filestore_verify_dispatches_and_records_result(
    project_dir,
    fake_registry,
):
    from odooctl.operations.models import (
        Operation,
        OperationKind,
        OperationStatus,
    )
    from odooctl.operations.store import OperationStore
    from odooctl.runner.worker import RunnerWorker

    op_id = "filestoreverify1"
    migration_id = "production_filestore_123"
    params = {
        "action": "verify",
        "migration_id": migration_id,
    }
    store = OperationStore(project_dir / ".odooctl")
    operation = Operation.create(
        OperationKind.FILESTORE_MIGRATE,
        "test-project",
        "production",
        "api-client",
        params,
    )
    operation.id = op_id
    store.save(operation)
    OperationQueue(project_dir / ".odooctl").enqueue(
        _make_entry(
            op_id=op_id,
            kind="filestore_migrate",
            roles=["admin"],
            params=params,
        )
    )

    worker = RunnerWorker(registry=fake_registry, api_key=TEST_KEY)
    with patch(
        "odooctl.services.filestore_storage.verify_migration",
        return_value=SimpleNamespace(
            migration_id=migration_id,
            inventory_sha256="a" * 64,
            object_count=2,
            total_size=42,
        ),
    ) as verify:
        assert worker.claim_and_run() is True

    verify.assert_called_once()
    assert verify.call_args.args[1:] == (
        "production",
        migration_id,
    )
    assert store.load(op_id).status == OperationStatus.SUCCEEDED
    event = next(
        item
        for item in store.load_events(op_id)
        if item.phase == "filestore_verify"
    )
    assert event.data["inventory_sha256"] == "a" * 64


def test_runner_rejects_absolute_filestore_download_if_queue_is_forged(
    project_dir,
    fake_registry,
):
    from odooctl.operations.models import (
        Operation,
        OperationKind,
        OperationStatus,
    )
    from odooctl.operations.store import OperationStore
    from odooctl.runner.worker import RunnerWorker

    op_id = "filestorepath1"
    params = {
        "action": "download",
        "migration_id": "production_filestore_123",
        "destination": "/tmp/outside-project",
    }
    store = OperationStore(project_dir / ".odooctl")
    operation = Operation.create(
        OperationKind.FILESTORE_MIGRATE,
        "test-project",
        "production",
        "api-client",
        params,
    )
    operation.id = op_id
    store.save(operation)
    OperationQueue(project_dir / ".odooctl").enqueue(
        _make_entry(
            op_id=op_id,
            kind="filestore_migrate",
            roles=["admin"],
            params=params,
        )
    )

    worker = RunnerWorker(registry=fake_registry, api_key=TEST_KEY)
    with patch(
        "odooctl.services.filestore_storage.download_migration",
    ) as download:
        assert worker.claim_and_run() is True

    download.assert_not_called()
    failed = store.load(op_id)
    assert failed.status == OperationStatus.FAILED
    assert "project-relative" in (failed.error or "")


def test_runner_rejects_protected_destructive_op_with_operator_role(tmp_path):
    """Runner must reject a destructive op on a protected env when token has operator-level roles.

    Simulates a malformed/forged queue entry where operator roles are embedded
    in the capability token but the target is an explicitly protected environment.
    Clone to secure_staging (protected=true) with operator roles must fail with
    an RBAC error, not reach dispatch.
    """
    from odooctl.operations.models import Operation, OperationKind, OperationStatus
    from odooctl.operations.store import OperationStore
    from odooctl.registry import RegisteredProject, Registry
    from odooctl.runner.worker import RunnerWorker

    (tmp_path / "odooctl.yml").write_text(MINIMAL_CONFIG_PROTECTED_STAGING)
    registry = Registry(
        path=tmp_path / "registry.toml",
        active="test-project",
        projects={"test-project": RegisteredProject("test-project", tmp_path, "odooctl.yml")},
    )

    store = OperationStore(tmp_path / ".odooctl")
    op_id = "rbac_prot_001"
    op = Operation.create(OperationKind.CLONE, "test-project", "secure_staging", "op-user", {})
    op.id = op_id
    store.save(op)

    # Capability token carries operator roles — not sufficient for a protected env clone
    entry = _make_entry(op_id=op_id, kind="clone", env="secure_staging", roles=["operator"])
    OperationQueue(tmp_path / ".odooctl").enqueue(entry)

    worker = RunnerWorker(registry=registry, api_key=TEST_KEY)
    did_work = worker.claim_and_run()

    assert did_work is True
    op = store.load(op_id)
    assert op.status == OperationStatus.FAILED
    assert "protected" in (op.error or "").lower()


def test_runner_once_processes_one_item(project_dir, fake_registry):
    from odooctl.runner.worker import RunnerWorker

    call_count = 0

    def mock_backup(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _make_backup_result(backup_id=f"bk_{call_count}")

    # Enqueue 2 items
    for i in range(2):
        op_id = f"once{i:03d}"
        _pre_create_op(project_dir, op_id)
        OperationQueue(project_dir / ".odooctl").enqueue(_make_entry(op_id=op_id))

    worker = RunnerWorker(registry=fake_registry, api_key=TEST_KEY)
    with patch("odooctl.runner.worker.run_backup", side_effect=mock_backup):
        worker.run_loop(once=True)

    assert call_count == 1

def test_run_loop_once_returns_true_on_success(project_dir, fake_registry):
    from odooctl.runner.worker import RunnerWorker

    op_id = "exit000"
    _pre_create_op(project_dir, op_id)
    OperationQueue(project_dir / ".odooctl").enqueue(_make_entry(op_id=op_id))

    worker = RunnerWorker(registry=fake_registry, api_key=TEST_KEY)
    with patch("odooctl.runner.worker.run_backup", return_value=_make_backup_result()):
        assert worker.run_loop(once=True) is True


def test_run_loop_once_returns_false_on_failed_operation(project_dir, fake_registry):
    from odooctl.runner.worker import RunnerWorker

    op_id = "exit001"
    _pre_create_op(project_dir, op_id)
    OperationQueue(project_dir / ".odooctl").enqueue(_make_entry(op_id=op_id))

    worker = RunnerWorker(registry=fake_registry, api_key=TEST_KEY)
    with patch("odooctl.runner.worker.run_backup", side_effect=RuntimeError("disk full")):
        assert worker.run_loop(once=True) is False


def test_run_loop_once_returns_false_on_rejected_token(project_dir, fake_registry):
    from odooctl.runner.worker import RunnerWorker

    op_id = "exit002"
    _pre_create_op(project_dir, op_id)
    good = tokens.mint(TEST_KEY, action="backup", environment="production", project="test-project", ttl_seconds=300)
    entry = QueueEntry.create(
        op_id=op_id,
        kind="backup",
        project="test-project",
        environment="production",
        actor="api-client",
        params_redacted={},
        token=good[:-5] + "XXXXX",
    )
    OperationQueue(project_dir / ".odooctl").enqueue(entry)

    worker = RunnerWorker(registry=fake_registry, api_key=TEST_KEY)
    assert worker.run_loop(once=True) is False


def test_run_loop_once_returns_true_on_empty_queue(project_dir, fake_registry):
    from odooctl.runner.worker import RunnerWorker

    worker = RunnerWorker(registry=fake_registry, api_key=TEST_KEY)
    assert worker.run_loop(once=True) is True


def test_run_loop_fail_fast_stops_on_failure(project_dir, fake_registry):
    from odooctl.runner.worker import RunnerWorker

    op_id = "exit003"
    _pre_create_op(project_dir, op_id)
    OperationQueue(project_dir / ".odooctl").enqueue(_make_entry(op_id=op_id))

    worker = RunnerWorker(registry=fake_registry, api_key=TEST_KEY)
    with patch("odooctl.runner.worker.run_backup", side_effect=RuntimeError("boom")):
        assert worker.run_loop(fail_fast=True) is False


def test_runner_command_exits_nonzero_on_failed_operation(project_dir, fake_registry, monkeypatch):
    import pytest as _pytest

    from odooctl.commands import runner as runner_cmd

    op_id = "exit004"
    _pre_create_op(project_dir, op_id)
    OperationQueue(project_dir / ".odooctl").enqueue(_make_entry(op_id=op_id))

    monkeypatch.setattr("odooctl.registry.load_registry", lambda: fake_registry)
    with patch("odooctl.runner.worker.run_backup", side_effect=RuntimeError("boom")):
        with _pytest.raises(SystemExit) as excinfo:
            runner_cmd.run(once=True, api_key=TEST_KEY)
    assert excinfo.value.code == 1


def test_runner_persists_redacted_operation_error(project_dir, fake_registry, monkeypatch):
    """A failed operation whose error message contains a secret env value is stored redacted."""
    from odooctl.operations.models import OperationStatus
    from odooctl.operations.store import OperationStore
    from odooctl.runner.worker import RunnerWorker

    secret = "runner-db-secret-789"
    monkeypatch.setenv("ODOO_DB_PASSWORD", secret)

    op_id = "redact001"
    _pre_create_op(project_dir, op_id)
    OperationQueue(project_dir / ".odooctl").enqueue(_make_entry(op_id=op_id))

    worker = RunnerWorker(registry=fake_registry, api_key=TEST_KEY)
    boom = RuntimeError(f"connection to db failed: password={secret}")
    with patch("odooctl.runner.worker.run_backup", side_effect=boom):
        did_work = worker.claim_and_run()

    assert did_work is True
    assert worker.last_run_ok is False
    store = OperationStore(project_dir / ".odooctl")
    op = store.load(op_id)
    assert op.status == OperationStatus.FAILED
    assert secret not in (op.error or "")
    assert "***REDACTED***" in (op.error or "")
    # The streamed/persisted failure event must be redacted too.
    for event in store.load_events(op_id):
        assert secret not in event.message


def test_engine_persists_redacted_operation_error(tmp_path, monkeypatch):
    """run_operation stores a redacted error when the wrapped block leaks a secret."""
    import pytest as _pytest

    from odooctl.operations.audit import AuditStore
    from odooctl.operations.engine import run_operation
    from odooctl.operations.models import OperationKind, OperationStatus
    from odooctl.operations.store import OperationStore

    secret = "engine-db-secret-321"
    monkeypatch.setenv("ODOO_DB_PASSWORD", secret)

    store = OperationStore(tmp_path)
    audit = AuditStore(tmp_path)
    with _pytest.raises(RuntimeError):
        with run_operation(
            store,
            audit,
            kind=OperationKind.BACKUP,
            project="test-project",
            environment="production",
            actor="cli",
            params_redacted={},
            state_dir=tmp_path,
        ) as op_ctx:
            raise RuntimeError(f"pg_dump: authentication failed: {secret}")

    op = store.load(op_ctx.op.id)
    assert op.status == OperationStatus.FAILED
    assert secret not in (op.error or "")
    assert "***REDACTED***" in (op.error or "")


# --- NonceStore purge / format tests (F12) — appended, do not modify above ---


def test_nonce_store_stores_timestamped_entries(tmp_path):
    """New format: {"nonces": {nonce: consumed_at_iso}}."""
    import json as _json
    from datetime import datetime

    from odooctl.runner.worker import NonceStore

    store = NonceStore(tmp_path)
    store.mark_consumed("nonce-a")

    raw = _json.loads((tmp_path / "consumed_nonces.json").read_text())
    assert isinstance(raw["nonces"], dict)
    assert "nonce-a" in raw["nonces"]
    # the value is a parseable ISO timestamp
    datetime.fromisoformat(raw["nonces"]["nonce-a"])


def test_nonce_store_purges_entries_older_than_retention(tmp_path):
    import json as _json
    from datetime import datetime, timedelta, timezone

    from odooctl.runner.worker import NONCE_RETENTION_SECONDS, NonceStore

    assert NONCE_RETENTION_SECONDS == 7200  # 2 × max token TTL

    now = datetime.now(timezone.utc)
    # Stored values are expiry timestamps: an expiry in the past is purged, one
    # in the future is kept.
    expired = (now - timedelta(seconds=60)).isoformat()
    future = (now + timedelta(seconds=NONCE_RETENTION_SECONDS)).isoformat()
    (tmp_path / "consumed_nonces.json").write_text(
        _json.dumps({"nonces": {"ancient": expired, "recent": future}})
    )

    store = NonceStore(tmp_path)
    # Before any write, the not-yet-expired entry is recorded as consumed.
    assert store.is_consumed("recent")

    store.mark_consumed("fresh")

    assert not store.is_consumed("ancient")  # expired → purged
    assert store.is_consumed("recent")
    assert store.is_consumed("fresh")


def test_nonce_store_replay_blocked_within_token_validity(tmp_path):
    """Replay protection must hold within the token TTL despite purging."""
    from odooctl.runner.worker import NonceStore

    store = NonceStore(tmp_path)
    store.mark_consumed("replayed-nonce")
    # Later consumptions (which trigger purges) must not evict a fresh nonce.
    for i in range(5):
        store.mark_consumed(f"other-{i}")
    assert store.is_consumed("replayed-nonce")


def test_nonce_store_accepts_legacy_list_format(tmp_path):
    """Old {"nonces": [..]} files stay consumed and migrate on first write."""
    import json as _json
    from datetime import datetime

    from odooctl.runner.worker import NonceStore

    (tmp_path / "consumed_nonces.json").write_text(
        _json.dumps({"nonces": ["legacy-a", "legacy-b"]})
    )
    store = NonceStore(tmp_path)
    assert store.is_consumed("legacy-a")
    assert store.is_consumed("legacy-b")
    assert not store.is_consumed("unknown")

    store.mark_consumed("new-nonce")

    raw = _json.loads((tmp_path / "consumed_nonces.json").read_text())
    assert isinstance(raw["nonces"], dict)
    # legacy entries migrated to now-timestamps, still blocked
    for nonce in ("legacy-a", "legacy-b", "new-nonce"):
        assert store.is_consumed(nonce)
        datetime.fromisoformat(raw["nonces"][nonce])


def test_nonce_consume_is_single_use_atomic(tmp_path):
    from odooctl.runner.worker import NonceStore

    store = NonceStore(tmp_path)
    assert store.consume("abc") is True   # first claim wins
    assert store.consume("abc") is False  # replay rejected
    assert store.is_consumed("abc")


def test_nonce_consume_retains_until_token_expiry(tmp_path):
    from datetime import datetime, timedelta, timezone

    from odooctl.runner.worker import NONCE_RETENTION_SECONDS, NonceStore

    store = NonceStore(tmp_path)
    # A token whose TTL exceeds the default retention must keep its nonce until
    # the token actually expires, not be purged early.
    far_future = datetime.now(timezone.utc) + timedelta(seconds=NONCE_RETENTION_SECONDS * 4)
    store.consume("longlived", expires_at=far_future)
    # Simulate a later mark that triggers a purge pass; the long-lived nonce
    # must survive because its stored expiry is far in the future.
    store.consume("other")
    assert store.is_consumed("longlived")


def test_nonce_store_survives_corrupt_file(tmp_path):
    from odooctl.runner.worker import NonceStore

    store = NonceStore(tmp_path)
    store.consume("a")
    # A truncated/corrupt file (older non-atomic write) must not crash; consume
    # still works and re-establishes a valid store.
    (tmp_path / "consumed_nonces.json").write_text('{"nonces": {"a": ')  # truncated JSON
    assert store.consume("b") is True
    assert store.is_consumed("b")
