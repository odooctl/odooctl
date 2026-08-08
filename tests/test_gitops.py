from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from conftest import strip_ansi
from odooctl.config import load_config
from odooctl.main import app
from odooctl.services import gitops as gitops_service
from odooctl.services.context import ServiceContext
from odooctl.services.gitops import (
    EXPIRES_AT_ANNOTATION,
    cleanup_expired_previews,
    preview_id,
    render_environment_overlay,
    render_preview_overlay,
)


CONFIG = """\
project:
  name: Acme ERP
  odoo_version: "19.0"
runtime:
  type: kubernetes
  context: production
  namespace_template: "{project}-{environment}"
  secret_refs:
    PGPASSWORD:
      name: odoo-database
      key: password
gitops:
  enabled: true
  output_path: deploy/gitops
  preview_base_domain: preview.example.com
  preview_source_environment: staging
  preview_ttl_hours: 24
  initializer_image: registry.example.com/odooctl:latest
  preview_image_template: "registry.example.com/odoo:pr-{revision}"
postgres:
  host: postgres.example.internal
  password_env: ODOO_DB_PASSWORD
odoo:
  image: registry.example.com/odoo:19
  service: odoo
environments:
  production:
    tier: production
    branch: main
    domain: erp.example.com
    db_name: acme_production
    filestore_path: acme_production
    filestore_volume: production-filestore
  staging:
    tier: staging
    branch: staging
    domain: staging.example.com
    db_name: acme_staging
    filestore_path: acme_staging
    filestore_volume: staging-filestore
    clone_from: production
    sanitize: true
"""


def _context(tmp_path: Path) -> ServiceContext:
    config = tmp_path / "odooctl.yml"
    config.write_text(CONFIG)
    return ServiceContext.from_config_path(config)


def test_gitops_requires_kubernetes_runtime(tmp_path: Path):
    config = tmp_path / "odooctl.yml"
    config.write_text(
        CONFIG.replace("type: kubernetes", "type: docker_compose")
        .replace("  context: production\n", "  compose_file: docker-compose.yml\n")
        .replace(
            "  namespace_template: \"{project}-{environment}\"\n"
            "  secret_refs:\n"
            "    PGPASSWORD:\n"
            "      name: odoo-database\n"
            "      key: password\n",
            "",
        )
    )

    with pytest.raises(ValidationError, match="requires runtime.type: kubernetes"):
        load_config(config)


def test_preview_identity_is_deterministic_and_validated():
    assert preview_id(42, "ABCDEF0123456789") == "pr-42-abcdef01"
    assert preview_id(42, "abcdef0122222222") == "pr-42-abcdef01"
    with pytest.raises(ValueError, match="positive"):
        preview_id(0, "abcdef01")
    with pytest.raises(ValueError, match="hexadecimal"):
        preview_id(42, "not-a-commit")


def test_environment_overlay_renders_without_cluster_apply(tmp_path: Path):
    ctx = _context(tmp_path)

    result = render_environment_overlay(ctx, "production")

    assert result.directory == tmp_path / "deploy/gitops/environments/production"
    resources = list(yaml.safe_load_all(result.resources.read_text()))
    assert {item["kind"] for item in resources} == {
        "Namespace",
        "PersistentVolumeClaim",
        "Deployment",
        "Service",
        "Ingress",
    }
    kustomization = yaml.safe_load(result.kustomization.read_text())
    assert kustomization["resources"] == ["resources.yaml"]


def test_preview_overlay_is_isolated_neutralized_and_expiring(
    tmp_path: Path,
    monkeypatch,
):
    ctx = _context(tmp_path)
    monkeypatch.setenv("ODOO_DB_PASSWORD", "local-value-must-not-render")
    now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)

    result = render_preview_overlay(
        ctx,
        123,
        "abcdef0123456789",
        now=now,
    )

    assert result.environment == "pr-123-abcdef01"
    resources = list(yaml.safe_load_all(result.resources.read_text()))
    namespace = next(item for item in resources if item["kind"] == "Namespace")
    assert namespace["metadata"]["name"] == "acme-erp-pr-123-abcdef01"
    assert namespace["metadata"]["labels"]["odooctl.dev/ephemeral"] == "true"
    assert namespace["metadata"]["annotations"][EXPIRES_AT_ANNOTATION] == (
        "2026-08-01T12:00:00+00:00"
    )
    ingress = next(item for item in resources if item["kind"] == "Ingress")
    assert (
        ingress["spec"]["rules"][0]["host"]
        == "pr-123-abcdef01.preview.example.com"
    )
    deployment = next(item for item in resources if item["kind"] == "Deployment")
    assert deployment["spec"]["template"]["spec"]["containers"][0]["image"] == (
        "registry.example.com/odoo:pr-abcdef0123456789"
    )
    job = next(item for item in resources if item["kind"] == "Job")
    args = job["spec"]["template"]["spec"]["containers"][0]["args"]
    assert args[:3] == ["clone", "staging", "pr-123-abcdef01"]
    assert "--sanitize" in args
    config_map = next(item for item in resources if item["kind"] == "ConfigMap")
    generated_config = config_map["data"]["odooctl.yml"]
    assert "local-value-must-not-render" not in generated_config
    assert "context: production" not in generated_config
    assert "clone_from: staging" in generated_config
    metadata = yaml.safe_load(result.metadata.read_text())
    assert metadata["spec"] == {
        "source_environment": "staging",
        "domain": "pr-123-abcdef01.preview.example.com",
        "database": "acme_erp_pr_123_abcdef01",
        "filestore": "acme_erp_pr_123_abcdef01",
        "sanitize": True,
        "native_neutralization": "preferred",
    }


def test_preview_output_cannot_escape_project(tmp_path: Path):
    ctx = _context(tmp_path)

    with pytest.raises(ValueError, match="inside the project"):
        render_preview_overlay(
            ctx,
            1,
            "abcdef01",
            output=tmp_path.parent / "outside",
        )


def test_cleanup_plans_then_deletes_only_expired_preview_identities(
    tmp_path: Path,
    monkeypatch,
):
    ctx = _context(tmp_path)
    base = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    expired = render_preview_overlay(ctx, 10, "aaaaaaaa", ttl_hours=1, now=base)
    active = render_preview_overlay(ctx, 11, "bbbbbbbb", ttl_hours=24, now=base)
    assert expired.metadata and active.metadata
    deleted: list[str] = []

    class FakeRuntime:
        def __init__(self, project, environment):
            self.environment = environment

        def delete(self):
            deleted.append(self.environment)

    monkeypatch.setattr(gitops_service, "KubernetesAdapter", FakeRuntime)
    current = datetime(2026, 7, 31, 14, 0, tzinfo=timezone.utc)

    plan = cleanup_expired_previews(ctx, now=current)
    applied = cleanup_expired_previews(ctx, now=current, apply=True)

    assert plan.expired == ("pr-10-aaaaaaaa",)
    assert plan.deleted == ()
    assert applied.deleted == ("pr-10-aaaaaaaa",)
    assert deleted == ["pr-10-aaaaaaaa"]


def test_cleanup_cli_requires_explicit_yes(tmp_path: Path):
    config = tmp_path / "odooctl.yml"
    config.write_text(CONFIG)

    result = CliRunner().invoke(
        app,
        ["gitops", "cleanup", "--config", str(config), "--apply"],
    )

    assert result.exit_code != 0
    assert "--apply requires --yes" in strip_ansi(result.output)
