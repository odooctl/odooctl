from __future__ import annotations

from pathlib import Path

import pytest

from odooctl.adapters.docker_compose import DockerComposeAdapter
from odooctl.adapters.runtime import RuntimeAdapter, make_runtime_adapter, validate_runtime_definition
from odooctl.context import ProjectContext


CONFIG = """\
project:
  name: runtime-demo
  odoo_version: "19.0"
runtime:
  type: docker_compose
  compose_file: deploy/compose.yml
odoo:
  image: odoo:19
environments:
  staging:
    branch: staging
    domain: staging.example.com
    db_name: runtime_staging
    filestore_path: data/staging
"""


def _context(tmp_path: Path) -> ProjectContext:
    config = tmp_path / "odooctl.yml"
    config.write_text(CONFIG)
    return ProjectContext.from_config_path(config)


def test_runtime_factory_preserves_compose_as_default(tmp_path: Path):
    context = _context(tmp_path)

    runtime = make_runtime_adapter(context)

    assert isinstance(runtime, DockerComposeAdapter)
    assert isinstance(runtime, RuntimeAdapter)
    assert runtime.runtime_type == "docker_compose"
    assert runtime.compose_file == "deploy/compose.yml"
    assert runtime.project_dir == str(tmp_path)


def test_runtime_factory_supports_dependency_injection(tmp_path: Path):
    context = _context(tmp_path)

    class FakeCompose:
        def __init__(self, compose_file: str, project_dir: str):
            self.compose_file = compose_file
            self.project_dir = project_dir

    runtime = make_runtime_adapter(context, compose_adapter_cls=FakeCompose)

    assert runtime.compose_file == "deploy/compose.yml"
    assert runtime.project_dir == str(tmp_path)


def test_runtime_definition_fails_before_missing_compose_mutation(tmp_path: Path):
    context = _context(tmp_path)

    with pytest.raises(FileNotFoundError, match="Compose file not found"):
        validate_runtime_definition(context)


def test_runtime_definition_accepts_project_relative_compose(tmp_path: Path):
    context = _context(tmp_path)
    compose = tmp_path / "deploy/compose.yml"
    compose.parent.mkdir()
    compose.write_text("services: {}\n")

    validate_runtime_definition(context)


def test_services_do_not_construct_compose_adapters_directly():
    services = Path(__file__).parents[1] / "odooctl/services"

    offenders = [
        path.name
        for path in services.glob("*.py")
        if "DockerComposeAdapter(" in path.read_text()
    ]

    assert offenders == []
