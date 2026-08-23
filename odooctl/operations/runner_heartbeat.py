"""Runner liveness heartbeat.

The privileged runner writes a small heartbeat file next to the global registry
while it loops; the (unprivileged) API reads it to report whether queued
operations are actually being processed. Without a live runner, enqueued
operations sit ``queued`` forever — surfacing this is the difference between a
UI that "makes sense" and one that silently does nothing.

Deliberately dependency-free (stdlib only) so it can live in the shared
``operations`` package and be imported by both the runner and the API without
tripping the runner contract.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

HEARTBEAT_FILENAME = "runner-heartbeat.json"

# A heartbeat older than this many seconds is treated as "no live runner".
# The runner refreshes it at least once per idle loop (~1 s), so 15 s tolerates
# a slow iteration or brief GC pause without flapping.
STALE_AFTER_SECONDS = 15

_OFFLINE = {
    "online": False,
    "last_seen": None,
    "age_seconds": None,
    "pid": None,
    "started_at": None,
}


def heartbeat_path(registry_path: str | Path) -> Path:
    """Location of the heartbeat file, alongside the registry config."""
    return Path(registry_path).expanduser().parent / HEARTBEAT_FILENAME


def write_heartbeat(
    registry_path: str | Path,
    *,
    pid: int | None = None,
    started_at: str | None = None,
) -> None:
    """Atomically refresh the heartbeat file (called by the runner loop)."""
    path = heartbeat_path(registry_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "pid": pid if pid is not None else os.getpid(),
        "ts": now,
        "started_at": started_at or now,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


def read_status(registry_path: str | Path, *, now: datetime | None = None) -> dict:
    """Report runner liveness derived from the heartbeat file.

    Returns a dict with ``online`` (bool), ``last_seen`` (ISO str or None),
    ``age_seconds`` (float or None), ``pid`` and ``started_at``. A missing or
    unparseable file, or a stale timestamp, all read as offline.
    """
    path = heartbeat_path(registry_path)
    if not path.exists():
        return dict(_OFFLINE)
    try:
        data = json.loads(path.read_text())
    except Exception:
        return dict(_OFFLINE)

    ts_raw = data.get("ts")
    try:
        ts = datetime.fromisoformat(str(ts_raw))
    except (TypeError, ValueError):
        return {**_OFFLINE, "pid": data.get("pid"), "started_at": data.get("started_at")}
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    current = now or datetime.now(timezone.utc)
    age = (current - ts).total_seconds()
    return {
        "online": 0 <= age < STALE_AFTER_SECONDS,
        "last_seen": ts_raw,
        "age_seconds": round(max(0.0, age), 3),
        "pid": data.get("pid"),
        "started_at": data.get("started_at"),
    }
