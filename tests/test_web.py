"""M13 Web UI tests — static assets, SPA serving, API contract.

TDD approach: asset-existence and runner-contract tests run against the dist
files; serving tests use FastAPI TestClient with the bundled dist mounted.
"""
from __future__ import annotations

from pathlib import Path
import tomllib

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from odooctl.security import tokens  # noqa: E402

_DIST = Path(__file__).parent.parent / "odooctl" / "web" / "dist"

TEST_KEY = "test-web-secret-key-456-0123456789abcdef"

MINIMAL_CONFIG = """\
project:
  name: web-test-project
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
    domain: prod.web.local
    port: 8069
    db_name: web_prod
    filestore_path: ./filestore/production
  staging:
    branch: staging
    domain: staging.web.local
    port: 8070
    db_name: web_staging
    filestore_path: ./filestore/staging
    clone_from: production
    sanitize: true
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def project_dir(tmp_path):
    (tmp_path / "odooctl.yml").write_text(MINIMAL_CONFIG)
    return tmp_path


@pytest.fixture
def fake_registry(project_dir):
    from odooctl.registry import Registry, RegisteredProject

    return Registry(
        path=project_dir / "registry.toml",
        active="web-test-project",
        projects={
            "web-test-project": RegisteredProject(
                name="web-test-project",
                path=project_dir,
                config="odooctl.yml",
            )
        },
    )


@pytest.fixture
def spa_client(fake_registry):
    """TestClient with the bundled SPA mounted at /."""
    from odooctl.api.app import create_app

    app = create_app(
        api_key=TEST_KEY,
        registry_loader=lambda: fake_registry,
        allowed_hosts=["*"],
        static_dir=_DIST,
    )
    return TestClient(app, raise_server_exceptions=False)


def _viewer():
    return tokens.mint(TEST_KEY, action="api", environment="*", project="*", ttl_seconds=300, roles=["viewer"])


def _operator():
    return tokens.mint(TEST_KEY, action="api", environment="*", project="*", ttl_seconds=300, roles=["operator"])


# ---------------------------------------------------------------------------
# 1. Asset existence
# ---------------------------------------------------------------------------

def test_dist_directory_exists():
    assert _DIST.is_dir(), f"Bundled dist directory missing: {_DIST}"


def test_index_html_exists_and_is_non_empty():
    p = _DIST / "index.html"
    assert p.exists(), "index.html missing from dist"
    content = p.read_text()
    assert len(content) > 100
    assert "odooctl" in content.lower()


def test_app_js_exists_and_is_non_empty():
    p = _DIST / "app.js"
    assert p.exists(), "app.js missing from dist"
    assert p.stat().st_size > 200


def test_style_css_exists_and_is_non_empty():
    p = _DIST / "style.css"
    assert p.exists(), "style.css missing from dist"
    assert p.stat().st_size > 200


def test_index_html_references_app_js_and_css():
    content = (_DIST / "index.html").read_text()
    assert "app.js" in content
    assert "style.css" in content


def test_pyproject_packages_web_dist_assets():
    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    build_targets = pyproject["tool"]["hatch"]["build"]["targets"]
    for target in ("wheel", "sdist"):
        force_include = build_targets[target]["force-include"]
        assert force_include["odooctl/web/dist"] == "odooctl/web/dist"


# ---------------------------------------------------------------------------
# 2. SPA content — UI structure and constraints
# ---------------------------------------------------------------------------

def test_app_js_uses_fetch_for_api_calls():
    content = (_DIST / "app.js").read_text()
    assert "fetch(" in content, "SPA must use fetch() for API calls"


def test_app_js_has_no_cli_or_service_imports():
    content = (_DIST / "app.js").read_text()
    assert "subprocess" not in content
    assert "odooctl.adapters" not in content
    assert "odooctl.services" not in content
    assert "docker" not in content.lower() or "no docker" in content.lower()


def test_app_js_has_confirmation_for_destructive_actions():
    content = (_DIST / "app.js").read_text()
    assert "confirmAndRun" in content or "confirm" in content.lower()


def test_app_js_has_sse_streaming():
    content = (_DIST / "app.js").read_text()
    # Must use fetch-based streaming or EventSource for SSE
    assert "stream" in content.lower()
    assert "/events" in content


def test_app_js_has_rbac_role_check():
    content = (_DIST / "app.js").read_text()
    # Must decode token roles for client-side display gating
    assert "roles" in content
    assert "operator" in content
    assert "admin" in content


def test_app_js_has_doctor_view():
    content = (_DIST / "app.js").read_text()
    assert "Doctor" in content
    assert "buildDoctorTab" in content


def test_app_js_has_clone_operation():
    content = (_DIST / "app.js").read_text()
    assert "clone" in content


def test_clone_request_targets_selected_environment_and_names_source():
    content = (_DIST / "app.js").read_text()
    assert "kind: 'clone', environment: target, params: { source: sourceEnv" in content
    assert "kind: 'clone', environment: sourceEnv, params: { target: target" not in content


def test_app_js_has_promote_operation():
    content = (_DIST / "app.js").read_text()
    assert "promote" in content


def test_app_js_has_backup_operation():
    content = (_DIST / "app.js").read_text()
    assert "backup" in content


# ---------------------------------------------------------------------------
# 3. Runner contract: odooctl.web must not import privileged modules
# ---------------------------------------------------------------------------

def test_web_package_no_privileged_imports():
    from odooctl.security.runner_contract import find_violations

    violations = find_violations(("odooctl.web",))
    assert violations == [], \
        "odooctl.web imports privileged modules:\n" + "\n".join(str(v) for v in violations)


def test_find_violations_covers_web_package():
    """Smoke: find_violations() can scan odooctl.web without error."""
    from odooctl.security.runner_contract import find_violations

    # Should run without raising even though web/dist/*.js are not Python
    result = find_violations(("odooctl.web",))
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# 3b. docs/web-ui.md exists
# ---------------------------------------------------------------------------

def test_docs_web_ui_md_exists():
    p = Path(__file__).parent.parent / "docs" / "web-ui.md"
    assert p.exists(), "docs/web-ui.md is missing"
    content = p.read_text()
    assert len(content) > 200
    assert "Architecture" in content
    assert "RBAC" in content


# ---------------------------------------------------------------------------
# 4. Static file serving via FastAPI
# ---------------------------------------------------------------------------

def test_spa_serves_index_html_at_root(spa_client):
    resp = spa_client.get("/")
    assert resp.status_code == 200
    ct = resp.headers.get("content-type", "")
    assert "html" in ct.lower() or len(resp.content) > 50


def test_spa_serves_app_js(spa_client):
    resp = spa_client.get("/app.js")
    assert resp.status_code == 200


def test_spa_serves_style_css(spa_client):
    resp = spa_client.get("/style.css")
    assert resp.status_code == 200


def test_spa_returns_index_html_for_unknown_path(spa_client):
    """SPA html=True mode returns index.html for client-side routes."""
    resp = spa_client.get("/this-path-does-not-exist-in-the-api")
    assert resp.status_code == 200  # served as SPA index.html


# ---------------------------------------------------------------------------
# 4a. index.html is cached at startup (serve is long-running; rebuild → restart)
# ---------------------------------------------------------------------------

def test_spa_index_html_cached_at_startup(fake_registry, tmp_path):
    """The SPA fallback serves index.html bytes cached at app creation.

    Rewriting index.html on disk after startup must not change the fallback
    response — a rebuilt SPA dist requires a server restart to be picked up.
    """
    from fastapi.testclient import TestClient
    from odooctl.api.app import create_app

    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html>original index</html>")

    app = create_app(
        api_key=TEST_KEY,
        registry_loader=lambda: fake_registry,
        allowed_hosts=["*"],
        static_dir=static_dir,
    )
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/some/client-side/route")
    assert resp.status_code == 200
    assert "original index" in resp.text

    # Simulate a rebuild after startup: the cached bytes must still be served.
    (static_dir / "index.html").write_text("<html>rebuilt index</html>")

    resp = client.get("/some/client-side/route")
    assert resp.status_code == 200
    assert "original index" in resp.text
    assert "rebuilt index" not in resp.text


def test_spa_fallback_does_not_reread_index_per_request(fake_registry, tmp_path):
    """The fallback keeps serving from memory even if index.html is deleted."""
    from fastapi.testclient import TestClient
    from odooctl.api.app import create_app

    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html>cached copy</html>")

    app = create_app(
        api_key=TEST_KEY,
        registry_loader=lambda: fake_registry,
        allowed_hosts=["*"],
        static_dir=static_dir,
    )
    client = TestClient(app, raise_server_exceptions=False)

    (static_dir / "index.html").unlink()

    resp = client.get("/spa-route-after-delete")
    assert resp.status_code == 200
    assert "cached copy" in resp.text


# ---------------------------------------------------------------------------
# 4b. Path traversal guard
# ---------------------------------------------------------------------------

def test_spa_traversal_guard_relative_to_rejects_sibling_dir(tmp_path):
    """relative_to correctly blocks sibling-dir traversal that startswith would miss.

    If static_dir is /foo/dist and a resolved candidate is /foo/distevil/secret,
    str(candidate).startswith(str(static_dir)) returns True (false-positive).
    Path.relative_to() raises ValueError and correctly blocks it.
    """
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    sibling = tmp_path / "distevil"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("TOP SECRET")
    (static_dir / "index.html").write_text("<html>odooctl</html>")

    _static = static_dir.resolve()
    candidate = (_static / "../distevil/secret.txt").resolve()

    # Demonstrate the startswith flaw
    assert str(candidate).startswith(str(_static)), (
        "pre-condition: startswith incorrectly passes for sibling-dir path"
    )

    # relative_to correctly rejects it
    import pytest as _pytest
    with _pytest.raises(ValueError):
        candidate.relative_to(_static)


def test_spa_traversal_via_client_does_not_leak_secret(fake_registry, tmp_path):
    """Path traversal through the SPA route must not serve files outside static_dir."""
    from fastapi.testclient import TestClient
    from odooctl.api.app import create_app

    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    sibling = tmp_path / "distevil"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("TOP SECRET")
    (static_dir / "index.html").write_text("<html>odooctl safe</html>")

    app = create_app(
        api_key=TEST_KEY,
        registry_loader=lambda: fake_registry,
        allowed_hosts=["*"],
        static_dir=static_dir,
    )
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/../distevil/secret.txt", follow_redirects=False)
    assert "TOP SECRET" not in resp.text
    # Traversal either falls back to index.html (200) or redirect/4xx — never leaks
    assert resp.status_code in (200, 301, 302, 307, 308, 400, 404)


# ---------------------------------------------------------------------------
# 5. API routes take priority over SPA (contract)
# ---------------------------------------------------------------------------

def test_api_auth_takes_priority_over_spa(spa_client):
    """Unauthenticated /projects → 401, not SPA index.html."""
    resp = spa_client.get("/projects")
    assert resp.status_code == 401


def test_api_routes_work_with_spa_mounted(spa_client):
    token = _viewer()
    resp = spa_client.get("/projects", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert "projects" in resp.json()


def test_api_environments_route_with_spa(spa_client):
    token = _viewer()
    resp = spa_client.get(
        "/projects/web-test-project/environments",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "environments" in data
    names = [e["name"] for e in data["environments"]]
    assert "production" in names
    assert "staging" in names


def test_api_status_route_with_spa(spa_client):
    token = _viewer()
    resp = spa_client.get(
        "/projects/web-test-project/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "environments" in data
    assert "recent_operations" in data


def test_api_backups_route_with_spa(spa_client):
    token = _viewer()
    resp = spa_client.get(
        "/projects/web-test-project/backups",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert "backups" in resp.json()


def test_operator_can_enqueue_backup_via_spa_client(spa_client):
    token = _operator()
    resp = spa_client.post(
        "/projects/web-test-project/operations",
        json={"kind": "backup", "environment": "staging", "params": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["kind"] == "backup"
    assert data["environment"] == "staging"


def test_viewer_cannot_enqueue_operations_via_spa_client(spa_client):
    token = _viewer()
    resp = spa_client.post(
        "/projects/web-test-project/operations",
        json={"kind": "backup", "environment": "staging", "params": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 6. serve command: bundled dist path and auto-detection
# ---------------------------------------------------------------------------

def test_bundled_dist_path_helper():
    """_bundled_dist() returns a path inside the odooctl package."""
    from odooctl.commands.serve import _bundled_dist

    dist = _bundled_dist()
    assert dist.name == "dist"
    assert "odooctl" in str(dist)
    assert "web" in str(dist)


def test_bundled_dist_path_matches_known_location():
    from odooctl.commands.serve import _bundled_dist

    expected = Path(__file__).parent.parent / "odooctl" / "web" / "dist"
    assert _bundled_dist() == expected


def test_bundled_dist_is_valid_dir():
    from odooctl.commands.serve import _bundled_dist

    dist = _bundled_dist()
    assert dist.is_dir()
    assert (dist / "index.html").exists()
    assert (dist / "app.js").exists()
    assert (dist / "style.css").exists()


def test_serve_run_auto_detects_bundled_spa(monkeypatch):
    """serve.run() passes the bundled dist path to create_app when static_dir=None."""
    import odooctl.api.app as app_mod

    received = {}

    def capturing_create(api_key, **kwargs):
        received.update(kwargs)
        # Return a minimal FastAPI app without actually mounting anything
        from fastapi import FastAPI
        return FastAPI()

    monkeypatch.setattr(app_mod, "create_app", capturing_create)

    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)

    from odooctl.commands import serve as serve_mod
    serve_mod.run(api_key="testkey", static_dir=None)

    assert "static_dir" in received, "create_app must be called with static_dir"
    sd = received["static_dir"]
    assert sd is not None, "static_dir must not be None when bundled dist exists"
    sd_path = Path(sd)
    assert sd_path.exists()
    assert (sd_path / "index.html").exists()


def test_serve_run_respects_explicit_static_dir(monkeypatch, tmp_path):
    """serve.run() uses the explicit --static-dir instead of bundled dist."""
    import odooctl.api.app as app_mod

    (tmp_path / "index.html").write_text("<html>custom</html>")
    received = {}

    def capturing_create(api_key, **kwargs):
        received.update(kwargs)
        from fastapi import FastAPI
        return FastAPI()

    monkeypatch.setattr(app_mod, "create_app", capturing_create)

    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)

    from odooctl.commands import serve as serve_mod
    serve_mod.run(api_key="testkey", static_dir=tmp_path)

    assert received.get("static_dir") == tmp_path


def test_serve_run_no_static_dir_when_dist_absent(monkeypatch, tmp_path):
    """serve.run() passes static_dir=None when bundled dist does not exist."""
    import odooctl.api.app as app_mod
    from odooctl.commands import serve as serve_mod

    received = {}

    def capturing_create(api_key, **kwargs):
        received.update(kwargs)
        from fastapi import FastAPI
        return FastAPI()

    monkeypatch.setattr(app_mod, "create_app", capturing_create)

    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)

    # Point _bundled_dist() at a non-existent directory
    monkeypatch.setattr(serve_mod, "_bundled_dist", lambda: tmp_path / "nonexistent")

    serve_mod.run(api_key="testkey", static_dir=None)

    assert received.get("static_dir") is None
