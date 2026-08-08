from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from conftest import strip_ansi
from odooctl.config import load_config
from odooctl.main import app
from odooctl.services import local_cluster as local_service
from odooctl.services.context import ServiceContext
from odooctl.services.local_cluster import (
    create_local_cluster,
    delete_local_cluster,
    local_cluster_name,
    render_local_simulation,
    run_local_smoke,
)
from odooctl.utils.shell import CommandResult


CONFIG = """\
project:
  name: Acme ERP
  odoo_version: "19.0"
runtime:
  type: kubernetes
  namespace_template: "{project}-{environment}"
  image_pull_policy: IfNotPresent
postgres:
  host: postgres
  user: odoo
  password_env: ODOO_DB_PASSWORD
odoo:
  image: odooctl-local:19
  service: odoo
  cli_command: odoo
local_simulation:
  enabled: true
  environment: development
  output_path: .odooctl/local
  cluster_prefix: odooctl
  k3s_image: rancher/k3s:v1.31.5-k3s1
  http_port: 18069
  postgres_port: 15432
  postgres_image: postgres:16
  build_context: .
  dockerfile: Dockerfile
  live_update_paths: [addons]
  rollout_timeout_seconds: 30
environments:
  development:
    tier: development
    branch: develop
    scheme: http
    domain: odoo.localhost
    db_name: acme_development
    filestore_path: acme_development
    filestore_volume: development-filestore
    rollout_strategy: rolling
"""


def _context(tmp_path: Path) -> ServiceContext:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "odooctl.yml").write_text(CONFIG)
    (tmp_path / "Dockerfile").write_text("FROM odoo:19\n")
    (tmp_path / "addons").mkdir()
    return ServiceContext.from_config_path(tmp_path / "odooctl.yml")


def _result(args, returncode=0, stdout="", stderr=""):
    return CommandResult(list(args), returncode, stdout, stderr)


def _record_ownership(result) -> None:
    payload = json.loads(result.plan.read_text())
    payload.update({"created_by": "odooctl", "created_at": "2026-07-31T00:00:00+00:00"})
    result.ownership.write_text(json.dumps(payload))


def test_local_simulation_requires_kubernetes_runtime(tmp_path: Path):
    config = tmp_path / "odooctl.yml"
    config.write_text(
        CONFIG.replace("type: kubernetes", "type: docker_compose").replace(
            '  namespace_template: "{project}-{environment}"\n'
            "  image_pull_policy: IfNotPresent\n",
            "  compose_file: docker-compose.yml\n",
        )
    )

    with pytest.raises(ValidationError, match="requires runtime.type: kubernetes"):
        load_config(config)


def test_shipped_k3d_example_validates():
    root = Path(__file__).parents[1]

    cfg = load_config(root / "examples/k3d/odooctl.yml")

    assert cfg.local_simulation.enabled is True
    assert cfg.local_simulation.environment == "development"
    assert cfg.env("development").rollout_strategy == "rolling"


def test_cluster_name_is_reproducible_and_project_path_scoped(tmp_path: Path):
    first = _context(tmp_path / "first")
    second = _context(tmp_path / "second")

    assert local_cluster_name(first) == local_cluster_name(first)
    assert local_cluster_name(first) != local_cluster_name(second)
    assert local_cluster_name(first).startswith("odooctl-acme-erp-")
    assert len(local_cluster_name(first)) <= 32


def test_render_reuses_canonical_resources_and_adds_local_postgres(tmp_path: Path):
    ctx = _context(tmp_path)

    result = render_local_simulation(ctx)

    k3d = yaml.safe_load(result.k3d_config.read_text())
    assert k3d["metadata"]["name"] == result.cluster_name
    assert k3d["image"] == "rancher/k3s:v1.31.5-k3s1"
    assert k3d["ports"][0]["port"] == "127.0.0.1:18069:80"
    resources = list(yaml.safe_load_all(result.resources.read_text()))
    kinds = [item["kind"] for item in resources]
    assert kinds[:5] == [
        "Namespace",
        "PersistentVolumeClaim",
        "Deployment",
        "Service",
        "Ingress",
    ]
    assert kinds.count("Deployment") == 2
    assert kinds.count("Service") == 2
    for resource in resources:
        labels = resource["metadata"]["labels"]
        assert labels["app.kubernetes.io/managed-by"] == "odooctl"
        assert labels["odooctl.dev/project"] == "acme-erp"
        assert labels["odooctl.dev/environment"] == "development"
    assert not any(item["kind"] == "Secret" for item in resources)
    rendered = result.resources.read_text()
    assert "stringData:" not in rendered
    odoo = next(
        item
        for item in resources
        if item["kind"] == "Deployment" and item["metadata"]["name"] == "odoo"
    )
    container = odoo["spec"]["template"]["spec"]["containers"][0]
    assert {"name": "HOST", "value": "postgres"} in container["env"]
    assert {"name": "USER", "value": "odoo"} in container["env"]
    tilt = result.tiltfile.read_text()
    assert "docker_build('odooctl-local:19'" in tilt
    assert "sync('addons', '/mnt/extra-addons')" in tilt
    assert "k8s_resource('postgres'" in tilt
    assert "k8s_resource('odoo'" in tilt
    assert "k8s_resource('odoo-ingress'" in tilt
    assert result.plan.is_file()
    assert not result.ownership.exists()


def test_render_rejects_output_that_escapes_project_root(tmp_path: Path):
    ctx = _context(tmp_path)
    ctx.project.config.local_simulation.output_path = str(
        tmp_path / "inside" / ".." / ".." / "outside"
    )

    with pytest.raises(ValueError, match="must stay inside"):
        render_local_simulation(ctx)


def test_existing_unowned_cluster_is_never_adopted(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path)
    name = local_cluster_name(ctx)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return _result(args, stdout=json.dumps([{"name": name}]))

    monkeypatch.setattr(local_service, "run", fake_run)

    with pytest.raises(RuntimeError, match="without ownership record"):
        create_local_cluster(ctx)

    assert not any(call[:3] == ["k3d", "cluster", "create"] for call in calls)


def test_create_refuses_stale_owned_record_when_cluster_is_absent(
    tmp_path: Path,
    monkeypatch,
):
    ctx = _context(tmp_path)
    result = render_local_simulation(ctx)
    _record_ownership(result)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return _result(args, stdout="[]")

    monkeypatch.setattr(local_service, "run", fake_run)

    with pytest.raises(RuntimeError, match="stale ownership record"):
        create_local_cluster(ctx)

    assert not any(call[:3] == ["k3d", "cluster", "create"] for call in calls)


def test_create_refuses_when_cluster_inventory_fails(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path)

    def fake_run(args, **kwargs):
        raise RuntimeError("k3d unavailable")

    monkeypatch.setattr(local_service, "run", fake_run)

    with pytest.raises(RuntimeError, match="k3d unavailable"):
        create_local_cluster(ctx)


def test_create_records_ownership_only_after_cluster_success(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if list(args)[:4] == ["k3d", "cluster", "list", "-o"]:
            return _result(args, stdout="[]")
        return _result(args)

    monkeypatch.setattr(local_service, "run", fake_run)

    result = create_local_cluster(ctx)

    ownership = json.loads(result.ownership.read_text())
    assert ownership["created_by"] == "odooctl"
    assert ownership["cluster_name"] == result.cluster_name
    assert ownership["project_root"] == str(tmp_path)
    assert any(call[:3] == ["k3d", "cluster", "create"] for call in calls)


def test_failed_create_removes_only_newly_observed_exact_cluster(
    tmp_path: Path,
    monkeypatch,
):
    ctx = _context(tmp_path)
    name = local_cluster_name(ctx)
    calls = []
    list_count = 0

    def fake_run(args, **kwargs):
        nonlocal list_count
        command = list(args)
        calls.append(command)
        if command[:3] == ["k3d", "cluster", "list"]:
            list_count += 1
            payload = [] if list_count == 1 else [{"name": name}]
            return _result(command, stdout=json.dumps(payload))
        if command[:3] == ["k3d", "cluster", "create"]:
            raise RuntimeError("create interrupted")
        return _result(command)

    monkeypatch.setattr(local_service, "run", fake_run)

    with pytest.raises(RuntimeError, match="create interrupted"):
        create_local_cluster(ctx)

    assert ["k3d", "cluster", "delete", name] in calls


def test_teardown_deletes_exact_owned_cluster_and_clears_record(
    tmp_path: Path,
    monkeypatch,
):
    ctx = _context(tmp_path)
    result = render_local_simulation(ctx)
    _record_ownership(result)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if list(args)[:4] == ["k3d", "cluster", "list", "-o"]:
            return _result(args, stdout=json.dumps([{"name": result.cluster_name}]))
        return _result(args)

    monkeypatch.setattr(local_service, "run", fake_run)

    deleted = delete_local_cluster(ctx)

    assert deleted == result.cluster_name
    assert ["k3d", "cluster", "delete", result.cluster_name] in calls
    assert not result.ownership.exists()


def test_teardown_refuses_tampered_ownership(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path)
    result = render_local_simulation(ctx)
    _record_ownership(result)
    payload = json.loads(result.ownership.read_text())
    payload["project_root"] = "/different/project"
    result.ownership.write_text(json.dumps(payload))
    calls = []
    monkeypatch.setattr(
        local_service,
        "run",
        lambda args, **kwargs: calls.append(list(args)) or _result(args),
    )

    with pytest.raises(RuntimeError, match="ownership mismatch"):
        delete_local_cluster(ctx)

    assert calls == []


def test_teardown_preserves_ownership_when_cluster_inventory_fails(
    tmp_path: Path,
    monkeypatch,
):
    ctx = _context(tmp_path)
    result = render_local_simulation(ctx)
    _record_ownership(result)
    monkeypatch.setattr(
        local_service,
        "run",
        lambda args, **kwargs: (_ for _ in ()).throw(RuntimeError("k3d unavailable")),
    )

    with pytest.raises(RuntimeError, match="k3d unavailable"):
        delete_local_cluster(ctx)

    assert result.ownership.is_file()


def test_teardown_does_not_require_build_sources_after_creation(
    tmp_path: Path,
    monkeypatch,
):
    ctx = _context(tmp_path)
    result = render_local_simulation(ctx)
    _record_ownership(result)
    (tmp_path / "Dockerfile").unlink()
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return _result(args, stdout=json.dumps([{"name": result.cluster_name}]))

    monkeypatch.setattr(local_service, "run", fake_run)

    assert delete_local_cluster(ctx) == result.cluster_name
    assert ["k3d", "cluster", "delete", result.cluster_name] in calls


def test_smoke_exercises_deploy_neutralize_backup_restore_and_rollback(
    tmp_path: Path,
    monkeypatch,
):
    ctx = _context(tmp_path)
    rendered = render_local_simulation(ctx)
    _record_ownership(rendered)
    calls: list[list[str]] = []
    captures: list[list[str]] = []
    pipes: list[list[str]] = []

    def fake_run(args, **kwargs):
        command = list(args)
        calls.append(command)
        if "--timeout=15s" in command:
            return _result(command, returncode=1, stderr="rollout failed")
        return _result(command)

    def fake_capture(args, *, stdout_path, **kwargs):
        captures.append(list(args))
        Path(stdout_path).write_bytes(b"PGDMP")
        return _result(args)

    def fake_pipe(args, *, stdin_path, **kwargs):
        pipes.append(list(args))
        assert Path(stdin_path).read_bytes() == b"PGDMP"
        return _result(args)

    monkeypatch.setattr(local_service, "run", fake_run)
    monkeypatch.setattr(local_service, "run_capture_bytes", fake_capture)
    monkeypatch.setattr(local_service, "run_pipe_stdin", fake_pipe)

    result = run_local_smoke(ctx)

    assert result.rollback_verified is True
    assert result.backup_path.read_bytes() == b"PGDMP"
    assert any(call[:2] == ["docker", "build"] for call in calls)
    assert any(call[:3] == ["k3d", "image", "import"] for call in calls)
    assert any("apply" in call and str(rendered.resources) in call for call in calls)
    neutralize = next(call for call in calls if "neutralize" in call)
    assert "acme_development" in neutralize
    init_index = next(index for index, call in enumerate(calls) if "-i" in call)
    restart_index = next(
        index
        for index, call in enumerate(calls)
        if call[-2:] == ["restart", "deployment/odoo"]
    )
    ready_index = next(
        index
        for index, call in enumerate(calls)
        if call[-3:-1] == ["status", "deployment/odoo"]
        and "--timeout=30s" in call
    )
    assert init_index < restart_index < ready_index
    assert captures and "pg_dump" in captures[0]
    assert pipes and "pg_restore" in pipes[0]
    assert any(
        any("invalid.local/odooctl-smoke:missing" in arg for arg in call)
        for call in calls
    )
    assert any(call[-2:] == ["undo", "deployment/odoo"] for call in calls)
    assert not any(call[:3] == ["k3d", "cluster", "delete"] for call in calls)


def test_local_down_cli_requires_explicit_yes(tmp_path: Path):
    ctx = _context(tmp_path)

    result = CliRunner().invoke(
        app,
        ["local", "down", "--config", str(ctx.project.config_path)],
    )

    assert result.exit_code != 0
    assert "local down requires --yes" in strip_ansi(result.output)
