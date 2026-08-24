from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import click

from odooctl.config import OdooCtlConfig, load_config, local_overlay_path


@dataclass(frozen=True)
class ProjectContext:
    """Resolved filesystem context for an odooctl project.

    The project root is the directory that owns the selected config file unless
    an explicit root is provided. Relative runtime/state paths should be
    resolved through this object instead of the process current working
    directory.

    ``overlay_path`` is the machine-local overlay (``odooctl.local.yml``) that
    was merged into ``config``, or None when no overlay file exists.
    """

    root: Path
    config_path: Path
    config: OdooCtlConfig
    overlay_path: Path | None = None

    @classmethod
    def from_config_path(cls, config_path: str | Path = "odooctl.yml", *, root: str | Path | None = None) -> "ProjectContext":
        raw_config = Path(config_path).expanduser()
        if raw_config.is_absolute():
            resolved_config = raw_config.resolve()
        else:
            base = Path(root).expanduser() if root is not None else Path.cwd()
            resolved_config = (base / raw_config).resolve()

        if not resolved_config.exists():
            raise click.ClickException(f"Config file not found: {resolved_config}")

        project_root = Path(root).expanduser().resolve() if root is not None else resolved_config.parent
        cfg = load_config(resolved_config)
        overlay = local_overlay_path(resolved_config)
        if overlay is not None and not overlay.exists():
            overlay = None
        return cls(root=project_root, config_path=resolved_config, config=cfg, overlay_path=overlay)

    def resolve_path(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        if path.is_absolute():
            return path
        return (self.root / path).resolve()

    @property
    def state_dir(self) -> Path:
        return self.root / ".odooctl"

    @property
    def compose_file(self) -> Path:
        return self.resolve_path(self.config.runtime.compose_file)

    @property
    def backups_dir(self) -> Path:
        return self.resolve_path(self.config.backups.local_path)

    @property
    def odoo_config_path(self) -> Path:
        return self.resolve_path(self.config.odoo.config_path)

    def sanitization_sql_files(self) -> list[Path]:
        return [self.resolve_path(path) for path in self.config.sanitization.sql_files]
