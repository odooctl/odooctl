from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import tomllib

import click

from odooctl.context import ProjectContext


@dataclass(frozen=True)
class RegisteredProject:
    name: str
    path: Path
    config: str = "odooctl.yml"
    # Owning user/team (an email or team label) for "who owns what" queries.
    # Informational attribution; RBAC roles still decide who may act.
    owner: str = ""


@dataclass(frozen=True)
class Registry:
    path: Path
    active: str | None
    projects: dict[str, RegisteredProject]


def default_registry_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home).expanduser() / "odooctl" / "config.toml"
    return Path.home() / ".config" / "odooctl" / "config.toml"


def load_registry(path: str | Path | None = None) -> Registry:
    registry_path = Path(path).expanduser() if path is not None else default_registry_path()
    if not registry_path.exists():
        return Registry(path=registry_path, active=None, projects={})

    data = tomllib.loads(registry_path.read_text())
    raw_projects = data.get("projects", {}) or {}
    projects: dict[str, RegisteredProject] = {}
    for name, raw in raw_projects.items():
        if not isinstance(raw, dict) or "path" not in raw:
            continue
        projects[name] = RegisteredProject(
            name=name,
            path=Path(str(raw["path"])).expanduser(),
            config=str(raw.get("config", "odooctl.yml")),
            owner=str(raw.get("owner", "")),
        )
    active = data.get("active")
    if active is not None:
        active = str(active)
    return Registry(path=registry_path, active=active, projects=projects)


def save_registry(registry: Registry) -> None:
    registry.path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if registry.active:
        lines.append(f'active = "{_toml_escape(registry.active)}"')
        lines.append("")
    if registry.projects:
        lines.append("[projects]")
        for name in sorted(registry.projects):
            project = registry.projects[name]
            entry = (
                f'"{_toml_escape(name)}" = {{ path = "{_toml_escape(str(project.path))}", '
                f'config = "{_toml_escape(project.config)}"'
            )
            if project.owner:
                entry += f', owner = "{_toml_escape(project.owner)}"'
            lines.append(entry + " }")
    registry.path.write_text("\n".join(lines).rstrip() + "\n")


def _validate_project_name(name: str) -> str:
    """Reject project names that could inject path components (audit finding F10).

    Project names flow into registry keys and state paths, so they follow the
    same identifier rule as config environment names.
    """
    from odooctl.config import validate_identifier

    try:
        return validate_identifier(name, "project name")
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


def _contained_config_path(name: str, root: Path, config: str | Path) -> Path:
    """Resolve a project's config path and require it to stay inside *root*.

    Path containment (audit finding F10): a registry entry's ``config`` value
    is attacker-influenceable (hand-edited config.toml), so a value like
    ``../../etc/passwd`` must not read files outside the registered project
    root. The project root itself may be any absolute path; absolute config
    paths keep working as long as they resolve inside the root.
    """
    root_resolved = Path(root).expanduser().resolve()
    config_path = Path(config).expanduser()
    resolved = (config_path if config_path.is_absolute() else root_resolved / config_path).resolve()
    if not resolved.is_relative_to(root_resolved):
        raise click.ClickException(
            f"Config path for project {name!r} escapes the project root: "
            f"{resolved} is not inside {root_resolved}"
        )
    return resolved


def add_project(
    name: str,
    path: str | Path,
    config: str = "odooctl.yml",
    *,
    make_active: bool = True,
    owner: str = "",
) -> RegisteredProject:
    _validate_project_name(name)
    registry = load_registry()
    root = Path(path).expanduser().resolve()
    resolved_config = _contained_config_path(name, root, config)
    if not resolved_config.exists():
        raise click.ClickException(f"Config file not found for project {name!r}: {resolved_config}")
    project = RegisteredProject(name=name, path=root, config=str(config), owner=owner)
    projects = dict(registry.projects)
    projects[name] = project
    active = name if make_active or registry.active is None else registry.active
    save_registry(Registry(path=registry.path, active=active, projects=projects))
    return project


def remove_project(name: str) -> None:
    registry = load_registry()
    if name not in registry.projects:
        raise click.ClickException(f"Unknown project: {name}")
    projects = dict(registry.projects)
    projects.pop(name)
    active = registry.active
    if active == name:
        active = next(iter(sorted(projects)), None)
    save_registry(Registry(path=registry.path, active=active, projects=projects))


def set_project_owner(
    name: str, owner: str, *, registry_path: str | Path | None = None
) -> RegisteredProject:
    """Record the owning user/team for a registered project."""
    import dataclasses

    registry = load_registry(registry_path)
    project = registry.projects.get(name)
    if project is None:
        raise click.ClickException(f"Unknown project: {name}")
    updated = dataclasses.replace(project, owner=owner)
    projects = dict(registry.projects)
    projects[name] = updated
    save_registry(Registry(path=registry.path, active=registry.active, projects=projects))
    return updated


def use_project(name: str) -> RegisteredProject:
    registry = load_registry()
    project = registry.projects.get(name)
    if project is None:
        raise click.ClickException(f"Unknown project: {name}")
    save_registry(Registry(path=registry.path, active=name, projects=registry.projects))
    return project


def context_from_registered(registered: "RegisteredProject") -> ProjectContext:
    """Build a ProjectContext from a registry entry with path containment.

    Codex re-scan finding #6: the CLI resolver enforced ``_contained_config_path``
    but the API loaders and the privileged runner called
    ``ProjectContext.from_config_path`` directly, so a hand-edited registry entry
    with ``config="../../attacker/odooctl.yml"`` could load a config outside the
    registered root. All registry-to-context resolution must go through here.
    """
    resolved_config = _contained_config_path(registered.name, registered.path, registered.config)
    return ProjectContext.from_config_path(resolved_config, root=registered.path)


def resolve_project_context(
    *,
    project: str | None = None,
    project_dir: str | Path | None = None,
    config: str | Path = "odooctl.yml",
) -> ProjectContext:
    """Resolve project context with precedence: -p > -C > cwd/config."""

    if project:
        registry = load_registry()
        registered = registry.projects.get(project)
        if registered is None:
            raise click.ClickException(f"Unknown project: {project}")
        # Containment check (audit F10): reject registry entries whose config
        # resolves outside the registered project root.
        return context_from_registered(registered)
    if project_dir is not None:
        return ProjectContext.from_config_path(config, root=project_dir)
    return ProjectContext.from_config_path(config)


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
