"""M14 DR drill tests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _make_backup(
    backups_root: Path,
    environment: str,
    project: str = "dr-test",
    timestamp: str = "2026-05-31_100000",
) -> Path:
    ts = timestamp
    backup_id = f"{environment}_{ts}"
    d = backups_root / backup_id
    d.mkdir(parents=True)
    (d / "db.dump").write_bytes(b"dbdata")
    (d / "filestore.tar").write_bytes(b"fsdata")
    db_hash = hashlib.sha256(b"dbdata").hexdigest()
    fs_hash = hashlib.sha256(b"fsdata").hexdigest()
    manifest = {
        "backup_id": backup_id,
        "project": project,
        "environment": environment,
        "timestamp": ts,
        "db_name": f"{environment}_db",
        "odoo_version": "19.0",
        "backup_mode": "full",
        "checksums": {"db_dump": db_hash, "filestore": fs_hash},
    }
    (d / "manifest.json").write_text(json.dumps(manifest))
    return d


def _runtime_callbacks(events: list | None = None):
    events = events if events is not None else []

    def prepare(target):
        events.append(("prepare", target))

    def restore_database(target, dump_path):
        events.append(("restore-db", target, Path(dump_path).name))

    def restore_filestore(target, archive_path):
        events.append(("restore-filestore", target, Path(archive_path).name))

    def start(target):
        events.append(("start", target))
        return f"http://127.0.0.1:49152/web/health?db={target.database}"

    def stop(target):
        events.append(("stop", target))

    return {
        "prepare_runtime_fn": prepare,
        "restore_database_fn": restore_database,
        "restore_filestore_fn": restore_filestore,
        "start_runtime_fn": start,
        "stop_runtime_fn": stop,
    }


# ---------------------------------------------------------------------------
# DrDrillResult shape
# ---------------------------------------------------------------------------

def test_dr_drill_result_fields():
    from odooctl.services.dr import DrDrillResult
    r = DrDrillResult(
        status="success",
        environment="production",
        backup_id="production_2026-05-31_100000",
        message=None,
    )
    assert r.status == "success"
    assert r.environment == "production"
    assert r.backup_id is not None


# ---------------------------------------------------------------------------
# Protected environment check
# ---------------------------------------------------------------------------

def test_dr_drill_allows_protected_source_environment(tmp_path):
    """Protected environments (e.g. production) are valid DR drill SOURCES."""
    from odooctl.services.dr import run_dr_drill

    backups_root = tmp_path / "backups"
    backups_root.mkdir()
    _make_backup(backups_root, "production")

    result = run_dr_drill(
        environment="production",
        expected_project="dr-test",
        backups_root=backups_root,
        db_adapter=MagicMock(),
        fs_adapter=MagicMock(),
        healthcheck_fn=lambda url: True,
        is_protected_fn=lambda env: True,  # source is protected — must NOT block the drill
        throwaway_db_suffix="_dr_drill",
        **_runtime_callbacks(),
    )
    assert result.status == "success"


def test_dr_drill_raises_if_throwaway_matches_live_db(tmp_path):
    """Safety guard: throwaway DB name must differ from manifest live DB name."""
    from odooctl.services.dr import run_dr_drill

    backups_root = tmp_path / "backups"
    backups_root.mkdir()
    _make_backup(backups_root, "production")

    with pytest.raises(RuntimeError, match="throwaway"):
        run_dr_drill(
            environment="production",
            expected_project="dr-test",
            backups_root=backups_root,
            db_adapter=MagicMock(),
            fs_adapter=MagicMock(),
            healthcheck_fn=lambda url: True,
            is_protected_fn=lambda env: False,
            throwaway_db_suffix="",  # empty suffix → throwaway_db == live DB name
        )


def test_dr_drill_truncates_max_length_source_name_for_safe_throwaway(tmp_path):
    from odooctl.services.dr import run_dr_drill

    backups_root = tmp_path / "backups"
    backups_root.mkdir()
    backup_dir = _make_backup(backups_root, "production")
    manifest_path = backup_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["db_name"] = "d" * 64
    manifest_path.write_text(json.dumps(manifest))

    result = run_dr_drill(
        environment="production",
        expected_project="dr-test",
        backups_root=backups_root,
        db_adapter=MagicMock(),
        fs_adapter=MagicMock(),
        healthcheck_fn=lambda url: True,
        **_runtime_callbacks(),
    )

    assert result.status == "success"
    assert result.database is not None
    assert len(result.database) == 64
    assert result.database.endswith("_dr_drill")


def test_dr_drill_allows_non_protected_env(tmp_path):
    from odooctl.services.dr import run_dr_drill

    backups_root = tmp_path / "backups"
    backups_root.mkdir()
    _make_backup(backups_root, "production")

    mock_db = MagicMock()
    mock_fs = MagicMock()

    # should not raise
    result = run_dr_drill(
        environment="production",
        expected_project="dr-test",
        backups_root=backups_root,
        db_adapter=mock_db,
        fs_adapter=mock_fs,
        healthcheck_fn=lambda url: True,
        is_protected_fn=lambda env: False,  # nothing protected
        throwaway_db_suffix="_dr_drill",
        **_runtime_callbacks(),
    )
    assert result is not None


# ---------------------------------------------------------------------------
# Throwaway DB — restoration and cleanup
# ---------------------------------------------------------------------------

def test_dr_drill_restores_to_throwaway_db(tmp_path):
    from odooctl.services.dr import run_dr_drill

    backups_root = tmp_path / "backups"
    backups_root.mkdir()
    _make_backup(backups_root, "production")

    live_db = MagicMock()
    live_fs = MagicMock()
    events = []

    result = run_dr_drill(
        environment="production",
        expected_project="dr-test",
        backups_root=backups_root,
        db_adapter=live_db,
        fs_adapter=live_fs,
        healthcheck_fn=lambda url: True,
        is_protected_fn=lambda env: False,
        throwaway_db_suffix="_dr_drill",
        **_runtime_callbacks(events),
    )

    restore_event = next(event for event in events if event[0] == "restore-db")
    assert restore_event[1].database == "production_db_dr_drill"
    assert restore_event[2] == "db.dump"
    assert result.database == "production_db_dr_drill"
    # Regression: compatibility arguments can never route a drill to live adapters.
    assert live_db.mock_calls == []
    assert live_fs.mock_calls == []


def test_dr_drill_destroys_isolated_environment_after_success(tmp_path):
    from odooctl.services.dr import run_dr_drill

    backups_root = tmp_path / "backups"
    backups_root.mkdir()
    _make_backup(backups_root, "production")

    events = []

    run_dr_drill(
        environment="production",
        expected_project="dr-test",
        backups_root=backups_root,
        healthcheck_fn=lambda url: True,
        is_protected_fn=lambda env: False,
        throwaway_db_suffix="_dr_drill",
        **_runtime_callbacks(events),
    )

    assert [event[0] for event in events][-1] == "stop"


def test_dr_drill_destroys_isolated_environment_on_healthcheck_failure(tmp_path):
    from odooctl.services.dr import run_dr_drill

    backups_root = tmp_path / "backups"
    backups_root.mkdir()
    _make_backup(backups_root, "production")

    events = []

    result = run_dr_drill(
        environment="production",
        expected_project="dr-test",
        backups_root=backups_root,
        healthcheck_fn=lambda url: False,
        is_protected_fn=lambda env: False,
        throwaway_db_suffix="_dr_drill",
        **_runtime_callbacks(events),
    )

    assert result.status == "failed"
    assert [event[0] for event in events][-1] == "stop"


def test_dr_drill_destroys_isolated_environment_on_restore_exception(tmp_path):
    from odooctl.services.dr import run_dr_drill

    backups_root = tmp_path / "backups"
    backups_root.mkdir()
    _make_backup(backups_root, "production")

    events = []
    callbacks = _runtime_callbacks(events)

    def fail_restore(target, dump_path):
        events.append(("restore-db", target, Path(dump_path).name))
        raise RuntimeError("restore failed")

    callbacks["restore_database_fn"] = fail_restore

    result = run_dr_drill(
        environment="production",
        expected_project="dr-test",
        backups_root=backups_root,
        healthcheck_fn=lambda url: True,
        is_protected_fn=lambda env: False,
        throwaway_db_suffix="_dr_drill",
        **callbacks,
    )

    assert result.status == "failed"
    assert "restore failed" in (result.message or "")
    assert [event[0] for event in events] == ["prepare", "restore-db", "stop"]


# ---------------------------------------------------------------------------
# Result fields
# ---------------------------------------------------------------------------

def test_dr_drill_success_result(tmp_path):
    from odooctl.services.dr import run_dr_drill

    backups_root = tmp_path / "backups"
    backups_root.mkdir()
    _make_backup(backups_root, "production")

    result = run_dr_drill(
        environment="production",
        expected_project="dr-test",
        backups_root=backups_root,
        db_adapter=MagicMock(),
        fs_adapter=MagicMock(),
        healthcheck_fn=lambda url: True,
        is_protected_fn=lambda env: False,
        throwaway_db_suffix="_dr_drill",
        **_runtime_callbacks(),
    )

    assert result.status == "success"
    assert result.backup_id is not None
    assert result.environment == "production"


def test_dr_drill_no_backup_raises(tmp_path):
    from odooctl.services.dr import run_dr_drill

    backups_root = tmp_path / "backups"
    backups_root.mkdir()

    with pytest.raises(RuntimeError, match="No backups found"):
        run_dr_drill(
            environment="production",
            expected_project="dr-test",
            backups_root=backups_root,
            db_adapter=MagicMock(),
            fs_adapter=MagicMock(),
            healthcheck_fn=lambda url: True,
            is_protected_fn=lambda env: False,
        )


def test_dr_drill_skips_newer_backup_owned_by_another_project(tmp_path):
    from odooctl.services.dr import run_dr_drill

    backups_root = tmp_path / "shared-backups"
    backups_root.mkdir()
    owned = _make_backup(
        backups_root,
        "production",
        project="dr-test",
        timestamp="2026-05-31_100000",
    )
    foreign = _make_backup(
        backups_root,
        "production",
        project="another-project",
        timestamp="2026-05-31_110000",
    )
    restored: list[str] = []
    callbacks = _runtime_callbacks()
    callbacks["restore_database_fn"] = (
        lambda target, path: restored.append(Path(path).parent.name)
    )

    result = run_dr_drill(
        environment="production",
        expected_project="dr-test",
        backups_root=backups_root,
        healthcheck_fn=lambda url: True,
        **callbacks,
    )

    assert result.status == "success"
    assert result.backup_id == owned.name
    assert restored == [owned.name]
    assert result.backup_id != foreign.name


def test_dr_drill_validates_selected_backup_environment_identity(tmp_path):
    from odooctl.services.dr import run_dr_drill

    backups_root = tmp_path / "shared-backups"
    backups_root.mkdir()
    backup_dir = _make_backup(backups_root, "production", project="dr-test")
    manifest_path = backup_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["environment"] = "staging"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(RuntimeError, match="Backup environment mismatch"):
        run_dr_drill(
            environment="production",
            expected_project="dr-test",
            backups_root=backups_root,
            healthcheck_fn=lambda url: True,
            **_runtime_callbacks(),
        )


def test_dr_drill_requires_expected_project(tmp_path):
    from odooctl.services.dr import run_dr_drill

    with pytest.raises(TypeError, match="expected_project"):
        run_dr_drill(  # type: ignore[call-arg]
            environment="production",
            backups_root=tmp_path,
            healthcheck_fn=lambda url: True,
        )


def test_dr_drill_restores_exact_pair_before_start_and_cleans_in_reverse(tmp_path):
    from odooctl.services.dr import run_dr_drill

    backups_root = tmp_path / "backups"
    backups_root.mkdir()
    _make_backup(backups_root, "production")
    events = []

    def prepare(target):
        events.append(("prepare", target.database, target.filestore_path))

    def restore_database(target, dump):
        events.append(("restore-db", target.database, Path(dump).name))

    def restore_filestore(target, archive):
        events.append(
            ("restore-filestore", target.filestore_path, Path(archive).name)
        )

    def start(target):
        events.append(("start-runtime", target.database, target.filestore_path))
        assert Path(target.filestore_path).name == target.database
        return "http://127.0.0.1:49152/web/health?db=production_db_dr_drill"

    def healthcheck(url):
        events.append(("healthcheck", url))
        return True

    def stop(target):
        events.append(("stop-runtime", target.database))

    result = run_dr_drill(
        environment="production",
        expected_project="dr-test",
        backups_root=backups_root,
        healthcheck_fn=healthcheck,
        prepare_runtime_fn=prepare,
        restore_database_fn=restore_database,
        restore_filestore_fn=restore_filestore,
        start_runtime_fn=start,
        stop_runtime_fn=stop,
    )

    assert result.status == "success"
    assert [event[0] for event in events] == [
        "prepare",
        "restore-db",
        "restore-filestore",
        "start-runtime",
        "healthcheck",
        "stop-runtime",
    ]
    assert result.database == "production_db_dr_drill"
    assert result.filestore_path == (
        "/var/lib/odoo/filestore/production_db_dr_drill"
    )


def test_dr_drill_cleanup_failure_overrides_success(tmp_path):
    from odooctl.services.dr import run_dr_drill

    backups_root = tmp_path / "backups"
    backups_root.mkdir()
    _make_backup(backups_root, "production")
    def stop(target):
        raise RuntimeError("network cleanup broke")

    result = run_dr_drill(
        environment="production",
        expected_project="dr-test",
        backups_root=backups_root,
        healthcheck_fn=lambda url: True,
        prepare_runtime_fn=lambda target: None,
        restore_database_fn=lambda target, path: None,
        restore_filestore_fn=lambda target, path: None,
        start_runtime_fn=lambda target: (
            "http://127.0.0.1:49152/web/health"
        ),
        stop_runtime_fn=stop,
    )

    assert result.status == "failed"
    assert "isolated environment cleanup failed" in (result.message or "")
    assert "network cleanup broke" in (result.message or "")


def test_dr_drill_without_isolated_runtime_fails_before_restore(tmp_path):
    from odooctl.services.dr import run_dr_drill

    backups_root = tmp_path / "backups"
    backups_root.mkdir()
    _make_backup(backups_root, "production")
    db = MagicMock()
    fs = MagicMock()

    result = run_dr_drill(
        environment="production",
        expected_project="dr-test",
        backups_root=backups_root,
        db_adapter=db,
        fs_adapter=fs,
        healthcheck_fn=lambda url: True,
    )

    assert result.status == "failed"
    assert "Complete isolated DR environment callbacks are required" in (
        result.message or ""
    )
    assert db.mock_calls == []
    assert fs.mock_calls == []


def test_dr_drill_start_failure_still_cleans_runtime_and_restored_pair(tmp_path):
    from odooctl.services.dr import run_dr_drill

    backups_root = tmp_path / "backups"
    backups_root.mkdir()
    _make_backup(backups_root, "production")
    stop = MagicMock()
    callbacks = _runtime_callbacks()

    def fail_start(target):
        raise RuntimeError("runtime start failed")

    callbacks["start_runtime_fn"] = fail_start
    callbacks["stop_runtime_fn"] = stop
    result = run_dr_drill(
        environment="production",
        expected_project="dr-test",
        backups_root=backups_root,
        healthcheck_fn=lambda url: True,
        **callbacks,
    )

    assert result.status == "failed"
    assert "runtime start failed" in (result.message or "")
    stop.assert_called_once()


def test_dr_drill_prepare_failure_still_destroys_partial_boundary(tmp_path):
    from odooctl.services.dr import run_dr_drill

    backups_root = tmp_path / "backups"
    backups_root.mkdir()
    _make_backup(backups_root, "production")
    stop = MagicMock()

    def fail_prepare(target):
        raise RuntimeError("postgres container failed to start")

    result = run_dr_drill(
        environment="production",
        expected_project="dr-test",
        backups_root=backups_root,
        healthcheck_fn=lambda url: True,
        prepare_runtime_fn=fail_prepare,
        restore_database_fn=lambda target, path: None,
        restore_filestore_fn=lambda target, path: None,
        start_runtime_fn=lambda target: "http://127.0.0.1:49152/web/health",
        stop_runtime_fn=stop,
    )

    assert result.status == "failed"
    assert "postgres container failed to start" in (result.message or "")
    stop.assert_called_once()


def test_dr_drill_cli_records_locked_durable_operation(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from odooctl.main import app
    from odooctl.operations.models import OperationKind, OperationStatus
    from odooctl.operations.store import OperationStore
    from odooctl.services.dr import DrDrillResult

    config = tmp_path / "odooctl.yml"
    config.write_text(
        """\
project:
  name: dr-test
  odoo_version: "19.0"
runtime:
  compose_file: docker-compose.yml
odoo:
  image: odoo:19.0
environments:
  production:
    branch: main
    domain: odoo.example.com
    db_name: production_db
    filestore_path: filestore/production_db
"""
    )
    drill_calls = []

    def fake_drill(**kwargs):
        drill_calls.append(kwargs)
        return DrDrillResult(
            status="success",
            environment="production",
            backup_id="production_2026-07-30_020000",
            database="production_db_dr_drill",
            filestore_path=str(tmp_path / "filestore" / "production_db_dr_drill"),
        )

    monkeypatch.setattr(
        "odooctl.services.dr.run_dr_drill",
        fake_drill,
    )
    monkeypatch.setattr(
        "odooctl.adapters.db.make_db_adapter",
        lambda *args, **kwargs: pytest.fail("live database adapter was constructed"),
    )
    monkeypatch.setattr(
        "odooctl.adapters.filestore.make_filestore_adapter",
        lambda *args, **kwargs: pytest.fail("live filestore adapter was constructed"),
    )

    result = CliRunner().invoke(
        app,
        ["dr", "drill", "production", "--config", str(config)],
    )

    assert result.exit_code == 0, result.output
    operations = OperationStore(tmp_path / ".odooctl").list_all()
    assert len(operations) == 1
    assert operations[0].kind is OperationKind.DR_DRILL
    assert operations[0].status is OperationStatus.SUCCEEDED
    assert drill_calls[0]["expected_project"] == "dr-test"


def test_dr_drill_cli_persists_failed_result_and_exits_nonzero(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from odooctl.main import app
    from odooctl.operations.models import OperationStatus
    from odooctl.operations.store import OperationStore
    from odooctl.services.dr import DrDrillResult

    config = tmp_path / "odooctl.yml"
    config.write_text(
        """\
project:
  name: dr-test
  odoo_version: "19.0"
runtime:
  compose_file: docker-compose.yml
odoo:
  image: odoo:19.0
environments:
  production:
    branch: main
    domain: odoo.example.com
    db_name: production_db
    filestore_path: filestore/production_db
"""
    )
    monkeypatch.setattr(
        "odooctl.services.dr.run_dr_drill",
        lambda **kwargs: DrDrillResult(
            status="failed",
            environment="production",
            backup_id="production_2026-07-30_020000",
            message="filestore cleanup failed",
        ),
    )

    result = CliRunner().invoke(
        app,
        ["dr", "drill", "production", "--config", str(config)],
    )

    assert result.exit_code != 0
    operations = OperationStore(tmp_path / ".odooctl").list_all()
    assert len(operations) == 1
    assert operations[0].status is OperationStatus.FAILED
    assert operations[0].error == "filestore cleanup failed"
