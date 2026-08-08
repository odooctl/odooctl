from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from odooctl.adapters.kubernetes import (
    COMPONENT_LABEL,
    REVISION_LABEL,
    KubernetesAdapter,
)
from odooctl.adapters.runtime import RolloutState
from odooctl.config import load_config
from odooctl.services import deploy as deploy_service
from odooctl.services.context import ServiceContext
from odooctl.utils.shell import CommandResult


CONFIG = """\
project:
  name: acme
  odoo_version: "19.0"
runtime:
  type: kubernetes
  namespace_template: "{project}-{environment}"
  canary_provider: nginx
  secret_refs:
    PGPASSWORD:
      name: odoo-database
      key: password
postgres:
  host: postgres.example.internal
  password_env: ODOO_DB_PASSWORD
odoo:
  image: registry.example.com/odoo:19
  service: odoo
environments:
  staging:
    branch: staging
    domain: staging.example.com
    db_name: acme_staging
    filestore_path: acme_staging
    filestore_volume: staging-filestore
    rollout_strategy: blue_green
    update_modules: [sale]
"""


def _context(tmp_path: Path, config_text: str = CONFIG) -> ServiceContext:
    config = tmp_path / "odooctl.yml"
    config.write_text(config_text)
    return ServiceContext.from_config_path(config)


def test_compose_rejects_unsupported_progressive_strategy(tmp_path: Path):
    config = tmp_path / "odooctl.yml"
    config.write_text(
        CONFIG.replace(
            "type: kubernetes\n"
            "  namespace_template: \"{project}-{environment}\"\n"
            "  canary_provider: nginx\n"
            "  secret_refs:\n"
            "    PGPASSWORD:\n"
            "      name: odoo-database\n"
            "      key: password",
            "type: docker_compose\n  compose_file: docker-compose.yml",
        )
    )

    with pytest.raises(ValidationError, match="not supported by docker_compose"):
        load_config(config)


def test_canary_requires_configured_traffic_provider(tmp_path: Path):
    config = tmp_path / "odooctl.yml"
    config.write_text(
        CONFIG.replace("canary_provider: nginx", "canary_provider: none").replace(
            "rollout_strategy: blue_green",
            "rollout_strategy: canary",
        )
    )

    with pytest.raises(ValidationError, match="canary rollout requires"):
        load_config(config)


def test_kubernetes_candidate_keeps_stable_selector_isolated(tmp_path: Path):
    runtime = KubernetesAdapter(_context(tmp_path).project, "staging")

    candidate, selector, resources = runtime._candidate_resources(
        "odoo",
        "abcdef0123456789",
        "blue_green",
        10,
    )

    assert candidate == "odoo-abcdef01"
    assert selector[COMPONENT_LABEL] == "odoo-candidate"
    assert selector[REVISION_LABEL] == "abcdef0123456789"
    deployment = resources[0]
    assert deployment["metadata"]["name"] == candidate
    assert deployment["spec"]["strategy"]["type"] == "RollingUpdate"
    assert deployment["spec"]["selector"]["matchLabels"] == selector
    assert len(resources) == 2


def test_kubernetes_canary_renders_weighted_nginx_ingress(tmp_path: Path):
    runtime = KubernetesAdapter(_context(tmp_path).project, "staging")

    candidate, _, resources = runtime._candidate_resources(
        "odoo",
        "abcdef0123456789",
        "canary",
        15,
    )

    ingress = resources[-1]
    assert ingress["kind"] == "Ingress"
    assert ingress["metadata"]["name"] == candidate
    assert ingress["metadata"]["annotations"][
        "nginx.ingress.kubernetes.io/canary"
    ] == "true"
    assert ingress["metadata"]["annotations"][
        "nginx.ingress.kubernetes.io/canary-weight"
    ] == "15"
    backend = ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]
    assert backend["service"]["name"] == candidate


def test_blue_green_promotion_and_rollback_preserve_stable_workload(
    tmp_path: Path,
    monkeypatch,
):
    from odooctl.adapters import kubernetes as kubernetes_module

    runtime = KubernetesAdapter(_context(tmp_path).project, "staging")
    calls: list[list[str]] = []
    candidate_applied = False
    stable_payload = json.dumps(
        {
            "metadata": {"labels": runtime._expected_labels},
            "spec": {
                "selector": {
                    "odooctl.dev/project": "acme",
                    "odooctl.dev/environment": "staging",
                    COMPONENT_LABEL: "odoo",
                }
            },
        }
    )
    candidate_labels = {
        "app.kubernetes.io/managed-by": "odooctl",
        "odooctl.dev/project": "acme",
        "odooctl.dev/environment": "staging",
        COMPONENT_LABEL: "odoo-candidate",
        REVISION_LABEL: "abcdef0123456789",
    }
    candidate_payload = json.dumps({"metadata": {"labels": candidate_labels}})

    def fake_run(args, **kwargs):
        nonlocal candidate_applied
        command = list(args)
        calls.append(command)
        if "get" in command:
            if "odoo-abcdef01" in command:
                if candidate_applied:
                    return CommandResult(command, 0, candidate_payload, "")
                return CommandResult(command, 1, "", "NotFound")
            return CommandResult(command, 0, stable_payload, "")
        if "apply" in command:
            candidate_applied = True
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(kubernetes_module, "run", fake_run)

    state = runtime.begin_rollout(
        "odoo",
        strategy="blue_green",
        revision="abcdef0123456789",
    )
    runtime.promote_rollout(state)
    runtime.rollback_rollout(state)

    patches = [call for call in calls if "patch" in call]
    assert len(patches) == 2
    assert "odoo-candidate" in patches[0][-1]
    assert '"app.kubernetes.io/component":"odoo"' in patches[1][-1]
    deletes = [call for call in calls if "delete" in call]
    assert deletes
    assert all("odoo-abcdef01" in call for call in deletes)
    assert not any(call[-1] == "odoo" for call in deletes)


class FakeProgressiveRuntime:
    runtime_type = "kubernetes"

    def __init__(self, events: list[tuple]):
        self.events = events

    def supports_rollout(self, strategy):
        return strategy in {"recreate", "rolling", "blue_green", "canary"}

    def begin_rollout(self, workload, *, strategy, revision, canary_percent):
        self.events.append(("begin", workload, strategy, revision, canary_percent))
        return RolloutState(
            strategy=strategy,
            workload=workload,
            command_workload="odoo-abcdef01",
            candidate_workload="odoo-abcdef01",
        )

    def promote_rollout(self, state):
        state.promoted = True
        self.events.append(("promote", state.command_workload))

    def finalize_rollout(self, state):
        self.events.append(("finalize", state.command_workload))

    def rollback_rollout(self, state):
        self.events.append(("rollback", state.command_workload))


class DummyPostgres:
    def __init__(self, config):
        self.config = config

    def ping(self, db_name):
        return None


class DummyStore:
    def __init__(self):
        self.saved = []

    def save_deployment(self, metadata):
        self.saved.append(metadata)


def _patch_deploy(
    monkeypatch,
    runtime: FakeProgressiveRuntime,
    store: DummyStore,
    events: list[tuple],
):
    monkeypatch.setenv("ODOO_DB_PASSWORD", "test-password")
    monkeypatch.setattr(deploy_service, "PostgresAdapter", DummyPostgres)
    monkeypatch.setattr(deploy_service, "make_runtime_adapter", lambda *a, **k: runtime)
    monkeypatch.setattr(deploy_service, "_assert_clean_worktree", lambda *a, **k: None)
    monkeypatch.setattr(deploy_service, "git_commit", lambda *a, **k: "abcdef0123456789")
    monkeypatch.setattr(deploy_service, "run", lambda *a, **k: None)
    monkeypatch.setattr(deploy_service, "MetadataStore", lambda *a, **k: store)
    monkeypatch.setattr(
        deploy_service,
        "update_modules_compose",
        lambda _, workload, db, modules, **kwargs: events.append(
            ("modules", workload, db, tuple(modules))
        ),
    )


def test_progressive_deploy_promotes_then_finalizes_after_health(
    tmp_path: Path,
    monkeypatch,
):
    ctx = _context(tmp_path)
    events: list[tuple] = []
    runtime = FakeProgressiveRuntime(events)
    store = DummyStore()
    _patch_deploy(monkeypatch, runtime, store, events)
    monkeypatch.setattr(
        deploy_service,
        "check_url",
        lambda *a, **k: events.append(("health",)),
    )

    result = deploy_service.run_deploy(ctx, "staging")

    assert result.status == "success"
    assert [event[0] for event in events] == [
        "begin",
        "modules",
        "promote",
        "health",
        "finalize",
    ]
    assert events[1][1] == "odoo-abcdef01"
    assert store.saved[-1].rollout_strategy == "blue_green"


def test_unprotected_progressive_deploy_automatically_rolls_back_on_health_failure(
    tmp_path: Path,
    monkeypatch,
):
    ctx = _context(tmp_path)
    events: list[tuple] = []
    runtime = FakeProgressiveRuntime(events)
    store = DummyStore()
    _patch_deploy(monkeypatch, runtime, store, events)
    monkeypatch.setattr(
        deploy_service,
        "check_url",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("health failed")),
    )

    with pytest.raises(RuntimeError, match="health failed"):
        deploy_service.run_deploy(ctx, "staging")

    assert events[-1] == ("rollback", "odoo-abcdef01")
    assert store.saved[-1].rollout_rollback == "success"
    assert "blue_green workload rollback complete" in store.saved[-1].message


def test_kubernetes_rolling_rollback_uses_native_undo(tmp_path: Path, monkeypatch):
    from odooctl.adapters import kubernetes as kubernetes_module

    runtime = KubernetesAdapter(_context(tmp_path).project, "staging")
    calls: list[list[str]] = []
    owned = json.dumps(
        {
            "metadata": {
                "labels": runtime._expected_labels,
            }
        }
    )

    def fake_run(args, **kwargs):
        command = list(args)
        calls.append(command)
        return CommandResult(command, 0, owned if "get" in command else "", "")

    monkeypatch.setattr(kubernetes_module, "run", fake_run)
    state = RolloutState(
        strategy="rolling",
        workload="odoo",
        command_workload="odoo",
    )

    runtime.rollback_rollout(state)

    assert any(call[-2:] == ["undo", "deployment/odoo"] for call in calls)
    assert any(call[-2:] == ["status", "deployment/odoo"] for call in calls)
