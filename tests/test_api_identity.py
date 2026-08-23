"""m17 §6 identity — login sessions, user management, ownership, attribution,
and RBAC enforcement coverage on every mutating API route."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from odooctl.security import tokens  # noqa: E402
from odooctl.security.principals import Role  # noqa: E402
from odooctl.security.sessions import SESSION_COOKIE, SessionStore  # noqa: E402
from odooctl.security.users import UserStore  # noqa: E402

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
    owner: platform-team
  staging:
    branch: staging
    domain: staging.test.local
    port: 8070
    db_name: test_staging
    filestore_path: ./filestore/staging
    clone_from: production
    sanitize: true
"""

TEST_KEY = "test-api-secret-key-123-0123456789abcdef"


def _mint(role: str):
    return tokens.mint(
        TEST_KEY, action="api", environment="*", project="*", ttl_seconds=300, roles=[role]
    )


def _bearer(role: str) -> dict:
    return {"Authorization": f"Bearer {_mint(role)}"}


@pytest.fixture
def project_dir(tmp_path):
    (tmp_path / "odooctl.yml").write_text(MINIMAL_CONFIG)
    return tmp_path


@pytest.fixture
def auth_dir(tmp_path):
    return tmp_path / "auth"


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
                owner="rami@example.com",
            )
        },
    )


@pytest.fixture
def app(fake_registry, auth_dir):
    from odooctl.api.app import create_app

    return create_app(
        api_key=TEST_KEY,
        registry_loader=lambda: fake_registry,
        allowed_hosts=["*"],
        auth_dir=auth_dir,
    )


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def users(auth_dir):
    return UserStore(auth_dir)


@pytest.fixture
def admin_user(users):
    return users.create("admin@example.com", "admin-password-1", roles=[Role.ADMIN], name="Admin")


@pytest.fixture
def viewer_user(users):
    return users.create("viewer@example.com", "viewer-password-1", roles=[Role.VIEWER])


def _login(client, email, password):
    return client.post("/auth/login", json={"email": email, "password": password})


# ------------------------------------------------------------------ login
def test_login_sets_session_cookie_and_authenticates(client, admin_user):
    resp = _login(client, "admin@example.com", "admin-password-1")
    assert resp.status_code == 200
    assert resp.json()["user"]["email"] == "admin@example.com"
    assert SESSION_COOKIE in resp.cookies

    me = client.get("/auth/me")
    assert me.status_code == 200
    body = me.json()
    assert body["id"] == "admin@example.com"
    assert body["kind"] == "user"
    assert body["roles"] == ["admin"]
    assert body["session"] is True

    # Session cookie works on ordinary routes too.
    assert client.get("/projects").status_code == 200


def test_login_wrong_password_and_unknown_email_are_identical(client, admin_user):
    wrong = _login(client, "admin@example.com", "not-the-password")
    unknown = _login(client, "ghost@example.com", "whatever-pass")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_login_backoff_returns_429_after_repeated_failures(client, admin_user):
    for _ in range(5):
        assert _login(client, "admin@example.com", "bad-password-x").status_code == 401
    assert _login(client, "admin@example.com", "bad-password-x").status_code == 429
    # Even the correct password is throttled once the window is tripped.
    assert _login(client, "admin@example.com", "admin-password-1").status_code == 429


def test_login_cookie_flags(client, admin_user):
    resp = _login(client, "admin@example.com", "admin-password-1")
    set_cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie


def test_disabled_user_cannot_login(client, users, admin_user):
    users.set_disabled(admin_user.id, True)
    assert _login(client, "admin@example.com", "admin-password-1").status_code == 401


# --------------------------------------------------------------- sessions
def test_logout_revokes_session(client, admin_user):
    _login(client, "admin@example.com", "admin-password-1")
    assert client.get("/auth/me").status_code == 200
    resp = client.post("/auth/logout")
    assert resp.status_code == 200 and resp.json()["revoked"] is True
    client.cookies.clear()  # TestClient keeps the (now dead) cookie value
    assert client.get("/auth/me").status_code == 401


def test_revoked_session_cookie_is_rejected(client, auth_dir, admin_user):
    resp = _login(client, "admin@example.com", "admin-password-1")
    sid = resp.cookies[SESSION_COOKIE]
    SessionStore(auth_dir).revoke(sid)
    assert client.get("/auth/me").status_code == 401


def test_disable_kills_live_sessions_immediately(client, users, admin_user):
    _login(client, "admin@example.com", "admin-password-1")
    users.set_disabled(admin_user.id, True)
    assert client.get("/auth/me").status_code == 401


def test_role_changes_apply_to_live_sessions(client, users, admin_user):
    _login(client, "admin@example.com", "admin-password-1")
    assert client.get("/users").status_code == 200
    users.set_roles(admin_user.id, [Role.VIEWER])
    assert client.get("/users").status_code == 403


def test_password_change_revokes_other_sessions(app, auth_dir, admin_user):
    first = TestClient(app)
    second = TestClient(app)
    _login(first, "admin@example.com", "admin-password-1")
    _login(second, "admin@example.com", "admin-password-1")

    resp = first.post(
        "/auth/password",
        json={"current_password": "admin-password-1", "new_password": "admin-password-2"},
    )
    assert resp.status_code == 200
    # The changing session survives; the other one is revoked.
    assert first.get("/auth/me").status_code == 200
    assert second.get("/auth/me").status_code == 401
    # New password is live.
    third = TestClient(app)
    assert _login(third, "admin@example.com", "admin-password-2").status_code == 200


def test_password_change_requires_correct_current(client, admin_user):
    _login(client, "admin@example.com", "admin-password-1")
    resp = client.post(
        "/auth/password",
        json={"current_password": "wrong-password", "new_password": "admin-password-2"},
    )
    assert resp.status_code == 403


def test_password_change_requires_session_not_bearer(client, admin_user):
    resp = client.post(
        "/auth/password",
        json={"current_password": "x", "new_password": "admin-password-2"},
        headers=_bearer("admin"),
    )
    assert resp.status_code == 400


# ------------------------------------------------------------ users admin
def test_admin_can_crud_users(client, admin_user):
    _login(client, "admin@example.com", "admin-password-1")

    resp = client.post(
        "/users",
        json={"email": "op@example.com", "password": "operator-pass-1", "roles": ["operator"]},
    )
    assert resp.status_code == 201
    uid = resp.json()["user"]["id"]

    listed = client.get("/users").json()["users"]
    assert {u["email"] for u in listed} == {"admin@example.com", "op@example.com"}

    resp = client.patch(f"/users/{uid}", json={"roles": ["viewer"], "name": "Op"})
    assert resp.status_code == 200
    assert resp.json()["user"]["roles"] == ["viewer"]

    resp = client.post(f"/users/{uid}/password", json={"new_password": "operator-pass-2"})
    assert resp.status_code == 200

    resp = client.delete(f"/users/{uid}")
    assert resp.status_code == 200
    assert client.get("/users").json()["users"][0]["email"] == "admin@example.com"


def test_user_management_requires_admin(client, viewer_user):
    assert client.get("/users", headers=_bearer("viewer")).status_code == 403
    assert client.get("/users", headers=_bearer("operator")).status_code == 403
    resp = client.post(
        "/users",
        json={"email": "x@example.com", "password": "password-123", "roles": ["viewer"]},
        headers=_bearer("operator"),
    )
    assert resp.status_code == 403


def test_admin_cannot_create_or_grant_above_own_rank(client, admin_user):
    _login(client, "admin@example.com", "admin-password-1")
    resp = client.post(
        "/users",
        json={"email": "boss@example.com", "password": "password-123", "roles": ["owner"]},
    )
    assert resp.status_code == 403


def test_admin_cannot_modify_owner_account(client, users, admin_user):
    owner = users.create("owner@example.com", "owner-password-1", roles=[Role.OWNER])
    _login(client, "admin@example.com", "admin-password-1")
    assert client.patch(f"/users/{owner.id}", json={"disabled": True}).status_code == 403
    assert client.delete(f"/users/{owner.id}").status_code == 403
    assert (
        client.post(f"/users/{owner.id}/password", json={"new_password": "password-123"})
    ).status_code == 403


def test_cannot_disable_or_delete_self(client, admin_user):
    _login(client, "admin@example.com", "admin-password-1")
    assert client.patch(f"/users/{admin_user.id}", json={"disabled": True}).status_code == 400
    assert client.delete(f"/users/{admin_user.id}").status_code == 400


def test_disable_via_api_revokes_sessions(app, users, admin_user, viewer_user):
    admin = TestClient(app)
    viewer = TestClient(app)
    _login(admin, "admin@example.com", "admin-password-1")
    _login(viewer, "viewer@example.com", "viewer-password-1")
    assert viewer.get("/auth/me").status_code == 200

    resp = admin.patch(f"/users/{viewer_user.id}", json={"disabled": True})
    assert resp.status_code == 200
    assert viewer.get("/auth/me").status_code == 401


def test_create_duplicate_user_is_409(client, admin_user):
    _login(client, "admin@example.com", "admin-password-1")
    body = {"email": "dup@example.com", "password": "password-123", "roles": ["viewer"]}
    assert client.post("/users", json=body).status_code == 201
    assert client.post("/users", json=body).status_code == 409


def test_create_user_validation_errors(client, admin_user):
    _login(client, "admin@example.com", "admin-password-1")
    resp = client.post(
        "/users", json={"email": "x@example.com", "password": "password-1", "roles": ["wizard"]}
    )
    assert resp.status_code == 400
    resp = client.post("/users", json={"email": "x@example.com", "password": "short"})
    assert resp.status_code == 400


# ------------------------------------------------------------- ownership
def test_project_and_environment_owner_exposed(client, admin_user):
    _login(client, "admin@example.com", "admin-password-1")
    proj = client.get("/projects/test-project").json()
    assert proj["owner"] == "rami@example.com"
    envs = client.get("/projects/test-project/environments").json()["environments"]
    by_name = {e["name"]: e for e in envs}
    assert by_name["production"]["owner"] == "platform-team"
    assert by_name["staging"]["owner"] is None


def test_set_project_owner_requires_admin_and_persists(client, fake_registry, admin_user):
    resp = client.patch(
        "/projects/test-project/owner",
        json={"owner": "new-owner@example.com"},
        headers=_bearer("viewer"),
    )
    assert resp.status_code == 403
    resp = client.patch(
        "/projects/test-project/owner",
        json={"owner": "new-owner@example.com"},
        headers=_bearer("operator"),
    )
    assert resp.status_code == 403

    _login(client, "admin@example.com", "admin-password-1")
    resp = client.patch("/projects/test-project/owner", json={"owner": "new-owner@example.com"})
    assert resp.status_code == 200
    assert resp.json()["owner"] == "new-owner@example.com"

    from odooctl.registry import load_registry

    saved = load_registry(fake_registry.path)
    assert saved.projects["test-project"].owner == "new-owner@example.com"


# ----------------------------------------------------------- attribution
def test_enqueued_operation_records_user_email_as_actor(client, project_dir, admin_user):
    _login(client, "admin@example.com", "admin-password-1")
    resp = client.post(
        "/projects/test-project/operations",
        json={"kind": "backup", "environment": "staging", "params": {}},
    )
    assert resp.status_code == 202
    op_id = resp.json()["op_id"]

    from odooctl.context import ProjectContext
    from odooctl.operations.store import OperationStore

    ctx = ProjectContext.from_config_path("odooctl.yml", root=project_dir)
    op = OperationStore(ctx.state_dir).load(op_id)
    assert op.actor == "admin@example.com"


def test_session_respects_protected_env_rbac(client, users):
    users.create("op@example.com", "operator-pass-1", roles=[Role.OPERATOR])
    _login(client, "op@example.com", "operator-pass-1")
    # Operator may back up staging but not deploy to protected production.
    resp = client.post(
        "/projects/test-project/operations",
        json={"kind": "backup", "environment": "staging", "params": {}},
    )
    assert resp.status_code == 202
    resp = client.post(
        "/projects/test-project/operations",
        json={"kind": "deploy", "environment": "production", "params": {}},
    )
    assert resp.status_code == 403


# ------------------------------------------- RBAC coverage (phase 5 gate)
# Mutating routes that are legitimately unauthenticated: login IS the
# authenticator; logout only revokes the caller's own cookie.
_AUTH_EXEMPT = {("/auth/login", "POST"), ("/auth/logout", "POST")}


def _depends_on_get_principal(dependant) -> bool:
    from odooctl.api.auth import get_principal

    if dependant.call is get_principal:
        return True
    return any(_depends_on_get_principal(sub) for sub in dependant.dependencies)


def _api_routes(routes):
    """Yield path-operation routes across supported FastAPI router layouts."""
    from fastapi.routing import APIRoute

    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif (nested := getattr(route, "original_router", None)) is not None:
            yield from _api_routes(nested.routes)


def test_every_mutating_route_requires_authentication(app):
    """Phase-5 invariant: no API mutation without an authenticated principal."""
    mutating = {"POST", "PUT", "PATCH", "DELETE"}
    checked = 0
    for route in _api_routes(app.routes):
        methods = set(route.methods or ()) & mutating
        if not methods:
            continue
        for method in methods:
            if (route.path, method) in _AUTH_EXEMPT:
                continue
            assert _depends_on_get_principal(route.dependant), (
                f"{method} {route.path} has no authentication dependency"
            )
            checked += 1
    assert checked >= 8  # tokens, ops enqueue/cancel, users CRUD, owner, password


def test_rbac_matrix_includes_users_action():
    from odooctl.security import rbac

    matrix = rbac.role_matrix()
    assert matrix["admin"]["users"] is True
    assert matrix["owner"]["users"] is True
    assert matrix["operator"]["users"] is False
    assert matrix["viewer"]["users"] is False
