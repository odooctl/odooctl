"""Restore-point browser service — list and verify local backup integrity."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from odooctl.services.restore import sha256_file


@dataclass
class RestorePoint:
    backup_id: str
    environment: str
    timestamp: str
    integrity: str  # "ok" | "failed" | "unknown"


def list_restore_points(
    backups_root: str | Path,
    *,
    environment: str | None = None,
) -> list[RestorePoint]:
    """Return restore points sorted newest-first, optionally filtered by environment."""
    root = Path(backups_root)
    if not root.exists() or not root.is_dir():
        return []

    points: list[RestorePoint] = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        manifest_file = d / "manifest.json"
        if not manifest_file.exists():
            continue

        try:
            manifest = json.loads(manifest_file.read_text())
        except Exception:
            continue

        env = manifest.get("environment", "")
        if environment is not None and env != environment:
            continue

        backup_id = manifest.get("backup_id", d.name)
        ts = str(manifest.get("timestamp") or "")

        integrity = _check_integrity(d, manifest)
        points.append(RestorePoint(
            backup_id=backup_id,
            environment=env,
            timestamp=ts,
            integrity=integrity,
        ))

    points.sort(
        key=lambda point: (
            _timestamp_key(point.timestamp),
            point.backup_id,
        ),
        reverse=True,
    )
    return points


def _timestamp_key(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d_%H%M%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _check_integrity(backup_dir: Path, manifest: dict) -> str:
    checksums = manifest.get("checksums") or {}
    pairs = [("db_dump", "db.dump"), ("filestore", "filestore.tar")]
    for key, fname in pairs:
        expected = checksums.get(key)
        if not expected:
            return "unknown"
        fpath = backup_dir / fname
        if not fpath.exists():
            return "failed"
        try:
            actual = sha256_file(fpath)
        except Exception:
            return "failed"
        if actual != expected:
            return "failed"
    return "ok"
