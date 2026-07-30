"""TDD tests for M7 operation engine — written before implementation."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

MINIMAL_CONFIG = """\
project:
  name: demo
  odoo_version: "19.0"
runtime:
  compose_file: docker-compose.yml
postgres:
  host: localhost
  port: 5432
  user: odoo
  password_env: ODOO_DB_PASSWORD
backups:
  local_path: backups
healthcheck:
  path: /web/health
  timeout_seconds: 10
  retries: 3
  interval_seconds: 1
odoo:
  image: registry/odoo:latest
  service: odoo
environments:
  production:
    branch: main
    domain: odoo.example.com
    db_name: odoo_prod
    filestore_path: /srv/filestore/prod
  staging:
    branch: staging
    domain: staging.example.com
    db_name: odoo_staging
    filestore_path: /srv/filestore/staging
    clone_from: production
    sanitize: true
"""


# ---- Operation model ----

def test_operation_kind_values():
    from odooctl.operations.models import OperationKind
    assert OperationKind.BACKUP.value == "backup"
    assert OperationKind.DEPLOY.value == "deploy"
    assert OperationKind.RESTORE.value == "restore"
    assert OperationKind.CLONE.value == "clone"
    assert OperationKind.ENV_CREATE.value == "env_create"
    assert OperationKind.ENV_DESTROY.value == "env_destroy"
    assert OperationKind.UPDATE_MODULES.value == "update_modules"
    assert OperationKind.ROLLBACK.value == "rollback"
    assert OperationKind.DR_DRILL.value == "dr_drill"
    assert OperationKind.SNAPSHOT_CREATE.value == "snapshot_create"
    assert OperationKind.SNAPSHOT_RECONCILE.value == "snapshot_reconcile"
    assert OperationKind.SNAPSHOT_RESTORE.value == "snapshot_restore"


def test_operation_status_values():
    from odooctl.operations.models import OperationStatus
    assert OperationStatus.QUEUED.value == "queued"
    assert OperationStatus.RUNNING.value == "running"
    assert OperationStatus.SUCCEEDED.value == "succeeded"
    assert OperationStatus.FAILED.value == "failed"
    assert OperationStatus.CANCELLED.value == "cancelled"


def test_operation_create_returns_queued():
    from odooctl.operations.models import Operation, OperationKind, OperationStatus
    op = Operation.create(OperationKind.BACKUP, "demo", "staging", "cli", {"env": "staging"})
    assert op.status == OperationStatus.QUEUED
    assert op.kind == OperationKind.BACKUP
    assert op.project == "demo"
    assert op.environment == "staging"
    assert op.actor == "cli"
    assert len(op.id) > 0


def test_operation_json_roundtrip():
    from odooctl.operations.models import Operation, OperationKind, OperationStatus
    op = Operation.create(OperationKind.RESTORE, "proj", "production", "web", {})
    text = op.to_json()
    loaded = Operation.from_json(text)
    assert loaded.id == op.id
    assert loaded.kind == OperationKind.RESTORE
    assert loaded.status == OperationStatus.QUEUED
    assert loaded.project == "proj"
    assert loaded.environment == "production"


def test_event_json_roundtrip():
    from odooctl.operations.models import Event
    ev = Event(op_id="abc", seq=3, timestamp="2026-01-01T00:00:00+00:00",
               level="info", phase="backup", message="starting dump", data={"size": 100})
    loaded = Event.from_json(ev.to_json())
    assert loaded.op_id == "abc"
    assert loaded.seq == 3
    assert loaded.phase == "backup"
    assert loaded.message == "starting dump"
    assert loaded.data == {"size": 100}


def test_audit_entry_roundtrip():
    from odooctl.operations.models import AuditEntry
    entry = AuditEntry(
        actor="cli", action="backup", target="production",
        params_redacted={"env": "production"}, outcome="succeeded",
        op_id="op123", timestamp="2026-01-01T00:00:00+00:00",
    )
    d = entry.to_dict()
    loaded = AuditEntry.from_dict({**d, "current_hash": "abc"})
    assert loaded.actor == "cli"
    assert loaded.action == "backup"
    assert loaded.target == "production"
    assert loaded.outcome == "succeeded"
    assert loaded.op_id == "op123"


# ---- OperationStore ----

def test_store_save_and_load(tmp_path):
    from odooctl.operations.models import Operation, OperationKind
    from odooctl.operations.store import OperationStore
    store = OperationStore(tmp_path)
    op = Operation.create(OperationKind.BACKUP, "demo", "staging", "cli", {})
    store.save(op)
    loaded = store.load(op.id)
    assert loaded.id == op.id
    assert loaded.kind == OperationKind.BACKUP


def test_store_load_missing_raises(tmp_path):
    from odooctl.operations.store import OperationStore
    store = OperationStore(tmp_path)
    with pytest.raises(KeyError):
        store.load("nonexistent")


def test_store_list_all_returns_sorted_by_mtime(tmp_path):
    import time
    from odooctl.operations.models import Operation, OperationKind
    from odooctl.operations.store import OperationStore
    store = OperationStore(tmp_path)
    op1 = Operation.create(OperationKind.BACKUP, "demo", "staging", "cli", {})
    op2 = Operation.create(OperationKind.DEPLOY, "demo", "production", "cli", {})
    store.save(op1)
    time.sleep(0.01)
    store.save(op2)
    ops = store.list_all()
    assert len(ops) == 2
    assert ops[0].id == op2.id  # most recent first


def test_store_update_status_persists(tmp_path):
    from odooctl.operations.models import Operation, OperationKind, OperationStatus
    from odooctl.operations.store import OperationStore
    store = OperationStore(tmp_path)
    op = Operation.create(OperationKind.BACKUP, "demo", "staging", "cli", {})
    store.save(op)
    updated = store.update_status(op.id, OperationStatus.RUNNING)
    assert updated.status == OperationStatus.RUNNING
    loaded = store.load(op.id)
    assert loaded.status == OperationStatus.RUNNING


def test_store_update_status_records_error(tmp_path):
    from odooctl.operations.models import Operation, OperationKind, OperationStatus
    from odooctl.operations.store import OperationStore
    store = OperationStore(tmp_path)
    op = Operation.create(OperationKind.BACKUP, "demo", "staging", "cli", {})
    store.save(op)
    store.update_status(op.id, OperationStatus.FAILED, error="pg connection refused")
    loaded = store.load(op.id)
    assert loaded.status == OperationStatus.FAILED
    assert loaded.error == "pg connection refused"


def test_store_append_and_load_events(tmp_path):
    from odooctl.operations.models import Event, Operation, OperationKind
    from odooctl.operations.store import OperationStore
    store = OperationStore(tmp_path)
    op = Operation.create(OperationKind.BACKUP, "demo", "staging", "cli", {})
    store.save(op)
    ev1 = Event(op_id=op.id, seq=0, timestamp="2026-01-01T00:00:00+00:00",
                level="info", phase="start", message="beginning backup")
    ev2 = Event(op_id=op.id, seq=1, timestamp="2026-01-01T00:00:01+00:00",
                level="info", phase="dump", message="dumping database")
    store.append_event(op.id, ev1)
    store.append_event(op.id, ev2)
    events = store.load_events(op.id)
    assert len(events) == 2
    assert events[0].message == "beginning backup"
    assert events[1].message == "dumping database"


def test_store_load_events_empty_when_no_events(tmp_path):
    from odooctl.operations.models import Operation, OperationKind
    from odooctl.operations.store import OperationStore
    store = OperationStore(tmp_path)
    op = Operation.create(OperationKind.BACKUP, "demo", "staging", "cli", {})
    store.save(op)
    assert store.load_events(op.id) == []


# ---- AuditStore ----

def test_audit_append_and_load(tmp_path):
    from odooctl.operations.audit import AuditStore
    from odooctl.operations.models import AuditEntry
    audit = AuditStore(tmp_path)
    entry = AuditEntry(actor="cli", action="backup", target="production",
                       params_redacted={}, outcome="succeeded",
                       op_id="op1", timestamp="2026-01-01T00:00:00+00:00")
    audit.append(entry)
    chain = audit.load_chain()
    assert len(chain) == 1
    assert chain[0].action == "backup"
    assert chain[0].current_hash != ""
    assert chain[0].prev_hash == ""


def test_audit_chain_links_entries(tmp_path):
    from odooctl.operations.audit import AuditStore
    from odooctl.operations.models import AuditEntry
    audit = AuditStore(tmp_path)
    for i in range(3):
        entry = AuditEntry(actor="cli", action=f"action_{i}", target="staging",
                           params_redacted={}, outcome="succeeded",
                           op_id=f"op{i}", timestamp="2026-01-01T00:00:00+00:00")
        audit.append(entry)
    chain = audit.load_chain()
    assert chain[1].prev_hash == chain[0].current_hash
    assert chain[2].prev_hash == chain[1].current_hash


def test_audit_verify_chain_clean(tmp_path):
    from odooctl.operations.audit import AuditStore, verify_chain
    from odooctl.operations.models import AuditEntry
    audit = AuditStore(tmp_path)
    for i in range(3):
        entry = AuditEntry(actor="cli", action=f"action_{i}", target="staging",
                           params_redacted={}, outcome="succeeded",
                           op_id=f"op{i}", timestamp="2026-01-01T00:00:00+00:00")
        audit.append(entry)
    chain = audit.load_chain()
    assert verify_chain(chain) is True


def test_audit_verify_chain_empty(tmp_path):
    from odooctl.operations.audit import verify_chain
    assert verify_chain([]) is True


def test_audit_tamper_detection_modifies_action(tmp_path):
    from odooctl.operations.audit import AuditStore, verify_chain
    from odooctl.operations.models import AuditEntry
    audit = AuditStore(tmp_path)
    for i in range(2):
        entry = AuditEntry(actor="cli", action=f"action_{i}", target="staging",
                           params_redacted={}, outcome="succeeded",
                           op_id=f"op{i}", timestamp="2026-01-01T00:00:00+00:00")
        audit.append(entry)
    # Tamper with first entry
    lines = audit.path.read_text().splitlines()
    first = json.loads(lines[0])
    first["action"] = "TAMPERED"
    lines[0] = json.dumps(first)
    audit.path.write_text("\n".join(lines) + "\n")
    chain = audit.load_chain()
    assert verify_chain(chain) is False


def test_audit_tamper_detection_modifies_outcome(tmp_path):
    from odooctl.operations.audit import AuditStore, verify_chain
    from odooctl.operations.models import AuditEntry
    audit = AuditStore(tmp_path)
    entry = AuditEntry(actor="cli", action="deploy", target="production",
                       params_redacted={}, outcome="failed",
                       op_id="op1", timestamp="2026-01-01T00:00:00+00:00")
    audit.append(entry)
    lines = audit.path.read_text().splitlines()
    first = json.loads(lines[0])
    first["outcome"] = "succeeded"  # change failed → succeeded
    lines[0] = json.dumps(first)
    audit.path.write_text("\n".join(lines) + "\n")
    chain = audit.load_chain()
    assert verify_chain(chain) is False


def test_audit_append_concurrent_preserves_chain_integrity(tmp_path):
    """Concurrent threads appending to the same AuditStore must produce a valid linear chain."""
    from odooctl.operations.audit import AuditStore, verify_chain
    from odooctl.operations.models import AuditEntry

    audit = AuditStore(tmp_path)
    n_threads = 10
    barrier = threading.Barrier(n_threads)

    def append_entry(i):
        barrier.wait()  # release all threads simultaneously to maximise race window
        entry = AuditEntry(
            actor="cli", action=f"action_{i}", target="staging",
            params_redacted={}, outcome="succeeded",
            op_id=f"op{i}", timestamp="2026-01-01T00:00:00+00:00",
        )
        audit.append(entry)

    threads = [threading.Thread(target=append_entry, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    chain = audit.load_chain()
    assert len(chain) == n_threads
    # Every current_hash must be unique — forked chains produce duplicates
    assert len({e.current_hash for e in chain}) == n_threads
    # The chain must verify cleanly (no forked prev_hash values)
    assert verify_chain(chain) is True


def test_audit_tamper_detection_modifies_stored_prev_hash(tmp_path):
    """Changing only the stored prev_hash field must be detected by verify_chain."""
    from odooctl.operations.audit import AuditStore, verify_chain
    from odooctl.operations.models import AuditEntry

    audit = AuditStore(tmp_path)
    for i in range(3):
        entry = AuditEntry(
            actor="cli", action=f"action_{i}", target="staging",
            params_redacted={}, outcome="succeeded",
            op_id=f"op{i}", timestamp="2026-01-01T00:00:00+00:00",
        )
        audit.append(entry)

    lines = audit.path.read_text().splitlines()
    second = json.loads(lines[1])
    second["prev_hash"] = "0" * 64  # plausible but wrong hash
    lines[1] = json.dumps(second)
    audit.path.write_text("\n".join(lines) + "\n")

    chain = audit.load_chain()
    assert verify_chain(chain) is False


# ---- EnvironmentLock ----

def test_lock_acquire_and_release_creates_and_removes_file(tmp_path):
    from odooctl.operations.locks import EnvironmentLock
    lock = EnvironmentLock("staging", tmp_path, "op1")
    with lock:
        assert (tmp_path / "locks" / "staging.lock").exists()
    assert not (tmp_path / "locks" / "staging.lock").exists()


def test_lock_file_contains_pid_and_op_id(tmp_path):
    import os
    from odooctl.operations.locks import EnvironmentLock
    lock = EnvironmentLock("staging", tmp_path, "myop123")
    with lock:
        data = json.loads((tmp_path / "locks" / "staging.lock").read_text())
        assert data["pid"] == os.getpid()
        assert data["op_id"] == "myop123"


def test_lock_conflict_raises_lock_acquisition_error(tmp_path):
    from odooctl.operations.locks import EnvironmentLock, LockAcquisitionError
    acquired = threading.Event()
    release = threading.Event()

    def holder():
        with EnvironmentLock("staging", tmp_path, "op1"):
            acquired.set()
            release.wait()

    t = threading.Thread(target=holder)
    t.start()
    acquired.wait()

    with pytest.raises(LockAcquisitionError):
        with EnvironmentLock("staging", tmp_path, "op2"):
            pass

    release.set()
    t.join()


def test_lock_stale_lock_is_cleared_and_reacquired(tmp_path):
    from odooctl.operations.locks import EnvironmentLock
    lock_path = tmp_path / "locks" / "staging.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Write a stale lock with a PID that cannot exist
    lock_path.write_text(json.dumps({"pid": 99999999, "op_id": "dead_op"}))

    lock = EnvironmentLock("staging", tmp_path, "new_op")
    with lock:
        assert lock_path.exists()
        data = json.loads(lock_path.read_text())
        assert data["op_id"] == "new_op"
    assert not lock_path.exists()


def test_different_environments_do_not_conflict(tmp_path):
    from odooctl.operations.locks import EnvironmentLock
    acquired1 = threading.Event()
    acquired2 = threading.Event()
    done = threading.Event()

    def env1():
        with EnvironmentLock("production", tmp_path, "op1"):
            acquired1.set()
            done.wait()

    def env2():
        acquired1.wait()
        with EnvironmentLock("staging", tmp_path, "op2"):
            acquired2.set()
            done.wait()

    t1 = threading.Thread(target=env1)
    t2 = threading.Thread(target=env2)
    t1.start()
    t2.start()
    acquired2.wait()  # Both acquired successfully
    done.set()
    t1.join()
    t2.join()


# ---- Engine ----

def test_engine_creates_running_operation(tmp_path):
    from odooctl.operations.audit import AuditStore
    from odooctl.operations.engine import run_operation
    from odooctl.operations.models import OperationKind, OperationStatus
    from odooctl.operations.store import OperationStore
    store = OperationStore(tmp_path)
    audit = AuditStore(tmp_path)
    with run_operation(
        store, audit,
        kind=OperationKind.BACKUP, project="demo", environment="staging",
        actor="cli", params_redacted={}, state_dir=tmp_path,
    ) as op_ctx:
        assert op_ctx.op.status == OperationStatus.RUNNING
        loaded = store.load(op_ctx.op.id)
        assert loaded.status == OperationStatus.RUNNING


def test_engine_marks_succeeded_on_clean_exit(tmp_path):
    from odooctl.operations.audit import AuditStore
    from odooctl.operations.engine import run_operation
    from odooctl.operations.models import OperationKind, OperationStatus
    from odooctl.operations.store import OperationStore
    store = OperationStore(tmp_path)
    audit = AuditStore(tmp_path)
    op_id = None
    with run_operation(
        store, audit,
        kind=OperationKind.BACKUP, project="demo", environment="staging",
        actor="cli", params_redacted={}, state_dir=tmp_path,
    ) as op_ctx:
        op_id = op_ctx.op.id
    op = store.load(op_id)
    assert op.status == OperationStatus.SUCCEEDED


def test_engine_marks_failed_on_exception(tmp_path):
    from odooctl.operations.audit import AuditStore
    from odooctl.operations.engine import run_operation
    from odooctl.operations.models import OperationKind, OperationStatus
    from odooctl.operations.store import OperationStore
    store = OperationStore(tmp_path)
    audit = AuditStore(tmp_path)
    op_id = None
    with pytest.raises(RuntimeError, match="boom"):
        with run_operation(
            store, audit,
            kind=OperationKind.RESTORE, project="demo", environment="staging",
            actor="cli", params_redacted={}, state_dir=tmp_path,
        ) as op_ctx:
            op_id = op_ctx.op.id
            raise RuntimeError("boom")
    op = store.load(op_id)
    assert op.status == OperationStatus.FAILED
    assert "boom" in (op.error or "")


def test_engine_records_error_message(tmp_path):
    from odooctl.operations.audit import AuditStore
    from odooctl.operations.engine import run_operation
    from odooctl.operations.models import OperationKind
    from odooctl.operations.store import OperationStore
    store = OperationStore(tmp_path)
    audit = AuditStore(tmp_path)
    op_id = None
    with pytest.raises(ValueError):
        with run_operation(
            store, audit,
            kind=OperationKind.DEPLOY, project="demo", environment="production",
            actor="cli", params_redacted={}, state_dir=tmp_path,
        ) as op_ctx:
            op_id = op_ctx.op.id
            raise ValueError("missing env var")
    op = store.load(op_id)
    assert "missing env var" in (op.error or "")


def test_engine_emit_appends_events(tmp_path):
    from odooctl.operations.audit import AuditStore
    from odooctl.operations.engine import run_operation
    from odooctl.operations.models import OperationKind
    from odooctl.operations.store import OperationStore
    store = OperationStore(tmp_path)
    audit = AuditStore(tmp_path)
    op_id = None
    with run_operation(
        store, audit,
        kind=OperationKind.BACKUP, project="demo", environment="staging",
        actor="cli", params_redacted={}, state_dir=tmp_path,
    ) as op_ctx:
        op_id = op_ctx.op.id
        op_ctx.emit("dumping database", phase="dump")
        op_ctx.emit("archiving filestore", phase="archive")
    events = store.load_events(op_id)
    messages = [e.message for e in events]
    assert any("dumping database" in m for m in messages)
    assert any("archiving filestore" in m for m in messages)


def test_engine_releases_lock_after_success(tmp_path):
    from odooctl.operations.audit import AuditStore
    from odooctl.operations.engine import run_operation
    from odooctl.operations.locks import EnvironmentLock
    from odooctl.operations.models import OperationKind
    from odooctl.operations.store import OperationStore
    store = OperationStore(tmp_path)
    audit = AuditStore(tmp_path)
    with run_operation(
        store, audit,
        kind=OperationKind.BACKUP, project="demo", environment="staging",
        actor="cli", params_redacted={}, state_dir=tmp_path,
    ):
        pass
    # Lock should be released — can acquire again
    with EnvironmentLock("staging", tmp_path, "after"):
        pass


def test_engine_releases_lock_after_failure(tmp_path):
    from odooctl.operations.audit import AuditStore
    from odooctl.operations.engine import run_operation
    from odooctl.operations.locks import EnvironmentLock
    from odooctl.operations.models import OperationKind
    from odooctl.operations.store import OperationStore
    store = OperationStore(tmp_path)
    audit = AuditStore(tmp_path)
    with pytest.raises(RuntimeError):
        with run_operation(
            store, audit,
            kind=OperationKind.BACKUP, project="demo", environment="staging",
            actor="cli", params_redacted={}, state_dir=tmp_path,
        ):
            raise RuntimeError("failed")
    # Lock should be released
    with EnvironmentLock("staging", tmp_path, "after"):
        pass


def test_engine_concurrent_conflict_raises(tmp_path):
    from odooctl.operations.audit import AuditStore
    from odooctl.operations.engine import run_operation
    from odooctl.operations.locks import LockAcquisitionError
    from odooctl.operations.models import OperationKind
    from odooctl.operations.store import OperationStore
    store = OperationStore(tmp_path)
    audit = AuditStore(tmp_path)

    entered = threading.Event()
    release = threading.Event()

    def first_op():
        with run_operation(
            store, audit,
            kind=OperationKind.RESTORE, project="demo", environment="staging",
            actor="cli", params_redacted={}, state_dir=tmp_path,
        ):
            entered.set()
            release.wait()

    t = threading.Thread(target=first_op)
    t.start()
    entered.wait()

    with pytest.raises(LockAcquisitionError):
        with run_operation(
            store, audit,
            kind=OperationKind.RESTORE, project="demo", environment="staging",
            actor="cli", params_redacted={}, state_dir=tmp_path,
        ):
            pass

    release.set()
    t.join()


def test_engine_appends_audit_entry_on_success(tmp_path):
    from odooctl.operations.audit import AuditStore, verify_chain
    from odooctl.operations.engine import run_operation
    from odooctl.operations.models import OperationKind
    from odooctl.operations.store import OperationStore
    store = OperationStore(tmp_path)
    audit = AuditStore(tmp_path)
    with run_operation(
        store, audit,
        kind=OperationKind.BACKUP, project="demo", environment="staging",
        actor="cli", params_redacted={}, state_dir=tmp_path,
    ):
        pass
    chain = audit.load_chain()
    assert len(chain) == 1
    assert chain[0].action == "backup"
    assert chain[0].outcome == "succeeded"
    assert verify_chain(chain) is True


def test_engine_appends_audit_entry_on_failure(tmp_path):
    from odooctl.operations.audit import AuditStore, verify_chain
    from odooctl.operations.engine import run_operation
    from odooctl.operations.models import OperationKind
    from odooctl.operations.store import OperationStore
    store = OperationStore(tmp_path)
    audit = AuditStore(tmp_path)
    with pytest.raises(RuntimeError):
        with run_operation(
            store, audit,
            kind=OperationKind.DEPLOY, project="demo", environment="production",
            actor="cli", params_redacted={}, state_dir=tmp_path,
        ):
            raise RuntimeError("deploy error")
    chain = audit.load_chain()
    assert len(chain) == 1
    assert chain[0].outcome == "failed"
    assert verify_chain(chain) is True


def test_engine_audit_chain_across_multiple_operations(tmp_path):
    from odooctl.operations.audit import AuditStore, verify_chain
    from odooctl.operations.engine import run_operation
    from odooctl.operations.models import OperationKind
    from odooctl.operations.store import OperationStore
    store = OperationStore(tmp_path)
    audit = AuditStore(tmp_path)
    for kind in [OperationKind.BACKUP, OperationKind.RESTORE, OperationKind.DEPLOY]:
        try:
            with run_operation(
                store, audit,
                kind=kind, project="demo", environment="staging",
                actor="cli", params_redacted={}, state_dir=tmp_path,
            ):
                if kind == OperationKind.RESTORE:
                    raise RuntimeError("test failure")
        except RuntimeError:
            pass
    chain = audit.load_chain()
    assert len(chain) == 3
    assert verify_chain(chain) is True
    assert chain[0].action == "backup"
    assert chain[1].action == "restore"
    assert chain[1].outcome == "failed"
    assert chain[2].action == "deploy"


# ---- Command-level operation integration ----

def test_backup_command_creates_operation_record(tmp_path, monkeypatch):
    from odooctl.commands import backup as backup_cmd
    from odooctl.operations.store import OperationStore
    from odooctl.services import backup as backup_svc

    config = tmp_path / "odooctl.yml"
    config.write_text(MINIMAL_CONFIG)

    class FakePg:
        def __init__(self, cfg): pass
        def dump(self, db, path): Path(path).write_bytes(b"dump")

    class FakeFs:
        def archive(self, src, dst): Path(dst).write_bytes(b"tar")

    class FakeMeta:
        def __init__(self, root): pass
        def save_backup_manifest(self, backup_id, manifest): pass

    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")
    monkeypatch.setattr(backup_svc, "PostgresAdapter", FakePg)
    monkeypatch.setattr(backup_svc, "FilestoreAdapter", FakeFs)
    monkeypatch.setattr(backup_svc, "MetadataStore", FakeMeta)
    monkeypatch.setattr(backup_svc, "git_commit", lambda cwd=None: "abc123")

    backup_id = backup_cmd.execute("production", str(config))
    assert backup_id.startswith("production_")

    store = OperationStore(tmp_path / ".odooctl")
    ops = store.list_all()
    assert len(ops) == 1
    assert ops[0].kind.value == "backup"
    assert ops[0].environment == "production"
    assert ops[0].status.value == "succeeded"


def test_backup_command_fails_operation_when_verification_fails(tmp_path, monkeypatch):
    from odooctl.commands import backup as backup_cmd
    from odooctl.operations.store import OperationStore
    from odooctl.services import backup as backup_svc
    from odooctl.services.models import BackupResult

    config = tmp_path / "odooctl.yml"
    config.write_text(MINIMAL_CONFIG)
    monkeypatch.setattr(
        backup_cmd,
        "run_backup",
        lambda ctx, environment: BackupResult(backup_id="production_test"),
    )
    monkeypatch.setattr(
        backup_svc,
        "verify_backup",
        lambda *args, **kwargs: backup_svc.BackupVerifyResult(
            ok=False,
            backup_id="production_test",
            error="checksum mismatch",
        ),
    )

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        backup_cmd.execute("production", str(config), verify=True)

    [operation] = OperationStore(tmp_path / ".odooctl").list_all()
    assert operation.status.value == "failed"


def test_deploy_command_creates_operation_record_on_failure(tmp_path, monkeypatch):
    from odooctl.commands import deploy as deploy_cmd
    from odooctl.operations.store import OperationStore
    from odooctl.services import deploy as deploy_svc

    config = tmp_path / "odooctl.yml"
    config.write_text(MINIMAL_CONFIG)
    # Missing docker-compose.yml → deploy fails at preflight

    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")
    monkeypatch.setattr(deploy_svc, "PostgresAdapter", lambda cfg: None)

    with pytest.raises(Exception):
        deploy_cmd.execute("production", "main", str(config))

    store = OperationStore(tmp_path / ".odooctl")
    ops = store.list_all()
    assert len(ops) == 1
    assert ops[0].kind.value == "deploy"
    assert ops[0].status.value == "failed"


def test_engine_lock_acquisition_failure_leaves_audit_trail(tmp_path):
    """When lock acquisition fails, run_operation must record FAILED status,
    emit an error event, and append a failed audit entry."""
    from odooctl.operations.audit import AuditStore
    from odooctl.operations.engine import run_operation
    from odooctl.operations.locks import LockAcquisitionError
    from odooctl.operations.models import OperationKind, OperationStatus
    from odooctl.operations.store import OperationStore

    store = OperationStore(tmp_path)
    audit = AuditStore(tmp_path)

    entered = threading.Event()
    release = threading.Event()

    def first_op():
        with run_operation(
            store, audit,
            kind=OperationKind.BACKUP, project="demo", environment="staging",
            actor="cli", params_redacted={}, state_dir=tmp_path,
        ):
            entered.set()
            release.wait()

    t = threading.Thread(target=first_op)
    t.start()
    entered.wait()

    with pytest.raises(LockAcquisitionError):
        with run_operation(
            store, audit,
            kind=OperationKind.BACKUP, project="demo", environment="staging",
            actor="cli", params_redacted={}, state_dir=tmp_path,
        ):
            pass

    release.set()
    t.join()

    ops = store.list_all()
    failed_ops = [op for op in ops if op.status == OperationStatus.FAILED]
    assert len(failed_ops) == 1
    assert failed_ops[0].kind == OperationKind.BACKUP

    events = store.load_events(failed_ops[0].id)
    assert any(e.level == "error" or e.phase == "end" for e in events)

    chain = audit.load_chain()
    failed_audits = [e for e in chain if e.outcome == "failed"]
    assert len(failed_audits) >= 1


# ---- HMAC-keyed audit chain (F13) ----

AUDIT_TEST_KEY = "audit-chain-hmac-key-0123456789abcdef"


def _audit_entry(i=0):
    from odooctl.operations.models import AuditEntry

    return AuditEntry(actor="cli", action=f"action_{i}", target="staging",
                      params_redacted={}, outcome="succeeded",
                      op_id=f"op{i}", timestamp="2026-01-01T00:00:00+00:00")


def test_audit_keyed_chain_roundtrip(tmp_path):
    from odooctl.operations.audit import AuditStore, verify_chain

    audit = AuditStore(tmp_path, key=AUDIT_TEST_KEY)
    for i in range(3):
        audit.append(_audit_entry(i))
    chain = audit.load_chain()
    assert verify_chain(chain, key=AUDIT_TEST_KEY) is True
    # without the key (or with the wrong key) the keyed chain does not verify
    assert verify_chain(chain) is False
    assert verify_chain(chain, key="wrong-key-0123456789abcdefwrongkey") is False


def test_audit_keyed_chain_via_env_var(tmp_path, monkeypatch):
    from odooctl.operations.audit import AuditStore, verify_chain

    monkeypatch.setenv("ODOOCTL_AUDIT_KEY", AUDIT_TEST_KEY)
    audit = AuditStore(tmp_path)  # picks the key up from the environment
    for i in range(2):
        audit.append(_audit_entry(i))
    chain = audit.load_chain()
    assert verify_chain(chain) is True  # env key applies to verification too

    monkeypatch.delenv("ODOOCTL_AUDIT_KEY")
    assert verify_chain(chain) is False  # unkeyed verify rejects keyed chain


def test_audit_unkeyed_default_unchanged(tmp_path, monkeypatch):
    """Compatibility: without ODOOCTL_AUDIT_KEY the chain stays plain SHA-256."""
    from odooctl.operations.audit import AuditStore, _hash_entry, verify_chain

    monkeypatch.delenv("ODOOCTL_AUDIT_KEY", raising=False)
    audit = AuditStore(tmp_path)
    entry = audit.append(_audit_entry(0))
    assert entry.current_hash == _hash_entry(entry.to_dict(), "")
    assert verify_chain(audit.load_chain()) is True


def test_audit_keyed_chain_detects_truncate_and_rehash(tmp_path):
    """F13 attack: truncate the file and rehash without the key.

    Against an unkeyed chain this forgery verifies; with ODOOCTL_AUDIT_KEY set
    the attacker cannot recompute valid HMAC links.
    """
    import json as _json

    from odooctl.operations.audit import AuditStore, _hash_entry, verify_chain

    audit = AuditStore(tmp_path, key=AUDIT_TEST_KEY)
    for i in range(3):
        audit.append(_audit_entry(i))

    # Attacker (no key): drop the middle entry, then rewrite the whole file
    # with recomputed unkeyed hashes so the chain "links up" again.
    lines = [ln for ln in audit.path.read_text().splitlines() if ln.strip()]
    kept = [_json.loads(lines[0]), _json.loads(lines[2])]
    prev_hash = ""
    forged_lines = []
    for record in kept:
        record["prev_hash"] = prev_hash
        body = {k: v for k, v in record.items() if k != "current_hash"}
        record["current_hash"] = _hash_entry(body, prev_hash)  # unkeyed rehash
        prev_hash = record["current_hash"]
        forged_lines.append(_json.dumps(record))
    audit.path.write_text("\n".join(forged_lines) + "\n")

    chain = audit.load_chain()
    assert len(chain) == 2
    # The forgery is self-consistent under the unkeyed scheme...
    assert verify_chain(chain) is True
    # ...but fails once verification uses the HMAC key.
    assert verify_chain(chain, key=AUDIT_TEST_KEY) is False


# --- Re-scan H2: audit chain truncation detection ---


def test_audit_verify_detects_tail_truncation(tmp_path):
    from odooctl.operations.audit import AuditStore
    from odooctl.operations.models import AuditEntry

    store = AuditStore(tmp_path)
    for i in range(3):
        store.append(AuditEntry(actor="u", action="backup", target="prod", params_redacted={}, outcome="succeeded", op_id=f"op{i}", timestamp="2026-07-19T00:00:00Z"))
    assert store.verify() is True

    # Truncate the newest entry: the remaining prefix is still a valid chain,
    # but the high-water mark records 3 entries.
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    (tmp_path / "audit.jsonl").write_text("\n".join(lines[:-1]) + "\n")

    from odooctl.operations.audit import verify_chain
    assert verify_chain(store.load_chain()) is True  # prefix looks intact
    assert store.verify() is False  # HWM catches the truncation


def test_audit_verify_detects_whole_file_deletion(tmp_path):
    from odooctl.operations.audit import AuditStore
    from odooctl.operations.models import AuditEntry

    store = AuditStore(tmp_path)
    store.append(AuditEntry(actor="u", action="backup", target="prod", params_redacted={}, outcome="succeeded", op_id="op0", timestamp="2026-07-19T00:00:00Z"))
    (tmp_path / "audit.jsonl").write_text("")  # attacker empties the log
    assert store.verify() is False


def test_audit_verify_keyed_hwm_resists_forged_high_water_mark(tmp_path, monkeypatch):
    monkeypatch.setenv("ODOOCTL_AUDIT_KEY", "audit-key-0123456789abcdef0123456789")
    from odooctl.operations.audit import AuditStore
    from odooctl.operations.models import AuditEntry

    store = AuditStore(tmp_path)
    for i in range(3):
        store.append(AuditEntry(actor="u", action="backup", target="prod", params_redacted={}, outcome="succeeded", op_id=f"op{i}", timestamp="2026-07-19T00:00:00Z"))
    # Attacker truncates the log AND forges the HWM to count=2 without the key.
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    (tmp_path / "audit.jsonl").write_text("\n".join(lines[:-1]) + "\n")
    import json as _json
    (tmp_path / "audit.hwm").write_text(_json.dumps({"count": 2, "last_hash": "x", "mac": "forged"}))
    assert store.verify() is False
