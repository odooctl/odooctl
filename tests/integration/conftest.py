"""Disposable real-Odoo stacks for integration tests.

Each stack lives in its own temp directory with a unique docker compose
project name, so it can never collide with — or touch — anything else running
on the host. Everything it creates (containers, volumes, networks) is removed
in teardown via ``docker compose -p <unique> down -v``.

Run with:  pytest -m integration tests/integration
Select versions:  ODOOCTL_IT_VERSIONS=17.0,18.0,19.0 (default: 19.0)

Resource note: stacks are brought up one at a time (function-scoped fixture,
serial parametrization) — sized for a 4-core / 8 GB host.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON = str(REPO_ROOT / ".venv" / "bin" / "python")
DB_PASSWORD = "odoo"

COMPOSE_TEMPLATE = """services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: postgres
      POSTGRES_USER: odoo
      POSTGRES_PASSWORD: {password}
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U odoo -d postgres"]
      interval: 2s
      timeout: 5s
      retries: 30
    volumes:
      - postgres-data:/var/lib/postgresql/data

  odoo:
    image: odoo:{version}
    depends_on:
      db:
        condition: service_healthy
    environment:
      HOST: db
      USER: odoo
      PASSWORD: {password}
    ports:
      - "{port}:8069"
    volumes:
      - odoo-data:/var/lib/odoo

volumes:
  postgres-data:
  odoo-data:
"""

ODOOCTL_TEMPLATE = """project:
  name: {project_name}
  odoo_version: "{version}"

runtime:
  type: docker_compose
  compose_file: docker-compose.yml
  reverse_proxy: none
  execution_mode: docker

postgres:
  host: localhost
  port: 5432
  user: odoo
  password_env: ODOO_DB_PASSWORD
  service: db
  internal_host: db

backups:
  local_path: backups

odoo:
  image: odoo:{version}
  cli_command: odoo
  service: odoo
  config_path: /etc/odoo/odoo.conf
  db_host: db
  db_user: odoo
  db_password_env: ODOO_DB_PASSWORD
  filestore_container_path: /var/lib/odoo

sanitization:
  native_neutralize: required

environments:
  production:
    branch: main
    scheme: http
    domain: localhost
    port: {port}
    db_selector: true
    db_name: odoo_prod
    filestore_path: filestore/odoo_prod
    filestore_volume: {volume}
    update_modules: [base]
  staging:
    branch: staging
    scheme: http
    domain: localhost
    port: {port}
    db_selector: true
    db_name: odoo_staging
    filestore_path: filestore/odoo_staging
    filestore_volume: {volume}
    clone_from: production
    sanitize: true
    update_modules: [base]

healthcheck:
  scheme: http
  path: /web/health
  timeout_seconds: 10
  retries: 20
  interval_seconds: 3
"""


def _versions() -> list[str]:
    return [v.strip() for v in os.environ.get("ODOOCTL_IT_VERSIONS", "19.0").split(",") if v.strip()]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class OdooStack:
    version: str
    root: Path
    compose_project: str
    port: int
    env: dict[str, str]

    def compose(self, *args: str, check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["docker", "compose", "-p", self.compose_project, "-f", str(self.root / "docker-compose.yml"), *args],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )

    def odooctl(self, *args: str, check: bool = True, timeout: int = 600, input_text: str | None = None) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [PYTHON, "-m", "odooctl.main", "-C", str(self.root), *args],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
        )
        if check and result.returncode != 0:
            raise AssertionError(
                f"odooctl {' '.join(args)} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}"
            )
        return result

    def psql(self, db: str, sql: str) -> str:
        result = self.compose(
            "exec", "-T", "-e", "PGPASSWORD", "db", "psql", "-U", "odoo", "-h", "db", "-d", db, "-tAc", sql,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(f"psql -d {db} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
        return result.stdout.strip()

    def init_database(self, db_name: str) -> None:
        """Initialize an Odoo database with the base module (no demo data)."""
        self.compose(
            "exec", "-T", "odoo",
            "odoo", "-d", db_name, "-i", "base", "--without-demo=all", "--stop-after-init",
            "--db_host", "db", "--db_user", "odoo", f"--db_password={DB_PASSWORD}",
            timeout=600,
        )

    def wait_healthy(self, timeout: int = 120) -> None:
        deadline = time.time() + timeout
        import urllib.request

        url = f"http://localhost:{self.port}/web/login"
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    if 200 <= response.status < 300:
                        return
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"Odoo at {url} did not become healthy within {timeout}s")


@pytest.fixture(params=_versions(), scope="module")
def odoo_stack(request, tmp_path_factory) -> OdooStack:
    version = request.param
    root = tmp_path_factory.mktemp(f"odooctl-it-{version.replace('.', '')}-{uuid.uuid4().hex[:6]}")
    # odooctl itself runs `docker compose -f <file>` from the project dir, so
    # the compose project name is derived from the directory name. The harness
    # must use the same name — an explicit -p that differs would split the
    # stack in two. The uuid in the directory name keeps it globally unique.
    compose_project = root.name.lower()
    port = _free_port()
    volume = f"{compose_project}_odoo-data"

    (root / "docker-compose.yml").write_text(
        COMPOSE_TEMPLATE.format(version=version, port=port, password=DB_PASSWORD)
    )
    (root / "odooctl.yml").write_text(
        ODOOCTL_TEMPLATE.format(project_name=f"it-{version.replace('.', '-')}", version=version, port=port, volume=volume)
    )
    (root / "backups").mkdir()

    env = os.environ.copy()
    env["ODOO_DB_PASSWORD"] = DB_PASSWORD
    # For the harness's own psql assertions (docker compose exec -e PGPASSWORD
    # forwards the value from the client environment).
    env["PGPASSWORD"] = DB_PASSWORD
    # Isolate the registry so `project add` never touches the operator's config.
    env["XDG_CONFIG_HOME"] = str(root / "xdg")

    stack = OdooStack(version=version, root=root, compose_project=compose_project, port=port, env=env)

    stack.compose("up", "-d", "--wait", timeout=600)
    stack.init_database("odoo_prod")
    stack.compose("restart", "odoo")
    stack.wait_healthy()

    yield stack

    stack.compose("down", "-v", "--remove-orphans", check=False, timeout=300)
