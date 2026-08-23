"""v0.3.0 container visibility + access management.

Covers: container-status snapshots (runner-written, API-read), the
/projects/{p}/containers and /rbac/matrix endpoints, admin token minting via
POST /tokens, and the service_logs / service_restart operation kinds with their
project-wide protection policy.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from odooctl.operations.container_status import (
    STALE_AFTER_SECONDS,
    read_snapshot,
    write_snapshot,
)
from odooctl.security import rbac

TEST_KEY = "test-containers-secret-key-0123456789abcdef"

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
  staging:
    branch: staging
    domain: staging.test.local
    port: 8070
    db_name: test_staging
    filestore_path: ./filestore/staging
    clone_from: production
    sanitize: true
"""


# ---------------------------------------------------------------------------
# container_status snapshot module
# ---------------------------------------------------------------------------


def test_snapshot_roundtrip_normalizes_compose_fields(tmp_path):
    write_snapshot(tmp_path, [{"Service": "odoo", "State": "running", "Health": "healthy", "Image": "odoo:19.0", "Name": "x-odoo-1", "Status": "Up 2 hours"}])
    snap = read_snapshot(tmp_path)
    assert snap["available"] is True
    assert snap["stale"] is False
    assert snap["containers"] == [
        {"service": "odoo", "name": "x-odoo-1", "image": "odoo:19.0", "state": "running", "status": "Up 2 hours", "health": "healthy"}
    ]


def test_snapshot_missing_reads_unavailable(tmp_path):
    snap = read_snapshot(tmp_path)
    assert snap["available"] is False
    assert snap["containers"] == []


def test_snapshot_goes_stale(tmp_path):
    write_snapshot(tmp_path, [])
    later = datetime.now(timezone.utc) + timedelta(seconds=STALE_AFTER_SECONDS + 1)
    assert read_snapshot(tmp_path, now=later)["stale"] is True


def test_snapshot_records_probe_error(tmp_path):
    write_snapshot(tmp_path, [], error="docker unreachable")
    assert read_snapshot(tmp_path)["error"] == "docker unreachable"


# ---------------------------------------------------------------------------
# ps_json parsing (NDJSON and array forms)
# ---------------------------------------------------------------------------


def _fake_run(stdout):
    class R:
        pass

    r = R()
    r.stdout = stdout
    return r


def test_ps_json_parses_ndjson(monkeypatch):
    from odooctl.adapters import docker_compose as dc

    monkeypatch.setattr(dc, "run", lambda *a, **k: _fake_run('{"Service": "odoo"}\n{"Service": "db"}\n'))
    records = dc.DockerComposeAdapter().ps_json()
    assert [r["Service"] for r in records] == ["odoo", "db"]


def test_ps_json_parses_array_and_empty(monkeypatch):
    from odooctl.adapters import docker_compose as dc

    monkeypatch.setattr(dc, "run", lambda *a, **k: _fake_run('[{"Service": "odoo"}]'))
    assert dc.DockerComposeAdapter().ps_json()[0]["Service"] == "odoo"
    monkeypatch.setattr(dc, "run", lambda *a, **k: _fake_run(""))
    assert dc.DockerComposeAdapter().ps_json() == []


# ---------------------------------------------------------------------------
# project-wide protection policy + service allowlist
# ---------------------------------------------------------------------------


class _Cfg:
    def __init__(self, envs, protected):
        self.environments = envs
        self._protected = protected

    def is_protected(self, name):
        return name in self._protected


def test_kind_protected_service_restart_inherits_any_protected_env():
    cfg = _Cfg({"production": None, "staging": None}, {"production"})
    assert rbac.kind_protected(cfg, "service_restart", "staging") is True
    assert rbac.kind_protected(cfg, "backup", "staging") is False
    assert rbac.kind_protected(cfg, "backup", "production") is True


def test_kind_protected_all_unprotected_project():
    cfg = _Cfg({"dev": None, "qa": None}, set())
    assert rbac.kind_protected(cfg, "service_restart", "dev") is False


def test_resolve_service_param_allowlist():
    from odooctl.runner.worker import _resolve_service_param

    class Odoo:
        service = "odoo"

    class Pg:
        service = "db"

    class Cfg:
        odoo = Odoo()
        postgres = Pg()

    assert _resolve_service_param(Cfg, None) == "odoo"
    assert _resolve_service_param(Cfg, "db") == "db"
    with pytest.raises(ValueError, match="not allowed"):
        _resolve_service_param(Cfg, "traefik; rm -rf /")


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from odooctl.security import tokens  # noqa: E402


def _mint(role):
    return tokens.mint(TEST_KEY, action="api", environment="*", project="*", ttl_seconds=300, roles=[role])


def _auth(role):
    return {"Authorization": "Bearer " + _mint(role)}


@pytest.fixture
def project_dir(tmp_path):
    (tmp_path / "odooctl.yml").write_text(MINIMAL_CONFIG)
    return tmp_path


@pytest.fixture
def client(project_dir):
    from odooctl.api.app import create_app
    from odooctl.registry import Registry, RegisteredProject

    reg = Registry(
        path=project_dir / "registry.toml",
        active="test-project",
        projects={"test-project": RegisteredProject(name="test-project", path=project_dir, config="odooctl.yml")},
    )
    app = create_app(api_key=TEST_KEY, registry_loader=lambda: reg, allowed_hosts=["*"])
    return TestClient(app)


def test_containers_endpoint_serves_runner_snapshot(client, project_dir):
    state_dir = project_dir / ".odooctl"
    write_snapshot(state_dir, [{"Service": "odoo", "State": "running"}])
    resp = client.get("/projects/test-project/containers", headers=_auth("viewer"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["containers"][0]["service"] == "odoo"
    assert body["services"]["odoo"] == "odoo"
    assert body["services"]["postgres"] == "postgres"


def test_containers_endpoint_unavailable_without_probe(client):
    resp = client.get("/projects/test-project/containers", headers=_auth("viewer"))
    assert resp.status_code == 200
    assert resp.json()["available"] is False


def test_containers_endpoint_requires_auth(client):
    assert client.get("/projects/test-project/containers").status_code == 401


def test_rbac_matrix_endpoint(client):
    resp = client.get("/rbac/matrix", headers=_auth("viewer"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["matrix"]["viewer"]["backup"] is False
    assert body["matrix"]["operator"]["backup"] is True
    assert body["matrix"]["admin"]["promote"] is True
    assert "deploy" in body["destructive_on_protected"]
    assert body["roles"] == ["viewer"]


# ---- POST /tokens (admin mint) ----


def test_admin_can_mint_viewer_token_that_authenticates(client):
    resp = client.post("/tokens", json={"role": "viewer", "ttl_seconds": 3600}, headers=_auth("admin"))
    assert resp.status_code == 201
    minted = resp.json()["token"]
    check = client.get("/projects", headers={"Authorization": "Bearer " + minted})
    assert check.status_code == 200


def test_operator_cannot_mint_tokens(client):
    resp = client.post("/tokens", json={"role": "viewer"}, headers=_auth("operator"))
    assert resp.status_code == 403


def test_admin_cannot_mint_owner_token(client):
    resp = client.post("/tokens", json={"role": "owner"}, headers=_auth("admin"))
    assert resp.status_code == 403


def test_mint_rejects_bad_role_and_ttl(client):
    assert client.post("/tokens", json={"role": "superuser"}, headers=_auth("admin")).status_code == 400
    assert client.post("/tokens", json={"role": "viewer", "ttl_seconds": 5}, headers=_auth("admin")).status_code == 400
    assert client.post("/tokens", json={"role": "viewer", "ttl_seconds": 10**9}, headers=_auth("admin")).status_code == 400


# ---- enqueue policy for the new kinds ----


def test_viewer_can_enqueue_service_logs(client):
    resp = client.post(
        "/projects/test-project/operations",
        json={"kind": "service_logs", "environment": "staging", "params": {"tail": 50}},
        headers=_auth("viewer"),
    )
    assert resp.status_code == 202, resp.text


def test_viewer_cannot_enqueue_service_restart(client):
    resp = client.post(
        "/projects/test-project/operations",
        json={"kind": "service_restart", "environment": "staging", "params": {}},
        headers=_auth("viewer"),
    )
    assert resp.status_code == 403


def test_operator_blocked_from_restart_when_project_has_protected_env(client):
    # staging itself is unprotected, but the shared container also serves
    # production — project-wide policy requires admin.
    resp = client.post(
        "/projects/test-project/operations",
        json={"kind": "service_restart", "environment": "staging", "params": {}},
        headers=_auth("operator"),
    )
    assert resp.status_code == 403


def test_admin_can_enqueue_service_restart(client):
    resp = client.post(
        "/projects/test-project/operations",
        json={"kind": "service_restart", "environment": "staging", "params": {}},
        headers=_auth("admin"),
    )
    assert resp.status_code == 202, resp.text


# ---------------------------------------------------------------------------
# SPA contract: new affordances present in the bundled dist
# ---------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

_DIST = Path(__file__).parent.parent / "odooctl" / "web" / "dist"


def test_app_js_has_containers_panel():
    content = (_DIST / "app.js").read_text()
    assert "/containers" in content
    assert "svc-logs" in content
    assert "svc-restart" in content
    assert "service_logs" in content
    assert "service_restart" in content


def test_app_js_has_access_page_and_minting():
    content = (_DIST / "app.js").read_text()
    assert "#/access" in content
    assert "/rbac/matrix" in content
    assert "'/tokens'" in content
    assert "mint-role" in content


def test_style_has_rbac_matrix_and_token_box():
    css = (_DIST / "style.css").read_text()
    assert ".rbac-matrix" in css
    assert ".token-box" in css
