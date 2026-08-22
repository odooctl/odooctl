"""Build retained, versioned MkDocs trees without mutating their sources.

The publisher deliberately checks out each immutable ref into a detached
worktree. It never rebuilds a retained version from ``master``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import yaml


BASE_URL = "https://odooctl.github.io/docs"


def _run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _load_versions(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text())
    if not isinstance(payload, dict) or not isinstance(payload.get("versions"), list):
        raise ValueError(f"{path} must define a versions list")
    return payload


def _stage_docs(source: Path, support_root: Path) -> None:
    docs = source / "docs"
    for name in (
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        "SECURITY.md",
        "LICENSING.md",
        "SUPPORT.md",
        "LICENSE",
    ):
        candidate = source / name
        if not candidate.exists():
            candidate = support_root / name
        if candidate.exists():
            shutil.copy2(candidate, docs / name)
    readme = (source / "README.md").read_text()
    (docs / "index.md").write_text(readme.replace("](docs/", "]("))
    # MkDocs normalizes uppercase Markdown source names inconsistently across
    # its supported versions. Use a lowercase staged copy for the policy page
    # and rewrite the generated source links without changing the tag itself.
    security = docs / "SECURITY.md"
    if security.exists():
        shutil.copy2(security, docs / "security-policy.md")
        for markdown in docs.rglob("*.md"):
            markdown.write_text(markdown.read_text().replace("SECURITY.md", "security-policy.md"))


def _replace_nav_path(value: Any, old: str, new: str) -> Any:
    if isinstance(value, list):
        return [_replace_nav_path(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: _replace_nav_path(item, old, new) for key, item in value.items()}
    return new if value == old else value


def build_one(
    source: Path,
    output: Path,
    *,
    version: str,
    channel: str,
    ref: str,
    commit: str,
    assets_dir: Path,
) -> None:
    """Build one source checkout and fail if its package version is different."""
    package = tomllib.loads((source / "pyproject.toml").read_text())["project"]["version"]
    if package != version:
        raise ValueError(f"{ref} declares package version {package!r}, expected {version!r}")

    with tempfile.TemporaryDirectory(prefix="odooctl-doc-source-") as temp:
        build_source = Path(temp) / "source"
        shutil.copytree(
            source,
            build_source,
            ignore=shutil.ignore_patterns(".git", ".venv", "site", "dist", "build", "__pycache__", "*.pyc"),
        )
        _stage_docs(build_source, assets_dir.parent.parent)
        checker = assets_dir.parent.parent / "scripts" / "check_documented_commands.py"
        _run(
            sys.executable,
            str(checker),
            "--docs-root",
            str(build_source / "docs"),
            "--package-root",
            str(build_source),
        )
        destination_assets = build_source / "docs" / "_versioning"
        shutil.copytree(assets_dir, destination_assets, dirs_exist_ok=True)
        config_path = build_source / "mkdocs.yml"
        config = yaml.safe_load(config_path.read_text())
        config["nav"] = _replace_nav_path(config.get("nav", []), "SECURITY.md", "security-policy.md")
        config["docs_dir"] = str(build_source / "docs")
        config["site_dir"] = str(output)
        config["site_url"] = f"{BASE_URL}/{version if channel != 'dev' else 'dev'}/"
        config["edit_uri"] = f"edit/{ref}/docs/"
        config.setdefault("theme", {})["custom_dir"] = str(destination_assets / "overrides")
        config.setdefault("extra_css", []).append("_versioning/versioning.css")
        config.setdefault("extra_javascript", []).append("_versioning/versioning.js")
        config.setdefault("extra", {}).update(
            {"docs_version": version, "docs_channel": channel, "docs_commit": commit}
        )
        generated = build_source / ".mkdocs-versioned.yml"
        generated.write_text(yaml.safe_dump(config, sort_keys=False))
        _run("mkdocs", "build", "--strict", "--config-file", str(generated))


def build_site(repo_root: Path, site_dir: Path, versions_file: Path, assets_dir: Path) -> dict[str, Any]:
    """Build immutable snapshots plus aliases and a current development tree."""
    config = _load_versions(versions_file)
    site_docs = site_dir / "docs"
    shutil.rmtree(site_docs, ignore_errors=True)
    site_docs.mkdir(parents=True)
    published_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    versions: list[dict[str, str]] = []

    with tempfile.TemporaryDirectory(prefix="odooctl-docs-") as temp:
        worktree_root = Path(temp)
        for item in config["versions"]:
            version, channel, ref = item["version"], item["channel"], item["ref"]
            source = worktree_root / version
            _run("git", "worktree", "add", "--detach", str(source), ref, cwd=repo_root)
            try:
                commit = _run("git", "rev-parse", "HEAD", cwd=source)
                build_one(source, site_docs / version, version=version, channel=channel, ref=ref,
                          commit=commit, assets_dir=assets_dir)
                versions.append({"version": version, "channel": channel, "ref": ref, "commit": commit,
                                 "published_at": published_at, "canonical_url": f"/docs/{version}/"})
            finally:
                _run("git", "worktree", "remove", "--force", str(source), cwd=repo_root)

    dev_version = tomllib.loads((repo_root / "pyproject.toml").read_text())["project"]["version"]
    dev_channel = config["development"]["channel"]
    dev_commit = _run("git", "rev-parse", "HEAD", cwd=repo_root)
    build_one(repo_root, site_docs / dev_channel, version=dev_version, channel=dev_channel,
              ref="master", commit=dev_commit, assets_dir=assets_dir)
    versions.append({"version": dev_version, "channel": dev_channel, "ref": "master", "commit": dev_commit,
                     "published_at": published_at, "canonical_url": f"/docs/{dev_channel}/"})

    aliases = config.get("aliases", {})
    by_version = {entry["version"]: entry for entry in versions}
    for channel, version in aliases.items():
        if version not in by_version:
            raise ValueError(f"alias {channel!r} points to unknown version {version!r}")
        shutil.copytree(site_docs / version, site_docs / channel, dirs_exist_ok=True)
    manifest = {"versions": versions, "aliases": aliases, "retention": config.get("retention", "")}
    (site_docs / "versions.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--site-dir", type=Path, required=True)
    parser.add_argument("--versions-file", type=Path, required=True)
    parser.add_argument("--assets-dir", type=Path, required=True)
    args = parser.parse_args()
    build_site(args.repo_root.resolve(), args.site_dir.resolve(), args.versions_file.resolve(), args.assets_dir.resolve())


if __name__ == "__main__":
    try:
        main()
    except (subprocess.CalledProcessError, ValueError, KeyError, FileNotFoundError) as exc:
        print(f"versioned docs build failed: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr, file=sys.stderr)
        raise SystemExit(1) from exc
