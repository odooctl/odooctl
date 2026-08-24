"""Runner heartbeat + /runner/status endpoint.

The runner writes a heartbeat file while looping; the API reports liveness so the
UI can tell operators when enqueued operations will actually be processed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from odooctl.operations.runner_heartbeat import (
    STALE_AFTER_SECONDS,
    heartbeat_path,
    read_status,
    write_heartbeat,
)

TEST_KEY = "test-heartbeat-secret-key-0123456789abcdef"


def test_missing_heartbeat_reads_offline(tmp_path):
    status = read_status(tmp_path / "registry.toml")
    assert status["online"] is False
    assert status["last_seen"] is None


def test_fresh_heartbeat_reads_online(tmp_path):
    reg = tmp_path / "registry.toml"
    write_heartbeat(reg, pid=4242, started_at="2026-07-20T00:00:00+00:00")
    status = read_status(reg)
    assert status["online"] is True
    assert status["pid"] == 4242
    assert status["started_at"] == "2026-07-20T00:00:00+00:00"
    assert status["age_seconds"] is not None and status["age_seconds"] < STALE_AFTER_SECONDS


def test_stale_heartbeat_reads_offline(tmp_path):
    reg = tmp_path / "registry.toml"
    write_heartbeat(reg)
    stale_now = datetime.now(timezone.utc) + timedelta(seconds=STALE_AFTER_SECONDS + 5)
    status = read_status(reg, now=stale_now)
    assert status["online"] is False
    assert status["age_seconds"] >= STALE_AFTER_SECONDS


def test_corrupt_heartbeat_reads_offline(tmp_path):
    reg = tmp_path / "registry.toml"
    heartbeat_path(reg).write_text("{not json")
    assert read_status(reg)["online"] is False


def test_heartbeat_written_next_to_registry(tmp_path):
    reg = tmp_path / "nested" / "config.toml"
    write_heartbeat(reg)
    assert heartbeat_path(reg).exists()
    assert heartbeat_path(reg).parent == reg.parent


# --- endpoint ---

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from odooctl.security import tokens  # noqa: E402


def _viewer():
    return tokens.mint(TEST_KEY, action="api", environment="*", project="*", ttl_seconds=300, roles=["viewer"])


def _client(tmp_path):
    from odooctl.api.app import create_app
    from odooctl.registry import Registry

    reg = Registry(path=tmp_path / "registry.toml", active=None, projects={})
    app = create_app(api_key=TEST_KEY, registry_loader=lambda: reg, allowed_hosts=["*"])
    return TestClient(app)


def test_runner_status_endpoint_offline_without_heartbeat(tmp_path):
    client = _client(tmp_path)
    resp = client.get("/runner/status", headers={"Authorization": "Bearer " + _viewer()})
    assert resp.status_code == 200
    body = resp.json()
    assert body["online"] is False
    assert body["hint"] == "odooctl runner"


def test_runner_status_endpoint_online_with_heartbeat(tmp_path):
    write_heartbeat(tmp_path / "registry.toml")
    client = _client(tmp_path)
    resp = client.get("/runner/status", headers={"Authorization": "Bearer " + _viewer()})
    assert resp.status_code == 200
    body = resp.json()
    assert body["online"] is True
    assert body["hint"] is None


def test_runner_status_requires_auth(tmp_path):
    client = _client(tmp_path)
    assert client.get("/runner/status").status_code == 401
