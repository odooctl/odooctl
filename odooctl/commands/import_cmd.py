"""odooctl import — take over an existing Docker Compose Odoo deployment.

Safety contract (enforced end-to-end):
  - Detection is strictly read-only: no subprocess calls, no Docker daemon
    access, no container mutations, no DB writes, no volume writes.
  - Secret values are never printed, logged, or written to config.
  - Generated config is written only after explicit --yes confirmation.
  - Existing odooctl.yml is never overwritten without --force.

After adoption (--yes), the command automatically:
  1. Registers the project in the local registry (add_project).
  2. Validates the generated config (validate).
  3. Runs preflight doctor checks unless --skip-doctor is passed.
  4. Runs a safety backup unless --skip-backup is passed.

Usage:
    odooctl import                             # preview current directory
    odooctl import PATH                        # preview a specific compose file/dir
    odooctl import --preview                   # explicit preview flag (same as default)
    odooctl import --name myproject --yes      # adopt with project name
    odooctl import --name myproject --yes --skip-backup   # adopt, skip backup
    odooctl import --force                     # overwrite existing odooctl.yml
"""
from __future__ import annotations

from pathlib import Path

import typer

from odooctl.commands import backup as backup_cmd
from odooctl.commands import doctor as doctor_cmd
from odooctl.commands import validate as validate_cmd
from odooctl.importer.adopt import adopt
from odooctl.importer.detect import detect_from_compose
from odooctl.importer.report import build_preview_report, render_preview_text
from odooctl.registry import add_project
from odooctl.utils.logging import info, success, warn


def _find_compose(path: Path | None) -> Path:
    if path is not None:
        p = Path(path)
        if p.is_file():
            return p
        if p.is_dir():
            for name in ("docker-compose.yml", "docker-compose.yaml"):
                candidate = p / name
                if candidate.exists():
                    return candidate
        raise typer.BadParameter(
            f"No docker-compose.yml found at {path}. "
            "Pass the path to a compose file or directory containing one."
        )
    for name in ("docker-compose.yml", "docker-compose.yaml"):
        p = Path.cwd() / name
        if p.exists():
            return p
    raise typer.BadParameter(
        "No docker-compose.yml found in the current directory. "
        "Pass a path as the first argument."
    )


def _infer_project_name(compose_path: Path) -> str:
    return compose_path.parent.name or "imported-odoo"


def _check_output_containment(output: Path, compose_path: Path, *, allow_outside: bool) -> None:
    """Refuse to write the generated config outside the working/project dir.

    Path containment (audit finding F20): ``--output`` combined with
    ``--force`` could otherwise overwrite arbitrary files. The resolved output
    must live under the current working directory or the imported project's
    directory (the compose file's parent) unless *allow_outside* is explicitly
    set (CLI flag ``--allow-outside``, default off).
    """
    if allow_outside:
        return
    resolved = output.expanduser().resolve()
    allowed_roots = (Path.cwd().resolve(), compose_path.parent.resolve())
    if not any(resolved.is_relative_to(root) for root in allowed_roots):
        raise typer.BadParameter(
            f"Refusing to write {resolved}: it is outside the current working "
            f"directory and the imported project directory. "
            "Pass --allow-outside to permit writing elsewhere."
        )


def run(
    path: Path | None = None,
    *,
    preview: bool = False,
    name: str | None = None,
    yes: bool = False,
    force: bool = False,
    output: Path = Path("odooctl.yml"),
    skip_doctor: bool = False,
    skip_backup: bool = False,
    allow_outside: bool = False,
) -> None:
    """Import an existing Odoo Docker Compose deployment.

    By default this shows a preview only. Pass --yes to write odooctl.yml.
    Secret values are never printed or written to config files.

    After writing the config, the project is registered in the registry,
    the config is validated, doctor preflight checks run (unless
    --skip-doctor), and a safety backup is created (unless --skip-backup).

    Path containment (audit finding F20): --output must resolve inside the
    current working directory or the imported project directory; pass
    --allow-outside (allow_outside=True) to write elsewhere. --force is still
    required to overwrite an existing file, regardless of --allow-outside.
    """
    compose_path = _find_compose(path)
    info(f"Detecting deployment from {compose_path} …")

    detected = detect_from_compose(compose_path)
    project_name = name or _infer_project_name(compose_path)
    report = build_preview_report(detected, project_name=project_name)

    typer.echo(render_preview_text(report))

    if not yes or preview:
        typer.echo(
            "\nThis is a preview. Run with --yes to adopt this config, "
            "or --name to change the project name.\n"
            "SAFETY: no files have been written and no containers were touched."
        )
        return

    _check_output_containment(output, compose_path, allow_outside=allow_outside)

    try:
        adopt(report, output_path=output, force=force)
    except FileExistsError as exc:
        raise typer.BadParameter(str(exc)) from exc

    config_path = output.resolve()
    success(f"Adopted config written to {config_path}")

    # Register the project so it can be referenced by --project/-p globally.
    try:
        add_project(project_name, config_path.parent, config=config_path.name)
        success(f"Registered project '{project_name}' in registry.")
    except Exception as exc:
        warn(f"Could not register project in registry: {exc}")

    # Validate the generated config (schema + env-var audit).
    try:
        validate_cmd.execute(str(config_path))
    except Exception as exc:
        warn(f"Config validation warning: {exc}")

    # Run preflight doctor checks (side-effect-free).
    if not skip_doctor:
        try:
            report_doc = doctor_cmd._run_doctor(str(config_path))
            if report_doc.ok:
                success("Doctor: all preflight checks passed.")
            else:
                for check in report_doc.checks:
                    if not check.ok:
                        warn(f"Doctor [{check.name}]: {check.message}")
                warn("Doctor: some checks failed — run 'odooctl doctor' to review and fix.")
        except Exception as exc:
            warn(f"Doctor: check failed with error: {exc}")

    # Run the first portable database + filestore backup after adoption.
    if not skip_backup:
        try:
            backup_id = backup_cmd.execute("production", str(config_path))
            success(f"Safety backup created: {backup_id}")
        except Exception as exc:
            warn(f"Backup after adoption failed: {exc}")
            warn("Run 'odooctl backup production' manually to create a safety backup.")
