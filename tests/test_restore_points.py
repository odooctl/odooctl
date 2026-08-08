"""M14 restore-points browser tests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _make_backup_dir(root: Path, env: str, ts: str, *, valid: bool = True) -> Path:
    backup_id = f"{env}_{ts}"
    d = root / backup_id
    d.mkdir(parents=True)
    (d / "db.dump").write_bytes(b"dbdump")
    (d / "filestore.tar").write_bytes(b"filestore")
    db_hash = hashlib.sha256(b"dbdump").hexdigest()
    fs_hash = hashlib.sha256(b"filestore").hexdigest()
    if not valid:
        db_hash = "badhashbadhashbadhashbadhashbadhashbadhashbadhashbadhashbadhash00"
    manifest = {
        "backup_id": backup_id,
        "project": "test",
        "environment": env,
        "timestamp": ts,
        "db_name": f"{env}_db",
        "odoo_version": "19.0",
        "backup_mode": "full",
        "checksums": {"db_dump": db_hash, "filestore": fs_hash},
    }
    (d / "manifest.json").write_text(json.dumps(manifest))
    return d


def test_restore_points_returns_sorted_descending(tmp_path):
    from odooctl.services.restore_points import list_restore_points

    root = tmp_path / "backups"
    root.mkdir()
    _make_backup_dir(root, "production", "2026-05-30_100000")
    _make_backup_dir(root, "production", "2026-05-31_100000")

    points = list_restore_points(root)
    assert len(points) == 2
    assert points[0].backup_id == "production_2026-05-31_100000"
    assert points[1].backup_id == "production_2026-05-30_100000"


def test_restore_points_sort_by_manifest_time_with_random_id_suffixes(tmp_path):
    from odooctl.services.restore_points import list_restore_points

    root = tmp_path / "backups"
    root.mkdir()
    older = _make_backup_dir(root, "production", "2026-05-30_100000")
    newer = _make_backup_dir(root, "production", "2026-05-31_100000")
    older.rename(root / f"{older.name}_ffffffffffff")
    newer.rename(root / f"{newer.name}_000000000000")

    points = list_restore_points(root)

    assert [point.timestamp for point in points] == [
        "2026-05-31_100000",
        "2026-05-30_100000",
    ]


def test_restore_points_filters_by_environment(tmp_path):
    from odooctl.services.restore_points import list_restore_points

    root = tmp_path / "backups"
    root.mkdir()
    _make_backup_dir(root, "production", "2026-05-31_100000")
    _make_backup_dir(root, "staging", "2026-05-31_120000")

    points = list_restore_points(root, environment="staging")
    assert len(points) == 1
    assert points[0].backup_id == "staging_2026-05-31_120000"


def test_restore_points_integrity_ok_on_valid_backup(tmp_path):
    from odooctl.services.restore_points import list_restore_points

    root = tmp_path / "backups"
    root.mkdir()
    _make_backup_dir(root, "production", "2026-05-31_100000", valid=True)

    points = list_restore_points(root)
    assert points[0].integrity == "ok"


def test_restore_points_integrity_failed_on_corrupt_backup(tmp_path):
    from odooctl.services.restore_points import list_restore_points

    root = tmp_path / "backups"
    root.mkdir()
    _make_backup_dir(root, "production", "2026-05-31_100000", valid=False)

    points = list_restore_points(root)
    assert points[0].integrity == "failed"


def test_restore_points_empty_for_empty_dir(tmp_path):
    from odooctl.services.restore_points import list_restore_points

    root = tmp_path / "backups"
    root.mkdir()

    assert list_restore_points(root) == []


def test_restore_points_missing_root_returns_empty(tmp_path):
    from odooctl.services.restore_points import list_restore_points

    assert list_restore_points(tmp_path / "nonexistent") == []


def test_restore_point_has_environment_field(tmp_path):
    from odooctl.services.restore_points import list_restore_points

    root = tmp_path / "backups"
    root.mkdir()
    _make_backup_dir(root, "staging", "2026-05-31_100000")

    points = list_restore_points(root)
    assert points[0].environment == "staging"


def test_restore_point_has_timestamp_field(tmp_path):
    from odooctl.services.restore_points import list_restore_points

    root = tmp_path / "backups"
    root.mkdir()
    _make_backup_dir(root, "production", "2026-05-31_100000")

    points = list_restore_points(root)
    assert points[0].timestamp == "2026-05-31_100000"


def test_restore_points_multiple_environments(tmp_path):
    from odooctl.services.restore_points import list_restore_points

    root = tmp_path / "backups"
    root.mkdir()
    _make_backup_dir(root, "production", "2026-05-31_100000")
    _make_backup_dir(root, "production", "2026-05-30_100000")
    _make_backup_dir(root, "staging", "2026-05-31_120000")

    all_points = list_restore_points(root)
    assert len(all_points) == 3

    prod_points = list_restore_points(root, environment="production")
    assert len(prod_points) == 2


def test_restore_points_skips_non_backup_dirs(tmp_path):
    from odooctl.services.restore_points import list_restore_points

    root = tmp_path / "backups"
    root.mkdir()
    _make_backup_dir(root, "production", "2026-05-31_100000")
    # A non-backup directory (no manifest.json)
    (root / "some_other_dir").mkdir()

    points = list_restore_points(root)
    assert len(points) == 1
