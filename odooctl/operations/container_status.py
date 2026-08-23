"""Container-status snapshots shared between the runner and the API.

The privileged runner probes ``docker compose ps`` for each registered project
on a fixed cadence and writes a small JSON snapshot into the project state dir;
the unprivileged API serves it from disk. This gives the web UI live container
state without the API ever touching Docker (runner contract).

Stdlib-only on purpose — importable from ``odooctl.api`` without dragging in
privileged adapters.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

SNAPSHOT_FILENAME = "container-status.json"

# How often the runner refreshes each project's snapshot, and after how long a
# snapshot is reported stale (runner stopped / project unreachable).
PROBE_INTERVAL_SECONDS = 10
STALE_AFTER_SECONDS = 30


def snapshot_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / SNAPSHOT_FILENAME


def normalize_record(raw: dict) -> dict:
    """Reduce a ``docker compose ps --format json`` record to stable fields."""
    return {
        "service": raw.get("Service") or raw.get("service") or "",
        "name": raw.get("Name") or raw.get("name") or "",
        "image": raw.get("Image") or raw.get("image") or "",
        "state": raw.get("State") or raw.get("state") or "unknown",
        "status": raw.get("Status") or raw.get("status") or "",
        "health": raw.get("Health") or raw.get("health") or "",
    }


def write_snapshot(state_dir: str | Path, records: list[dict], *, error: str | None = None) -> None:
    """Atomically persist a snapshot (called by the runner)."""
    path = snapshot_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "containers": [normalize_record(r) for r in records],
        "error": error,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


def read_snapshot(state_dir: str | Path, *, now: datetime | None = None) -> dict:
    """Load a snapshot for the API: ``{available, stale, ts, age_seconds, containers, error}``.

    ``available`` is False when no snapshot exists (no runner has probed yet);
    ``stale`` is True when the snapshot is older than :data:`STALE_AFTER_SECONDS`.
    """
    path = snapshot_path(state_dir)
    empty = {"available": False, "stale": True, "ts": None, "age_seconds": None, "containers": [], "error": None}
    if not path.exists():
        return empty
    try:
        data = json.loads(path.read_text())
    except Exception:
        return empty
    ts_raw = data.get("ts")
    try:
        ts = datetime.fromisoformat(str(ts_raw))
    except (TypeError, ValueError):
        return empty
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    age = max(0.0, (current - ts).total_seconds())
    return {
        "available": True,
        "stale": age >= STALE_AFTER_SECONDS,
        "ts": ts_raw,
        "age_seconds": round(age, 3),
        "containers": data.get("containers") or [],
        "error": data.get("error"),
    }
