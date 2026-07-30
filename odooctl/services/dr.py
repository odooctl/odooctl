"""Disaster recovery drill service."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from odooctl.config import IDENTIFIER_MAX_LENGTH, validate_identifier
from odooctl.services.restore import resolve_backup_dir, validate_backup_dir


@dataclass
class DrDrillResult:
    status: str  # "success" | "failed"
    environment: str
    backup_id: str | None
    message: str | None = None
    database: str | None = None
    filestore_path: str | None = None


@dataclass(frozen=True)
class DrDrillTarget:
    """Exact throwaway database/filestore pair presented to an isolated Odoo."""

    environment: str
    backup_id: str
    database: str
    filestore_path: str


PrepareRuntime = Callable[[DrDrillTarget], None]
RestoreArtifact = Callable[[DrDrillTarget, Path], None]
StartRuntime = Callable[[DrDrillTarget], str]
StopRuntime = Callable[[DrDrillTarget], None]


def run_dr_drill(
    *,
    environment: str,
    expected_project: str,
    backups_root: str | Path,
    healthcheck_fn: Callable[[str], bool],
    db_adapter=None,
    fs_adapter=None,
    is_protected_fn: Callable[[str], bool] | None = None,
    throwaway_db_suffix: str = "_dr_drill",
    runtime_filestore_root: str = "/var/lib/odoo/filestore",
    throwaway_filestore_root: str | Path | None = None,
    live_filestore_path: str | Path | None = None,
    prepare_runtime_fn: PrepareRuntime | None = None,
    restore_database_fn: RestoreArtifact | None = None,
    restore_filestore_fn: RestoreArtifact | None = None,
    start_runtime_fn: StartRuntime | None = None,
    stop_runtime_fn: StopRuntime | None = None,
    healthcheck_url: str | None = None,
) -> DrDrillResult:
    """Run a DR drill for *environment* (the SOURCE whose latest backup is drilled).

    The environment may be protected (e.g. production). Steps:
    1. Resolve the latest backup owned by *expected_project* for *environment*.
    2. Validate backup ownership, environment identity, and checksums.
    3. Prepare a disposable network/database/filestore boundary.
    4. Restore the database and filestore inside that boundary.
    5. Start an isolated Odoo bound to the exact pair.
    6. Probe the isolated runtime, then destroy the whole boundary.

    Safety guard: *throwaway_db_suffix* must produce a name that differs from
    the manifest's live DB name, preventing accidental restoration into the
    live database. A drill fails closed unless every isolated-runtime callback is
    supplied. Legacy live database/filestore adapters remain accepted only for
    source compatibility and are deliberately never called.

    ``healthcheck_url``, ``is_protected_fn``, ``throwaway_filestore_root``, and
    ``live_filestore_path`` are also compatibility-only. The health URL must
    come from ``start_runtime_fn`` after the isolated pair is bound.
    """
    del (
        db_adapter,
        fs_adapter,
        healthcheck_url,
        is_protected_fn,
        throwaway_filestore_root,
        live_filestore_path,
    )

    if not expected_project.strip():
        raise ValueError("expected_project must be a non-empty project name")

    backups_root = Path(backups_root)
    backup_dir = resolve_backup_dir(
        environment,
        "latest",
        backups_root,
        expected_project=expected_project,
    )
    manifest = validate_backup_dir(
        backup_dir,
        expected_project=expected_project,
        expected_environment=environment,
    )

    source_db_name = manifest.get("db_name") or f"{environment}_db"
    validate_identifier(source_db_name, "backup db_name")
    if not throwaway_db_suffix:
        raise RuntimeError(
            "Configured throwaway DB suffix must be non-empty so the drill cannot target "
            f"the live database {source_db_name!r}."
        )
    validate_identifier(f"x{throwaway_db_suffix}", "throwaway database suffix")
    available_source_length = IDENTIFIER_MAX_LENGTH - len(throwaway_db_suffix)
    throwaway_db = f"{source_db_name[:available_source_length]}{throwaway_db_suffix}"
    validate_identifier(throwaway_db, "throwaway database")

    if throwaway_db == source_db_name:
        raise RuntimeError(
            f"Throwaway DB name {throwaway_db!r} must differ from the live DB "
            f"{source_db_name!r}. Use a non-empty throwaway_db_suffix to prevent "
            "accidentally targeting the live database."
        )

    filestore_root = PurePosixPath(runtime_filestore_root)
    if not filestore_root.is_absolute() or ".." in filestore_root.parts:
        raise ValueError("runtime_filestore_root must be an absolute container path")
    backup_id = backup_dir.name
    throwaway_filestore = filestore_root / throwaway_db
    target = DrDrillTarget(
        environment=environment,
        backup_id=backup_id,
        database=throwaway_db,
        filestore_path=str(throwaway_filestore),
    )

    callbacks = (
        prepare_runtime_fn,
        restore_database_fn,
        restore_filestore_fn,
        start_runtime_fn,
        stop_runtime_fn,
    )
    if any(callback is None for callback in callbacks):
        return DrDrillResult(
            status="failed",
            environment=environment,
            backup_id=backup_id,
            message=(
                "Complete isolated DR environment callbacks are required; refusing "
                "to restore into live database or filestore services."
            ),
            database=target.database,
            filestore_path=target.filestore_path,
        )

    assert prepare_runtime_fn is not None
    assert restore_database_fn is not None
    assert restore_filestore_fn is not None
    assert start_runtime_fn is not None
    assert stop_runtime_fn is not None

    status = "failed"
    failure_messages: list[str] = []
    runtime_prepare_attempted = False
    try:
        runtime_prepare_attempted = True
        prepare_runtime_fn(target)
        restore_database_fn(target, backup_dir / "db.dump")
        restore_filestore_fn(target, backup_dir / "filestore.tar")
        runtime_url = start_runtime_fn(target)
        if not runtime_url:
            raise RuntimeError("Isolated DR runtime did not return a healthcheck URL")
        ok = healthcheck_fn(runtime_url)
        if ok:
            status = "success"
        else:
            failure_messages.append("Isolated Odoo healthcheck returned False after restore.")
    except Exception as exc:
        failure_messages.append(str(exc))
        status = "failed"
    finally:
        cleanup_errors: list[str] = []
        if runtime_prepare_attempted:
            try:
                stop_runtime_fn(target)
            except Exception as exc:
                cleanup_errors.append(f"isolated environment cleanup failed: {exc}")
        if cleanup_errors:
            failure_messages.extend(cleanup_errors)
            status = "failed"

    return DrDrillResult(
        status=status,
        environment=environment,
        backup_id=backup_id,
        message="; ".join(failure_messages) or None,
        database=target.database,
        filestore_path=target.filestore_path,
    )
