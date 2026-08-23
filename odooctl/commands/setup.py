"""odooctl setup — scaffold a greenfield Odoo project.

Generates a starter odooctl.yml for a brand-new project that does not yet
have a running deployment. For importing an existing deployment, use
'odooctl import' instead.

Usage:
    odooctl setup                            # interactive wizard
    odooctl setup --yes --stack odoo-19-community --name myproject
    odooctl setup --catalog my-catalog.yaml --stack my-custom-stack --yes
"""
from __future__ import annotations

from pathlib import Path

import yaml
import typer

from odooctl.catalog.registry import get_entry, get_stack_templates, load_manifest
from odooctl.catalog.render import render_stack_template
from odooctl.catalog.schema import CatalogEntry, StackTemplate
from odooctl.commands.init import ensure_overlay_gitignored
from odooctl.utils.logging import success, warn


# Legacy stacks not in catalog — kept for backward compatibility with existing
# configs that reference odoo-17-community or odoo-16-community.
_LEGACY_STACKS: dict[str, dict[str, str]] = {
    "odoo-17-community": {"odoo_version": "17.0", "image": "odoo:17.0"},
    "odoo-16-community": {"odoo_version": "16.0", "image": "odoo:16.0"},
}

_DEFAULT_STACK = "odoo-19-community"


def _build_known_stacks(extra: list[CatalogEntry] | None = None) -> dict[str, dict[str, str]]:
    """Merge legacy hardcoded stacks with catalog stack templates (plus any extras)."""
    result: dict[str, dict[str, str]] = dict(_LEGACY_STACKS)
    for st in get_stack_templates(extra=extra).values():
        result[st.id] = {"odoo_version": st.odoo_version, "image": st.odoo_image}
    return result


KNOWN_STACKS: dict[str, dict[str, str]] = _build_known_stacks()


def scaffold_project(
    project_name: str = "my-odoo-project",
    stack: str = _DEFAULT_STACK,
    output_path: Path = Path("odooctl.yml"),
    force: bool = False,
    extra_catalog: list[CatalogEntry] | None = None,
) -> None:
    """Write a starter odooctl.yml for a greenfield project.

    Raises FileExistsError if output_path exists and force is False.
    Secrets are referenced by env-var name only, never inlined.
    Uses catalog StackTemplate when available; falls back to legacy stack table.

    extra_catalog extends the bundled catalog for this call only (from --catalog PATH).
    """
    if output_path.exists() and not force:
        raise FileExistsError(
            f"{output_path} already exists. "
            "Pass force=True (or --force on the CLI) to overwrite."
        )

    # Prefer catalog StackTemplate for pinned images and richer metadata.
    entry = get_entry(stack, extra=extra_catalog)
    if isinstance(entry, StackTemplate):
        cfg = render_stack_template(entry, project_name)
    else:
        # Legacy fallback for stacks not in catalog (and not in extra_catalog).
        all_stacks = _build_known_stacks(extra=extra_catalog)
        stack_info = all_stacks.get(stack, KNOWN_STACKS[_DEFAULT_STACK])
        db_name = f"{project_name}_prod"
        cfg = {
            "project": {
                "name": project_name,
                "odoo_version": stack_info["odoo_version"],
            },
            "runtime": {
                "type": "docker_compose",
                "compose_file": "docker-compose.yml",
                "reverse_proxy": "traefik",
            },
            "postgres": {
                "host": "localhost",
                "port": 5432,
                "user": "odoo",
                "password_env": "ODOO_DB_PASSWORD",
                "service": "db",
            },
            "odoo": {
                "image": stack_info["image"],
                "service": "odoo",
                "addons_paths": ["/mnt/extra-addons"],
            },
            "backups": {
                "local_path": "./backups",
            },
            "environments": {
                "production": {
                    "branch": "main",
                    "domain": "odoo.example.com",
                    "db_name": db_name,
                    "filestore_path": f"/var/lib/odoo/filestore/{db_name}",
                }
            },
        }

    output_path.write_text(yaml.dump(cfg, default_flow_style=False, sort_keys=False))


def run(
    yes: bool = False,
    stack: str | None = None,
    name: str | None = None,
    output: Path = Path("odooctl.yml"),
    force: bool = False,
    catalog: Path | None = None,
) -> None:
    """Interactive wizard to scaffold a greenfield Odoo project."""
    extra_catalog: list[CatalogEntry] | None = None
    if catalog is not None:
        try:
            extra_catalog = load_manifest(catalog)
        except Exception as exc:
            raise typer.BadParameter(f"Failed to load --catalog {catalog}: {exc}") from exc

    all_stacks = _build_known_stacks(extra=extra_catalog)

    if yes:
        project_name = name or "my-odoo-project"
        chosen_stack = stack or _DEFAULT_STACK
    else:
        project_name = name or typer.prompt("Project name", default="my-odoo-project")
        stack_options = ", ".join(all_stacks.keys())
        chosen_stack = stack or typer.prompt(
            f"Stack [{stack_options}]",
            default=_DEFAULT_STACK,
        )

    try:
        scaffold_project(
            project_name=project_name,
            stack=chosen_stack,
            output_path=output,
            force=force,
            extra_catalog=extra_catalog,
        )
    except FileExistsError as exc:
        raise typer.BadParameter(str(exc)) from exc

    success(f"Scaffolded {output} for project '{project_name}' (stack: {chosen_stack})")
    ensure_overlay_gitignored(output)
    warn(
        "Update domains, db names, filestore paths, and environment variable "
        "names in the generated odooctl.yml before running 'odooctl deploy'."
    )
