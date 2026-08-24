"""Machine-local config overlay (odooctl.local.yml) — plan §4.

The overlay is an untracked sibling of the main config, deep-merged over it
at load time. Precedence: env vars (*_env indirections, read at runtime)
> overlay > main config.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import click
import pytest

from odooctl.commands import validate as validate_cmd
from odooctl.commands.init import ensure_overlay_gitignored
from odooctl.config import deep_merge, load_config, local_overlay_path
from odooctl.context import ProjectContext


BASE_CONFIG = """project:
  name: demo
  odoo_version: "19.0"
odoo:
  image: registry/odoo:19.0
environments:
  production:
    branch: main
    domain: odoo.example.com
    db_name: odoo_prod
    filestore_path: /var/lib/odoo/filestore/odoo_prod
    update_modules:
      - sale
      - stock
"""


def _write_project(tmp_path: Path, overlay: str | None = None, name: str = "odooctl.yml") -> Path:
    config = tmp_path / name
    config.write_text(BASE_CONFIG)
    if overlay is not None:
        stem = name.rsplit(".", 1)[0]
        (tmp_path / f"{stem}.local.yml").write_text(overlay)
    return config


# --- local_overlay_path -----------------------------------------------------

def test_overlay_path_derives_from_config_name():
    assert local_overlay_path("odooctl.yml") == Path("odooctl.local.yml")
    assert local_overlay_path(Path("/x/custom.yaml")) == Path("/x/custom.local.yaml")


def test_overlay_path_of_an_overlay_is_none():
    assert local_overlay_path("odooctl.local.yml") is None


# --- deep_merge -------------------------------------------------------------

def test_deep_merge_merges_nested_mappings_and_replaces_scalars():
    base = {"a": {"x": 1, "y": 2}, "b": 1}
    overlay = {"a": {"y": 3}, "b": 2}
    assert deep_merge(base, overlay) == {"a": {"x": 1, "y": 3}, "b": 2}


def test_deep_merge_lists_replace_wholesale():
    assert deep_merge({"a": [1, 2]}, {"a": [3]}) == {"a": [3]}


def test_deep_merge_null_replaces_value():
    assert deep_merge({"a": {"x": 1}}, {"a": None}) == {"a": None}


def test_deep_merge_does_not_mutate_inputs():
    base = {"a": {"x": 1}}
    overlay = {"a": {"y": 2}}
    deep_merge(base, overlay)
    assert base == {"a": {"x": 1}}
    assert overlay == {"a": {"y": 2}}


# --- load_config with overlay ----------------------------------------------

def test_load_config_without_overlay_is_unchanged(tmp_path: Path):
    config = _write_project(tmp_path)
    cfg = load_config(config)
    assert cfg.env("production").scheme == "https"
    assert cfg.env("production").port is None


def test_overlay_overrides_and_deep_merges(tmp_path: Path):
    config = _write_project(
        tmp_path,
        overlay="""environments:
  production:
    scheme: http
    port: 8069
""",
    )
    cfg = load_config(config)
    env = cfg.env("production")
    assert env.scheme == "http"
    assert env.port == 8069
    # Sibling keys from the main config survive the merge.
    assert env.db_name == "odoo_prod"
    assert cfg.project.name == "demo"


def test_overlay_lists_replace_not_append(tmp_path: Path):
    config = _write_project(
        tmp_path,
        overlay="""environments:
  production:
    update_modules:
      - custom_module
""",
    )
    cfg = load_config(config)
    assert cfg.env("production").update_modules == ["custom_module"]


def test_empty_overlay_is_a_no_op(tmp_path: Path):
    config = _write_project(tmp_path, overlay="")
    cfg = load_config(config)
    assert cfg.project.name == "demo"


def test_custom_config_name_uses_matching_overlay(tmp_path: Path):
    config = _write_project(
        tmp_path,
        overlay="project:\n  name: local-demo\n",
        name="custom.yml",
    )
    assert (tmp_path / "custom.local.yml").exists()
    cfg = load_config(config)
    assert cfg.project.name == "local-demo"


def test_loading_the_overlay_itself_does_not_recurse(tmp_path: Path):
    # A full config stored as *.local.yml loads standalone; no *.local.local.yml lookup.
    config = _write_project(tmp_path, name="odooctl.local.yml")
    cfg = load_config(config)
    assert cfg.project.name == "demo"


def test_non_mapping_overlay_is_rejected(tmp_path: Path):
    config = _write_project(tmp_path, overlay="- just\n- a\n- list\n")
    with pytest.raises(click.ClickException, match="YAML mapping"):
        load_config(config)


def test_invalid_merged_config_error_mentions_overlay(tmp_path: Path):
    config = _write_project(
        tmp_path,
        overlay="""environments:
  production:
    domain: "not a hostname!"
""",
    )
    with pytest.raises(click.ClickException) as excinfo:
        load_config(config)
    assert "odooctl.local.yml" in str(excinfo.value)
    assert "odooctl.yml" in str(excinfo.value)


# --- ProjectContext ---------------------------------------------------------

def test_project_context_exposes_overlay_path(tmp_path: Path):
    config = _write_project(tmp_path, overlay="project:\n  name: local-demo\n")
    ctx = ProjectContext.from_config_path(config)
    assert ctx.overlay_path == (tmp_path / "odooctl.local.yml").resolve()
    assert ctx.config.project.name == "local-demo"


def test_project_context_overlay_path_none_when_absent(tmp_path: Path):
    config = _write_project(tmp_path)
    ctx = ProjectContext.from_config_path(config)
    assert ctx.overlay_path is None


# --- init/setup gitignore handling ------------------------------------------

def test_ensure_overlay_gitignored_creates_gitignore(tmp_path: Path):
    config = _write_project(tmp_path)
    ensure_overlay_gitignored(config)
    assert "odooctl.local.yml" in (tmp_path / ".gitignore").read_text().splitlines()


def test_ensure_overlay_gitignored_appends_once(tmp_path: Path):
    config = _write_project(tmp_path)
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("backups/\n")
    ensure_overlay_gitignored(config)
    ensure_overlay_gitignored(config)
    lines = gitignore.read_text().splitlines()
    assert lines == ["backups/", "odooctl.local.yml"]


def test_ensure_overlay_gitignored_handles_missing_trailing_newline(tmp_path: Path):
    config = _write_project(tmp_path)
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("backups/")
    ensure_overlay_gitignored(config)
    assert gitignore.read_text() == "backups/\nodooctl.local.yml\n"


# --- validate surfacing -----------------------------------------------------

def _run_validate(monkeypatch, config: Path) -> str:
    lines: list[str] = []
    for fn in ("info", "success", "warn"):
        monkeypatch.setattr(validate_cmd, fn, lambda message: lines.append(message))
    monkeypatch.setenv("ODOO_DB_PASSWORD", "x")
    validate_cmd.execute(str(config))
    return "\n".join(lines)


def test_validate_reports_merged_overlay(monkeypatch, tmp_path: Path):
    config = _write_project(tmp_path, overlay="project:\n  name: local-demo\n")
    output = _run_validate(monkeypatch, config)
    assert "Config valid: local-demo" in output
    assert "Machine-local overlay merged: odooctl.local.yml" in output


def test_validate_silent_about_overlay_when_absent(monkeypatch, tmp_path: Path):
    config = _write_project(tmp_path)
    output = _run_validate(monkeypatch, config)
    assert "overlay" not in output


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_validate_warns_when_overlay_not_gitignored(monkeypatch, tmp_path: Path):
    config = _write_project(tmp_path, overlay="project:\n  name: local-demo\n")
    _git("init", cwd=tmp_path)
    output = _run_validate(monkeypatch, config)
    assert "not gitignored" in output


def test_validate_no_warning_when_overlay_gitignored(monkeypatch, tmp_path: Path):
    config = _write_project(tmp_path, overlay="project:\n  name: local-demo\n")
    _git("init", cwd=tmp_path)
    (tmp_path / ".gitignore").write_text("odooctl.local.yml\n")
    output = _run_validate(monkeypatch, config)
    assert "not gitignored" not in output
    assert "Machine-local overlay merged" in output
