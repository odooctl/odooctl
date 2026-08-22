"""Full operator lifecycle against a real Odoo stack.

Ordered, stateful scenario per Odoo version: validate → doctor → backup →
clone --sanitize → restore --to → API enqueue + runner parity. Uses one
module-scoped stack per version to keep total wall-clock reasonable on a
4-core host.
"""

from __future__ import annotations

import json
import signal
import subprocess
import time
import urllib.request

import pytest

from tests.integration.conftest import PYTHON

pytestmark = [pytest.mark.integration, pytest.mark.docker]


def test_validate_and_doctor(odoo_stack):
    result = odoo_stack.odooctl("validate")
    assert "Config valid" in result.stdout

    doctor = odoo_stack.odooctl("doctor", "--json")
    payload = json.loads(doctor.stdout)
    assert payload, doctor.stdout


def test_status_reports_environments(odoo_stack):
    result = odoo_stack.odooctl("status", "--json")
    payload = json.loads(result.stdout)
    names = {env.get("environment") or env.get("name") for env in payload} if isinstance(payload, list) else set(payload)
    assert names, result.stdout


def test_backup_with_verify(odoo_stack):
    result = odoo_stack.odooctl("backup", "production", "--verify")
    backup_id = result.stdout.strip().splitlines()[-1]
    assert backup_id.startswith("production_")
    backup_dir = odoo_stack.root / "backups" / backup_id
    assert (backup_dir / "db.dump").exists()
    assert (backup_dir / "filestore.tar").exists()
    manifest = json.loads((backup_dir / "manifest.json").read_text())
    assert manifest["environment"] == "production"
    assert manifest["checksums"]["db_dump"]


def test_clone_production_to_staging_sanitizes(odoo_stack):
    odoo_stack.odooctl("clone", "production", "staging")
    databases = odoo_stack.psql("postgres", "SELECT datname FROM pg_database")
    assert "odoo_staging" in databases

    crons = odoo_stack.psql("odoo_staging", "SELECT count(*) FROM ir_cron WHERE active")
    assert crons == "0", f"expected all crons disabled in sanitized staging, found {crons} active"

    mail = odoo_stack.psql(
        "odoo_staging",
        "SELECT count(*) FROM ir_mail_server "
        "WHERE active AND name = 'neutralization - disable emails' "
        "AND smtp_host = 'invalid'",
    )
    assert mail == "1"
    active_mail = odoo_stack.psql(
        "odoo_staging",
        "SELECT count(*) FROM ir_mail_server WHERE active",
    )
    assert active_mail == "1"
    neutralized = odoo_stack.psql(
        "odoo_staging",
        "SELECT value FROM ir_config_parameter WHERE key = 'database.is_neutralized'",
    )
    assert neutralized.lower() == "true"
    database_secret = odoo_stack.psql(
        "odoo_staging",
        "SELECT value FROM ir_config_parameter WHERE key = 'database.secret'",
    )
    assert len(database_secret) >= 32
    login_form = odoo_stack.staging_login_form()
    assert "csrf_token" in login_form
    assert "<form" in login_form
    metadata = json.loads(
        (odoo_stack.root / ".odooctl" / "sanitizations" / "staging-latest.json").read_text()
    )
    assert metadata["native_status"] == "executed"
    assert metadata["verified"] is True


def test_restore_production_backup_into_staging(odoo_stack):
    result = odoo_stack.odooctl("restore", "production", "--to", "staging", "--yes")
    assert "Restored production backup" in result.stdout
    marker = odoo_stack.psql("odoo_staging", "SELECT count(*) FROM res_users")
    assert int(marker) >= 1


def test_api_enqueue_and_runner_parity(odoo_stack):
    """Regression for audit finding C2: an operation enqueued through the API
    must succeed through the runner, and `runner --once` must exit non-zero
    when it does not."""
    api_key = "integration-test-api-key-0123456789abcdef"
    env = dict(odoo_stack.env, ODOOCTL_API_KEY=api_key)

    odoo_stack.odooctl("project", "add", "itproj", "--path", str(odoo_stack.root))

    mint = subprocess.run(
        [
            PYTHON, "-m", "odooctl.main", "security", "token", "mint",
            "--action", "api", "--env", "*", "--project", "*",
            "--role", "operator", "--key-env", "ODOOCTL_API_KEY", "--ttl", "600",
        ],
        cwd=odoo_stack.root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    bearer = mint.stdout.strip().splitlines()[-1]

    port = None
    server = None
    try:
        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

        server = subprocess.Popen(
            [PYTHON, "-m", "odooctl.main", "serve", "--host", "127.0.0.1", "--port", str(port)],
            cwd=odoo_stack.root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                req = urllib.request.Request(f"{base}/projects", headers={"Authorization": f"Bearer {bearer}"})
                with urllib.request.urlopen(req, timeout=3) as response:
                    if response.status == 200:
                        break
            except Exception:
                time.sleep(1)
        else:
            raise TimeoutError("API server did not come up")

        body = json.dumps({"kind": "backup", "environment": "production"}).encode()
        req = urllib.request.Request(
            f"{base}/projects/itproj/operations",
            data=body,
            headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            assert response.status == 202, response.status
            op = json.loads(response.read())
        op_id = op.get("op_id") or op.get("id")
        assert op_id

        runner = subprocess.run(
            [PYTHON, "-m", "odooctl.main", "runner", "--once"],
            cwd=odoo_stack.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert runner.returncode == 0, (
            f"runner --once failed (exit {runner.returncode}):\n{runner.stdout}\n{runner.stderr}"
        )

        req = urllib.request.Request(
            f"{base}/operations/{op_id}",
            headers={"Authorization": f"Bearer {bearer}"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            final = json.loads(response.read())
        assert final.get("status") == "succeeded", final
    finally:
        if server is not None:
            server.send_signal(signal.SIGINT)
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()


def test_harness_never_touches_foreign_containers(odoo_stack):
    """Everything this stack created is namespaced by its compose project."""
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={odoo_stack.compose_project}", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    own = set(result.stdout.split())
    assert own, "expected the stack's own containers to exist"
    assert all(name.startswith(odoo_stack.compose_project) for name in own)
