"""Tests for fail-closed historical documentation backports."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_versioned_docs", ROOT / "scripts/build_versioned_docs.py"
)
assert SPEC and SPEC.loader
build_versioned_docs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_versioned_docs)


def _write_backport(tmp_path: Path, replacement: str) -> tuple[Path, Path]:
    source = tmp_path / "source"
    support = tmp_path / "support"
    (source / "docs").mkdir(parents=True)
    (source / "docs" / "installation.md").write_text("before\nold text\nafter\n")
    patch_root = support / "docs-version-patches" / "1.2.3"
    patch_root.mkdir(parents=True)
    (patch_root / "replacements.yml").write_text(replacement)
    return source, support


def test_historical_backport_replaces_exact_text_and_preserves_page(tmp_path: Path) -> None:
    source, support = _write_backport(
        tmp_path,
        "- path: installation.md\n  old: old text\n  new: corrected text\n",
    )

    build_versioned_docs._apply_backport(source, "1.2.3", support)

    assert (source / "docs" / "installation.md").read_text() == (
        "before\ncorrected text\nafter\n"
    )


def test_historical_backport_fails_when_tagged_text_does_not_match(tmp_path: Path) -> None:
    source, support = _write_backport(
        tmp_path,
        "- path: installation.md\n  old: missing text\n  new: corrected text\n",
    )

    with pytest.raises(ValueError, match="expected one match"):
        build_versioned_docs._apply_backport(source, "1.2.3", support)


def test_historical_backport_rejects_parent_path(tmp_path: Path) -> None:
    source, support = _write_backport(
        tmp_path,
        "- path: ../outside.md\n  old: secret\n  new: exposed\n",
    )

    with pytest.raises(ValueError, match="unsafe path"):
        build_versioned_docs._apply_backport(source, "1.2.3", support)


def test_build_rejects_source_version_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')

    with pytest.raises(ValueError, match="declares package version"):
        build_versioned_docs.build_one(
            source,
            tmp_path / "site",
            version="1.2.4",
            channel="stable",
            ref="v1.2.4",
            commit="abc123",
            assets_dir=tmp_path / "assets",
            apply_backports=True,
        )
