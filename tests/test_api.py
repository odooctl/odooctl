"""M12 API tests — auth, RBAC, routes, queue, event streaming."""
from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from odooctl.security import tokens  # noqa: E402

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

# Must satisfy the F24 key-strength floor (>= 32 characters).
TEST_KEY = "test-api-secret-key-123-0123456789abcdef"


def _mint_viewer(now=None):
    return tokens.mint(
        TEST_KEY,
        action="api",
        environment="*",
        project="*",
        ttl_seconds=300,
        now=now,
        roles=["viewer"],
    )


def _mint_operator(now=None):
    return tokens.mint(
        TEST_KEY,
        action="api",
        environment="*",
        project="*",
        ttl_seconds=300,
        now=now,
        roles=["operator"],
    )


def _mint_admin(now=None):
    return tokens.mint(
        TEST_KEY,
        action="api",
        environment="*",
        project="*",
        ttl_seconds=300,
        now=now,
        roles=["admin"],
    )


@pytest.fixture
def project_dir(tmp_path):
    (tmp_path / "odooctl.yml").write_text(MINIMAL_CONFIG)
    return tmp_path


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
            )
        },
    )


@pytest.fixture
def client(fake_registry):
    from odooctl.api.app import create_app

    app = create_app(
        api_key=TEST_KEY,
        registry_loader=lambda: fake_registry,
        allowed_hosts=["*"],
    )
    return TestClient(app)


# --- Auth tests ---


def test_unauthenticated_request_returns_401(client):
    resp = client.get("/projects")
    assert resp.status_code == 401


def test_invalid_token_returns_401(client):
    resp = client.get("/projects", headers={"Authorization": "Bearer bad.token.here"})
    assert resp.status_code == 401


def test_expired_token_returns_401(client):
    token = tokens.mint(
        TEST_KEY,
        action="api",
        environment="*",
        project="*",
        ttl_seconds=1,
        now=time.time() - 10,
        roles=["viewer"],
    )
    resp = client.get("/projects", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_viewer_can_list_projects(client):
    token = _mint_viewer()
    resp = client.get("/projects", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert "test-project" in resp.json()["projects"]


def test_viewer_can_get_environments(client):
    token = _mint_viewer()
    resp = client.get(
        "/projects/test-project/environments",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    envs = {e["name"] for e in resp.json()["environments"]}
    assert "production" in envs
    assert "staging" in envs


def test_viewer_cannot_enqueue_operation(client):
    token = _mint_viewer()
    resp = client.post(
        "/projects/test-project/operations",
        json={"kind": "backup", "environment": "production", "params": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_operator_can_enqueue_backup(client):
    token = _mint_operator()
    resp = client.post(
        "/projects/test-project/operations",
        json={"kind": "backup", "environment": "production", "params": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["kind"] == "backup"
    assert data["status"] == "queued"
    assert "op_id" in data


def test_operator_can_enqueue_clone(client):
    token = _mint_operator()
    resp = client.post(
        "/projects/test-project/operations",
        json={"kind": "clone", "environment": "staging", "params": {"source": "production"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 202


def test_get_operation_returns_record(client):
    token = _mint_operator()
    resp = client.post(
        "/projects/test-project/operations",
        json={"kind": "backup", "environment": "production", "params": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 202
    op_id = resp.json()["op_id"]

    resp2 = client.get(f"/operations/{op_id}", headers={"Authorization": f"Bearer {token}"})
    assert resp2.status_code == 200
    data = resp2.json()
    assert data["op_id"] == op_id
    assert data["status"] == "queued"


def test_get_operation_events_returns_sse(client):
    token = _mint_operator()
    resp = client.post(
        "/projects/test-project/operations",
        json={"kind": "backup", "environment": "production", "params": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    op_id = resp.json()["op_id"]

    resp2 = client.get(
        f"/operations/{op_id}/events?max_polls=1",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.status_code == 200
    assert "text/event-stream" in resp2.headers.get("content-type", "")


def test_post_operations_redacts_params(client, project_dir):
    token = _mint_operator()
    resp = client.post(
        "/projects/test-project/operations",
        json={
            "kind": "backup",
            "environment": "production",
            "params": {"password": "supersecret123", "db": "prod"},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 202
    op_id = resp.json()["op_id"]

    from odooctl.operations.store import OperationStore

    store = OperationStore(project_dir / ".odooctl")
    op = store.load(op_id)
    assert "supersecret123" not in json.dumps(op.params_redacted)


def test_api_does_not_import_privileged():
    """Runner contract: odooctl.api must not import privileged adapters."""
    from odooctl.security.runner_contract import find_violations

    violations = find_violations(("odooctl.api",))
    assert violations == [], "Contract violations:\n" + "\n".join(str(v) for v in violations)


def test_list_projects_returns_project_names(client):
    token = _mint_viewer()
    resp = client.get("/projects", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["projects"] == ["test-project"]


def test_get_project_not_found_returns_404(client):
    token = _mint_viewer()
    resp = client.get("/projects/unknown-project", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404


def test_get_project_returns_info(client):
    token = _mint_viewer()
    resp = client.get("/projects/test-project", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "test-project"


def test_list_backups(client):
    token = _mint_viewer()
    resp = client.get(
        "/projects/test-project/backups",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert "backups" in resp.json()


def test_get_project_status(client):
    token = _mint_viewer()
    resp = client.get(
        "/projects/test-project/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["project"] == "test-project"
    assert "environments" in data


def test_queue_entry_persisted_after_enqueue(client, project_dir):
    token = _mint_operator()
    resp = client.post(
        "/projects/test-project/operations",
        json={"kind": "backup", "environment": "production", "params": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    op_id = resp.json()["op_id"]

    queue_dir = project_dir / ".odooctl" / "queue"
    assert queue_dir.exists()
    assert len(list(queue_dir.glob(f"{op_id}.json"))) == 1


def test_capability_token_included_in_queue_entry(client, project_dir):
    token = _mint_operator()
    resp = client.post(
        "/projects/test-project/operations",
        json={"kind": "backup", "environment": "production", "params": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    op_id = resp.json()["op_id"]

    entry_path = project_dir / ".odooctl" / "queue" / f"{op_id}.json"
    entry = json.loads(entry_path.read_text())
    assert "token" in entry
    assert entry["token"]


def test_non_api_token_rejected_by_auth(client):
    """A capability token scoped to action='backup' must not authenticate the API."""
    token = tokens.mint(
        TEST_KEY,
        action="backup",
        environment="production",
        project="test-project",
        ttl_seconds=300,
    )
    resp = client.get("/projects", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_operator_cannot_enqueue_destructive_op_on_protected_env(client):
    """Operator must get 403 for destructive ops targeting a protected environment."""
    token = _mint_operator()
    resp = client.post(
        "/projects/test-project/operations",
        json={"kind": "clone", "environment": "production", "params": {"source": "staging"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_admin_can_enqueue_destructive_op_on_protected_env(client):
    """Admin must succeed for destructive ops on protected environments."""
    token = _mint_admin()
    resp = client.post(
        "/projects/test-project/operations",
        json={"kind": "clone", "environment": "production", "params": {"source": "staging"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 202


def test_admin_can_enqueue_dr_drill_on_protected_env(client):
    """DR drill is restore-class and admin-gated for protected source envs."""
    token = _mint_admin()
    resp = client.post(
        "/projects/test-project/operations",
        json={"kind": "dr_drill", "environment": "production", "params": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 202
    assert resp.json()["kind"] == "dr_drill"


def test_operator_cannot_enqueue_dr_drill_on_protected_env(client):
    """Operator cannot enqueue restore-class DR drills against protected envs."""
    token = _mint_operator()
    resp = client.post(
        "/projects/test-project/operations",
        json={"kind": "dr_drill", "environment": "production", "params": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_snapshot_operations_have_backup_and_restore_rbac_classes(client):
    operator = _mint_operator()
    create = client.post(
        "/projects/test-project/operations",
        json={
            "kind": "snapshot_create",
            "environment": "production",
            "params": {},
        },
        headers={"Authorization": f"Bearer {operator}"},
    )
    assert create.status_code == 202

    reconcile = client.post(
        "/projects/test-project/operations",
        json={
            "kind": "snapshot_reconcile",
            "environment": "production",
            "params": {"snapshot_id": "production-snapshot-1"},
        },
        headers={"Authorization": f"Bearer {operator}"},
    )
    assert reconcile.status_code == 202

    restore = client.post(
        "/projects/test-project/operations",
        json={
            "kind": "snapshot_restore",
            "environment": "production",
            "params": {"snapshot_id": "production-snapshot-1"},
        },
        headers={"Authorization": f"Bearer {operator}"},
    )
    assert restore.status_code == 403

    admin = _mint_admin()
    restore = client.post(
        "/projects/test-project/operations",
        json={
            "kind": "snapshot_restore",
            "environment": "production",
            "params": {"snapshot_id": "production-snapshot-1"},
        },
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert restore.status_code == 202


def test_snapshot_api_rejects_environment_label_outside_provider_binding(
    client,
    project_dir,
):
    with (project_dir / "odooctl.yml").open("a") as handle:
        handle.write(
            """
snapshots:
  provider: aws_ebs
  environment: production
  aws_ebs:
    instance_id: i-0123456789abcdef0
    region: us-east-1
    recovery_availability_zone: us-east-1a
"""
        )
    response = client.post(
        "/projects/test-project/operations",
        json={
            "kind": "snapshot_create",
            "environment": "staging",
            "params": {},
        },
        headers={"Authorization": f"Bearer {_mint_operator()}"},
    )

    assert response.status_code == 400
    assert "bound to environment 'production'" in response.json()["detail"]
    queue = project_dir / ".odooctl" / "queue"
    assert not queue.exists() or not list(queue.glob("*.json"))


def test_snapshot_api_exposes_exact_restore_confirmation_identity(
    client,
    project_dir,
):
    from odooctl.metadata.models import SnapshotManifest, SnapshotResource
    from odooctl.metadata.store import MetadataStore

    manifest = SnapshotManifest(
        snapshot_id="production-20260730-deadbeef",
        project="test-project",
        environment="production",
        provider="hetzner_cloud",
        source_resource_id="424242",
        resources=[
            SnapshotResource(
                snapshot_resource_id="12345",
                source_resource_id="424242",
                kind="server_root_disk",
                state="available",
            )
        ],
        scope=["hetzner_server_local_root_disk"],
        consistency="powered_off_consistent",
    )
    MetadataStore(project_dir / ".odooctl").save_snapshot_manifest(manifest)
    headers = {"Authorization": f"Bearer {_mint_viewer()}"}

    listed = client.get(
        "/projects/test-project/snapshots",
        headers=headers,
    )
    detail = client.get(
        f"/projects/test-project/snapshots/{manifest.snapshot_id}",
        headers=headers,
    )

    assert listed.status_code == 200
    assert listed.json()["snapshots"][0]["source_resource_id"] == "424242"
    assert detail.status_code == 200
    assert detail.json()["snapshot_id"] == manifest.snapshot_id
    assert detail.json()["source_resource_id"] == "424242"


def _enqueue_backup(client, token):
    resp = client.post(
        "/projects/test-project/operations",
        json={"kind": "backup", "environment": "production", "params": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 202
    return resp.json()["op_id"]


def test_viewer_cannot_cancel_operation(client, project_dir):
    """C5/F6: cancel is a write action — viewer tokens must get 403."""
    op_id = _enqueue_backup(client, _mint_operator())

    viewer = _mint_viewer()
    resp = client.post(f"/operations/{op_id}/cancel", headers={"Authorization": f"Bearer {viewer}"})
    assert resp.status_code == 403

    # The queue entry must still be pending (cancel did not go through).
    assert (project_dir / ".odooctl" / "queue" / f"{op_id}.json").exists()


def test_operator_can_cancel_operation(client):
    op_id = _enqueue_backup(client, _mint_operator())
    resp = client.post(
        f"/operations/{op_id}/cancel",
        headers={"Authorization": f"Bearer {_mint_operator()}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


def test_project_scoped_token_cannot_read_other_projects_operation(client):
    """C5: a token with a concrete proj claim must not see other projects' ops."""
    op_id = _enqueue_backup(client, _mint_operator())

    scoped = tokens.mint(
        TEST_KEY,
        action="api",
        environment="*",
        project="other-project",
        ttl_seconds=300,
        roles=["operator"],
    )
    resp = client.get(f"/operations/{op_id}", headers={"Authorization": f"Bearer {scoped}"})
    assert resp.status_code == 404
    resp = client.get(
        f"/operations/{op_id}/events?max_polls=1",
        headers={"Authorization": f"Bearer {scoped}"},
    )
    assert resp.status_code == 404


def test_project_scoped_token_cannot_cancel_other_projects_operation(client, project_dir):
    op_id = _enqueue_backup(client, _mint_operator())

    scoped = tokens.mint(
        TEST_KEY,
        action="api",
        environment="*",
        project="other-project",
        ttl_seconds=300,
        roles=["operator"],
    )
    resp = client.post(f"/operations/{op_id}/cancel", headers={"Authorization": f"Bearer {scoped}"})
    assert resp.status_code == 404
    assert (project_dir / ".odooctl" / "queue" / f"{op_id}.json").exists()


def test_project_scoped_token_matching_project_can_operate(client):
    """A proj claim naming the op's own project still reads and cancels it."""
    op_id = _enqueue_backup(client, _mint_operator())

    scoped = tokens.mint(
        TEST_KEY,
        action="api",
        environment="*",
        project="test-project",
        ttl_seconds=300,
        roles=["operator"],
    )
    resp = client.get(f"/operations/{op_id}", headers={"Authorization": f"Bearer {scoped}"})
    assert resp.status_code == 200
    resp = client.post(f"/operations/{op_id}/cancel", headers={"Authorization": f"Bearer {scoped}"})
    assert resp.status_code == 200


def test_capability_token_minted_with_default_300s_ttl(client, project_dir):
    """F12: enqueued capability tokens carry the short default TTL (300 s)."""
    op_id = _enqueue_backup(client, _mint_operator())
    entry = json.loads((project_dir / ".odooctl" / "queue" / f"{op_id}.json").read_text())
    payload = tokens.decode_unverified(entry["token"])
    assert payload["exp"] - payload["iat"] == 300


def test_create_app_rejects_short_env_sourced_key(monkeypatch, fake_registry):
    """F24: a weak ODOOCTL_API_KEY is rejected at app startup."""
    from odooctl.api.app import create_app

    monkeypatch.setenv("ODOOCTL_API_KEY", "short-key")
    with pytest.raises(ValueError, match="at least 32"):
        create_app(api_key="short-key", registry_loader=lambda: fake_registry)


def test_create_app_rejects_short_programmatic_key(fake_registry):
    """F24 / re-scan M4: the key floor is unconditional — a weak key is
    rejected regardless of whether it came from the environment."""
    from odooctl.api.app import create_app

    with pytest.raises(ValueError, match="at least 32"):
        create_app(api_key="short-key", registry_loader=lambda: fake_registry)


def test_max_polls_clamped_to_ceiling():
    from odooctl.api.routes_operations import MAX_POLLS_CEILING, _clamp_max_polls

    assert MAX_POLLS_CEILING == 600
    assert _clamp_max_polls(999_999) == 600
    assert _clamp_max_polls(600) == 600
    assert _clamp_max_polls(120) == 120
    assert _clamp_max_polls(0) == 1
    assert _clamp_max_polls(-5) == 1


def test_events_endpoint_accepts_huge_max_polls(client):
    """A huge max_polls must not 500/hang: clamp applies, terminal op ends stream."""
    token = _mint_operator()
    op_id = _enqueue_backup(client, token)
    resp = client.post(f"/operations/{op_id}/cancel", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200

    resp = client.get(
        f"/operations/{op_id}/events?max_polls=999999",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")


def test_cancel_operation_removes_queue_file(client, project_dir):
    """Cancelling a queued operation must remove its pending queue file."""
    token = _mint_operator()
    resp = client.post(
        "/projects/test-project/operations",
        json={"kind": "backup", "environment": "production", "params": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 202
    op_id = resp.json()["op_id"]

    queue_dir = project_dir / ".odooctl" / "queue"
    assert (queue_dir / f"{op_id}.json").exists()

    resp2 = client.post(f"/operations/{op_id}/cancel", headers={"Authorization": f"Bearer {token}"})
    assert resp2.status_code == 200

    assert not (queue_dir / f"{op_id}.json").exists()


# --- Re-scan H1: token project-scope enforcement ---


def _mint_scoped(project, roles=("operator",), now=None):
    """A capability token confined to a single project via its ``proj`` claim."""
    return tokens.mint(
        TEST_KEY,
        action="api",
        environment="*",
        project=project,
        ttl_seconds=300,
        now=now,
        roles=list(roles),
    )


def test_scoped_token_cannot_read_other_project_audit(client):
    """A token scoped to project 'acme' must not read 'test-project' data."""
    token = _mint_scoped("acme", roles=["admin"])
    resp = client.get("/projects/test-project/audit", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404


def test_scoped_token_cannot_enqueue_against_other_project(client):
    token = _mint_scoped("acme", roles=["operator"])
    resp = client.post(
        "/projects/test-project/operations",
        json={"kind": "backup", "environment": "production", "params": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


def test_scoped_token_reaches_its_own_project(client):
    token = _mint_scoped("test-project", roles=["admin"])
    resp = client.get("/projects/test-project/audit", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_wildcard_token_reaches_any_project(client):
    token = _mint_operator()  # project="*"
    resp = client.get("/projects/test-project/audit", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_scoped_token_list_projects_is_filtered(client):
    token = _mint_scoped("acme", roles=["viewer"])
    resp = client.get("/projects", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    # 'acme' isn't registered, so the scoped view is empty; crucially it does
    # not leak 'test-project'.
    assert resp.json()["projects"] == []


def test_scoped_token_status_and_backups_blocked(client):
    token = _mint_scoped("acme", roles=["operator"])
    for path in ("/projects/test-project/status", "/projects/test-project/backups",
                 "/projects/test-project/environments", "/projects/test-project/restore-points",
                 "/projects/test-project"):
        resp = client.get(path, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 404, path


def test_api_rejects_registry_config_escaping_project_root(tmp_path):
    """Re-scan #6: a hand-edited registry entry whose config escapes the
    project root must not be loaded by the API loaders."""
    from odooctl.api.app import create_app
    from odooctl.registry import Registry, RegisteredProject

    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    (proj_root / "odooctl.yml").write_text(MINIMAL_CONFIG)
    # Attacker points config outside the root.
    outside = tmp_path / "evil.yml"
    outside.write_text(MINIMAL_CONFIG)

    reg = Registry(
        path=tmp_path / "registry.toml",
        active="test-project",
        projects={
            "test-project": RegisteredProject(
                name="test-project", path=proj_root, config="../evil.yml"
            )
        },
    )
    app = create_app(api_key=TEST_KEY, registry_loader=lambda: reg, allowed_hosts=["*"])
    client = TestClient(app)
    token = _mint_admin()
    resp = client.get("/projects/test-project/status", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404
