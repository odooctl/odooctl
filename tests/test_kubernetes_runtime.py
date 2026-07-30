from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from odooctl.adapters import kubernetes as kubernetes_module
from odooctl.adapters.kubernetes import (
    COMPONENT_LABEL,
    ENVIRONMENT_LABEL,
    MANAGED_BY,
    MANAGED_BY_LABEL,
    PROJECT_LABEL,
    KubernetesAdapter,
    render_kubernetes_resources,
)
from odooctl.adapters.runtime import RuntimeAdapter, make_runtime_adapter
from odooctl.config import load_config
from odooctl.context import ProjectContext
from odooctl.utils.shell import CommandResult


CONFIG = """\
project:
  name: Acme ERP
  odoo_version: "19.0"
runtime:
  type: kubernetes
  context: production
  namespace_template: "{project}-{environment}"
  manifests_path: build/kubernetes
  replicas: 2
  postgres_mode: external
  secret_refs:
    PGPASSWORD:
      name: odoo-database
      key: password
postgres:
  host: postgres.example.internal
  user: odoo
  password_env: ODOO_DB_PASSWORD
odoo:
  image: registry.example.com/odoo:19
  service: odoo
  filestore_container_path: /var/lib/odoo
environments:
  production:
    branch: main
    domain: erp.example.com
    db_name: acme_production
    filestore_path: acme_production
    filestore_volume: odoo-filestore
"""


def _context(tmp_path: Path) -> ProjectContext:
    config = tmp_path / "odooctl.yml"
    config.write_text(CONFIG)
    return ProjectContext.from_config_path(config)


def _result(args: list[str], returncode: int = 0, stdout: str = "", stderr: str = ""):
    return CommandResult(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def _owned_resource() -> str:
    return json.dumps(
        {
            "metadata": {
                "labels": {
                    MANAGED_BY_LABEL: MANAGED_BY,
                    PROJECT_LABEL: "acme-erp",
                    ENVIRONMENT_LABEL: "production",
                    COMPONENT_LABEL: "odoo",
                }
            }
        }
    )


def test_kubernetes_config_is_discriminated_and_defaults_to_external_postgres(tmp_path: Path):
    cfg = load_config((_context(tmp_path)).config_path)

    assert cfg.runtime.type == "kubernetes"
    assert cfg.runtime.postgres_mode == "external"
    assert cfg.runtime.execution_mode == "host"


def test_shipped_kubernetes_example_validates():
    root = Path(__file__).parents[1]

    cfg = load_config(root / "examples/kubernetes/odooctl.yml")

    assert cfg.runtime.type == "kubernetes"
    assert cfg.is_protected("production")


def test_kubernetes_config_rejects_unsafe_manifest_path(tmp_path: Path):
    config = tmp_path / "odooctl.yml"
    config.write_text(CONFIG.replace("build/kubernetes", "../../outside"))

    with pytest.raises(ValidationError, match="project-relative"):
        load_config(config)


def test_kubernetes_factory_requires_and_scopes_environment(tmp_path: Path):
    context = _context(tmp_path)

    with pytest.raises(ValueError, match="requires an environment"):
        make_runtime_adapter(context)

    runtime = make_runtime_adapter(context, environment="production")

    assert isinstance(runtime, KubernetesAdapter)
    assert isinstance(runtime, RuntimeAdapter)
    assert runtime.namespace == "acme-erp-production"


def test_renderer_labels_every_resource_and_uses_secret_references(tmp_path: Path):
    context = _context(tmp_path)

    resources = render_kubernetes_resources(context, "production")

    assert {resource["kind"] for resource in resources} == {
        "Namespace",
        "PersistentVolumeClaim",
        "Deployment",
        "Service",
        "Ingress",
    }
    for resource in resources:
        labels = resource["metadata"]["labels"]
        assert labels[MANAGED_BY_LABEL] == MANAGED_BY
        assert labels[PROJECT_LABEL] == "acme-erp"
        assert labels[ENVIRONMENT_LABEL] == "production"
    deployment = next(item for item in resources if item["kind"] == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "registry.example.com/odoo:19"
    assert container["env"] == [
        {
            "name": "PGPASSWORD",
            "valueFrom": {
                "secretKeyRef": {"name": "odoo-database", "key": "password"}
            },
        }
    ]
    assert "super-secret-value" not in yaml.safe_dump_all(resources)


def test_apply_checks_ownership_then_uses_rendered_manifest(tmp_path: Path, monkeypatch):
    context = _context(tmp_path)
    runtime = KubernetesAdapter(context, "production")
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        command = list(args)
        calls.append(command)
        if "get" in command:
            return _result(command, returncode=1, stderr="NotFound")
        return _result(command)

    monkeypatch.setattr(kubernetes_module, "run", fake_run)

    runtime.up("odoo")

    assert sum("get" in call for call in calls) == 5
    apply = next(call for call in calls if "apply" in call)
    manifest = Path(apply[-1])
    assert manifest == tmp_path / "build/kubernetes/production/resources.yaml"
    assert manifest.is_file()
    assert any(call[-2:] == ["status", "deployment/odoo"] for call in calls)


def test_apply_refuses_an_existing_unowned_resource(tmp_path: Path, monkeypatch):
    context = _context(tmp_path)
    runtime = KubernetesAdapter(context, "production")
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        command = list(args)
        calls.append(command)
        foreign = json.dumps(
            {
                "metadata": {
                    "labels": {
                        MANAGED_BY_LABEL: "someone-else",
                        PROJECT_LABEL: "acme-erp",
                        ENVIRONMENT_LABEL: "production",
                        COMPONENT_LABEL: "odoo",
                    }
                }
            }
        )
        return _result(command, stdout=foreign)

    monkeypatch.setattr(kubernetes_module, "run", fake_run)

    with pytest.raises(RuntimeError, match="Refusing to mutate unowned"):
        runtime.up()

    assert len(calls) == 1
    assert "apply" not in calls[0]


def test_exec_uses_pod_secret_reference_without_secret_on_argv(
    tmp_path: Path,
    monkeypatch,
):
    context = _context(tmp_path)
    runtime = KubernetesAdapter(context, "production")
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        command = list(args)
        calls.append(command)
        if "get" in command:
            return _result(command, stdout=_owned_resource())
        return _result(command)

    monkeypatch.setattr(kubernetes_module, "run", fake_run)

    runtime.exec(
        "odoo",
        ["odoo", "neutralize", "-d", "acme_production"],
        extra_env={"PGPASSWORD": "super-secret-value"},
    )

    exec_command = calls[-1]
    assert "super-secret-value" not in exec_command
    assert "PGPASSWORD" not in exec_command
    assert exec_command[-5:] == [
        "--",
        "odoo",
        "neutralize",
        "-d",
        "acme_production",
    ]


def test_exec_rejects_unmapped_local_environment_values(tmp_path: Path, monkeypatch):
    context = _context(tmp_path)
    runtime = KubernetesAdapter(context, "production")
    monkeypatch.setattr(
        kubernetes_module,
        "run",
        lambda args, **kwargs: _result(list(args), stdout=_owned_resource()),
    )

    with pytest.raises(RuntimeError, match="Secret references: API_TOKEN"):
        runtime.exec("odoo", ["true"], extra_env={"API_TOKEN": "do-not-leak"})


def test_status_normalizes_owned_workloads_and_external_postgres(
    tmp_path: Path,
    monkeypatch,
):
    context = _context(tmp_path)
    runtime = KubernetesAdapter(context, "production")
    payload = json.dumps(
        {
            "items": [
                {
                    "metadata": {
                        "name": "odoo",
                        "labels": json.loads(_owned_resource())["metadata"]["labels"],
                    },
                    "spec": {"replicas": 2},
                    "status": {"availableReplicas": 2},
                }
            ]
        }
    )
    monkeypatch.setattr(
        kubernetes_module,
        "run",
        lambda args, **kwargs: _result(list(args), stdout=payload),
    )

    assert runtime.ps() == "odoo running\npostgres running external"


def test_namespace_delete_is_ownership_guarded(tmp_path: Path, monkeypatch):
    context = _context(tmp_path)
    runtime = KubernetesAdapter(context, "production")
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        command = list(args)
        calls.append(command)
        return _result(command, stdout=_owned_resource())

    monkeypatch.setattr(kubernetes_module, "run", fake_run)

    runtime.delete()

    assert "get" in calls[0]
    assert calls[1][-3:] == ["delete", "namespace", "acme-erp-production"]
