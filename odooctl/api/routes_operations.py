"""Operation queue and event streaming routes.

POST /projects/{project}/operations  — enqueue a mutating operation.
GET  /operations/{id}                — fetch operation record.
GET  /operations/{id}/events         — SSE stream of operation events.
POST /operations/{id}/cancel         — cancel a queued/running operation.

Params are redacted via ``odooctl.security.redaction.redact`` before storing.
A capability token scoped to the exact action/environment/project is minted
and embedded in the queue entry; the runner verifies it before executing.

No privileged imports — satisfies the runner contract.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import PurePosixPath
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from odooctl.api.auth import get_principal, require_action
from odooctl.security.rbac import Action

router = APIRouter()

# Map operation kind strings to the RBAC action that gates them.
_KIND_ACTION: dict[str, Action] = {
    "backup": Action.BACKUP,
    "restore": Action.RESTORE,
    "clone": Action.CLONE,
    "deploy": Action.DEPLOY,
    "promote": Action.PROMOTE,
    "env_create": Action.ENV,
    "env_destroy": Action.ENV,
    "update_modules": Action.DEPLOY,
    "rollback": Action.RESTORE,
    "dr_drill": Action.RESTORE,
    "snapshot_create": Action.BACKUP,
    "snapshot_reconcile": Action.BACKUP,
    "snapshot_restore": Action.RESTORE,
    "pitr_base_create": Action.BACKUP,
    "pitr_restore": Action.RESTORE,
    "pitr_cutover": Action.RESTORE,
    "pitr_reconcile": Action.BACKUP,
    "filestore_migrate": Action.RESTORE,
    "migrate_rehearsal": Action.RESTORE,
}

_SAFE_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _require_safe_operation_id(
    params: dict[str, Any],
    name: str,
) -> str:
    value = params.get(name)
    if (
        not isinstance(value, str)
        or value in {".", ".."}
        or not _SAFE_OPERATION_ID.fullmatch(value)
    ):
        raise HTTPException(
            status_code=400,
            detail=f"{name} must be one safe operation identifier",
        )
    return value


_FILESTORE_ACTIONS = {
    "plan",
    "sync",
    "verify",
    "cutover",
    "download",
    "delete_source",
    "delete_remote_marker",
}


def _validate_filestore_params(
    environment: str,
    params: dict[str, Any],
) -> None:
    action = params.get("action")
    if action not in _FILESTORE_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail="filestore_migrate requires a supported action",
        )
    if action == "plan":
        return
    migration_id = _require_safe_operation_id(params, "migration_id")
    if action == "cutover" and (
        params.get("confirm_environment") != environment
        or params.get("confirm_source_retained") is not True
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "filestore cutover requires the exact environment and "
                "confirm_source_retained=true"
            ),
        )
    if action == "download":
        destination = params.get("destination")
        if not isinstance(destination, str) or not destination.strip():
            raise HTTPException(
                status_code=400,
                detail="filestore download requires a destination",
            )
        destination_path = PurePosixPath(destination)
        if (
            destination_path.is_absolute()
            or ".." in destination_path.parts
            or "\\" in destination
            or any(ord(character) < 32 for character in destination)
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "queued filestore download destination must be a safe "
                    "project-relative path"
                ),
            )
    if action == "delete_source" and (
        params.get("confirm_environment") != environment
        or params.get("confirm_migration_id") != migration_id
        or params.get("delete_source") is not True
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "filestore source deletion requires exact environment/"
                "migration confirmations and delete_source=true"
            ),
        )
    if (
        action == "delete_remote_marker"
        and params.get("confirm_migration_id") != migration_id
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "filestore remote marker deletion requires exact migration "
                "confirmation"
            ),
        )


class OperationRequest(BaseModel):
    kind: str
    environment: str
    params: dict[str, Any] = {}


# Server-side ceiling for the events endpoint's ``max_polls`` query parameter
# (600 × 0.5 s = 5 minutes). Prevents a client from pinning a worker on an
# effectively unbounded poll loop.
MAX_POLLS_CEILING = 600


def _clamp_max_polls(value: int) -> int:
    """Clamp a client-supplied poll count into [1, MAX_POLLS_CEILING]."""
    return max(1, min(int(value), MAX_POLLS_CEILING))


def _require_op_in_token_scope(request: Request, op) -> None:
    """Enforce the token's project claim on non-project-scoped op routes.

    ``/operations/{op_id}`` routes carry no ``{project}`` path segment, so the
    op is located by searching all projects. A token minted with a concrete
    ``proj`` claim (not ``"*"``) must not read or cancel operations belonging
    to another project. Responds 404 (not 403) so op IDs in other projects are
    not disclosed as existing.
    """
    claim = str(getattr(request.state, "token_project", "*") or "*")
    if claim != "*" and op.project != claim:
        raise HTTPException(status_code=404, detail=f"Operation {op.id!r} not found")


def _load_ctx(request: Request, project: str):
    from odooctl.registry import context_from_registered

    reg = request.app.state.registry_loader()
    proj = reg.projects.get(project)
    if proj is None:
        raise HTTPException(status_code=404, detail=f"Project {project!r} not found")
    try:
        return context_from_registered(proj)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _find_op_ctx(request: Request, op_id: str):
    """Search all registered projects for an operation by ID."""
    from odooctl.operations.store import OperationStore
    from odooctl.registry import context_from_registered

    reg = request.app.state.registry_loader()
    for proj in reg.projects.values():
        try:
            ctx = context_from_registered(proj)
        except Exception:
            continue
        store = OperationStore(ctx.state_dir)
        try:
            op = store.load(op_id)
            return op, store
        except KeyError:
            continue
    raise HTTPException(status_code=404, detail=f"Operation {op_id!r} not found")


@router.post("/projects/{project}/operations", status_code=202)
def enqueue_operation(
    project: str,
    body: OperationRequest,
    request: Request,
    principal=Depends(get_principal),
):
    from odooctl.api.auth import enforce_project_scope
    from odooctl.api.queue import OperationQueue, QueueEntry
    from odooctl.operations.models import Operation, OperationKind
    from odooctl.operations.store import OperationStore
    from odooctl.security import rbac, tokens
    from odooctl.security.redaction import redact

    enforce_project_scope(request, project)
    ctx = _load_ctx(request, project)

    if (
        body.kind
        in {"snapshot_create", "snapshot_reconcile", "snapshot_restore"}
        and ctx.config.snapshots.provider != "none"
        and body.environment != ctx.config.snapshots.environment
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Snapshot provider is bound to environment "
                f"{ctx.config.snapshots.environment!r}, not "
                f"{body.environment!r}"
            ),
        )

    if body.kind.startswith("pitr_"):
        pitr = ctx.config.pitr
        if not pitr.enabled:
            raise HTTPException(
                status_code=400,
                detail="PITR is disabled for this project",
            )
        if body.environment != pitr.environment:
            raise HTTPException(
                status_code=400,
                detail=(
                    "PITR is bound to environment "
                    f"{pitr.environment!r}, not {body.environment!r}"
                ),
            )
        if body.kind == "pitr_restore":
            _require_safe_operation_id(body.params, "plan_id")
        elif body.kind == "pitr_cutover":
            _require_safe_operation_id(body.params, "restore_id")
            expected_database = ctx.config.env(
                body.environment
            ).db_name
            if (
                body.params.get("confirm_environment")
                != body.environment
                or body.params.get("confirm_database")
                != expected_database
                or body.params.get("accept_database_only") is not True
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "PITR cutover requires exact environment/database "
                        "confirmations and accept_database_only=true"
                    ),
                )

    # Resolve the target environment before authorization so protected-env
    # policy is applied to the actual enqueue target.
    try:
        environment_config = ctx.config.env(body.environment)
        protected = ctx.config.is_protected(body.environment)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if body.kind == "filestore_migrate":
        _validate_filestore_params(body.environment, body.params)
        backend = environment_config.filestore_backend
        if backend is None or backend.type not in {
            "object_mirror",
            "posix_object_mount",
            "odoo_module",
        }:
            raise HTTPException(
                status_code=400,
                detail=(
                    "filestore migration requires an explicit object_mirror, "
                    "posix_object_mount, or odoo_module backend"
                ),
            )
        if (
            body.params.get("action") == "delete_source"
            and backend.type == "object_mirror"
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "object_mirror is not an Odoo serving backend and "
                    "cannot authorize source deletion"
                ),
            )

    # RBAC check for the specific operation kind
    action = _KIND_ACTION.get(body.kind)
    if action is None:
        raise HTTPException(status_code=400, detail=f"Unknown operation kind: {body.kind!r}")
    try:
        rbac.require(principal, action, protected=protected)
    except rbac.AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    api_key: str = request.app.state.api_key

    # Redact user-supplied params before recording
    params_clean = redact(body.params)

    # Create durable operation record (status=QUEUED)
    try:
        kind_enum = OperationKind(body.kind)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid operation kind: {body.kind!r}")

    op = Operation.create(
        kind=kind_enum,
        project=project,
        environment=body.environment,
        actor=principal.id,
        params_redacted=params_clean if isinstance(params_clean, dict) else {},
    )
    store = OperationStore(ctx.state_dir)
    store.save(op)

    # Mint a short-lived capability token scoped to this exact operation.
    # The default TTL (300 s) bounds the replay window; see F12.
    cap_token = tokens.mint(
        api_key,
        action=body.kind,
        environment=body.environment,
        project=project,
        ttl_seconds=tokens.DEFAULT_TTL_SECONDS,
        subject=principal.id,
        roles=[role.value for role in principal.roles],
    )

    # Write queue entry
    entry = QueueEntry.create(
        op_id=op.id,
        kind=body.kind,
        project=project,
        environment=body.environment,
        actor=principal.id,
        params_redacted=op.params_redacted,
        token=cap_token,
    )
    OperationQueue(ctx.state_dir).enqueue(entry)

    return {
        "op_id": op.id,
        "kind": op.kind.value,
        "project": project,
        "environment": body.environment,
        "status": op.status.value,
        "created_at": op.created_at,
    }


@router.get("/operations/{op_id}")
def get_operation(
    op_id: str,
    request: Request,
    principal=Depends(require_action(Action.OPERATIONS)),
):
    op, _ = _find_op_ctx(request, op_id)
    _require_op_in_token_scope(request, op)
    return {
        "op_id": op.id,
        "kind": op.kind.value,
        "project": op.project,
        "environment": op.environment,
        "status": op.status.value,
        "actor": op.actor,
        "params_redacted": op.params_redacted,
        "created_at": op.created_at,
        "updated_at": op.updated_at,
        "error": op.error,
        "result_ref": op.result_ref,
    }


@router.get("/operations/{op_id}/events")
def stream_events(
    op_id: str,
    request: Request,
    principal=Depends(require_action(Action.OPERATIONS)),
    max_polls: int = 120,
):
    """Stream operation events as Server-Sent Events.

    Polls until the operation reaches a terminal state or *max_polls* is
    exhausted (default 120 × 0.5 s = 60 s). Pass ``?max_polls=1`` in tests
    to avoid blocking indefinitely on a queued operation. ``max_polls`` is
    clamped server-side to :data:`MAX_POLLS_CEILING`.
    """
    from odooctl.operations.models import OperationStatus

    op, store = _find_op_ctx(request, op_id)
    _require_op_in_token_scope(request, op)
    max_polls = _clamp_max_polls(max_polls)

    async def _generate():
        seen = 0
        polls = 0
        while True:
            events = store.load_events(op_id)
            for event in events[seen:]:
                yield f"data: {event.to_json()}\n\n"
                seen += 1
            current_op = store.load(op_id)
            if current_op.status in (
                OperationStatus.SUCCEEDED,
                OperationStatus.FAILED,
                OperationStatus.CANCELLED,
            ):
                break
            polls += 1
            if polls >= max_polls:
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(_generate(), media_type="text/event-stream")


@router.post("/operations/{op_id}/cancel", status_code=200)
def cancel_operation(
    op_id: str,
    request: Request,
    principal=Depends(require_action(Action.CANCEL)),
):
    from odooctl.api.queue import OperationQueue
    from odooctl.context import ProjectContext
    from odooctl.operations.models import OperationStatus

    op, store = _find_op_ctx(request, op_id)
    _require_op_in_token_scope(request, op)
    if op.status not in (OperationStatus.QUEUED,):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel operation in status {op.status.value!r}",
        )

    # Remove the pending queue file so the runner cannot claim and execute it.
    # Best-effort: if the queue file is already claimed (.running), the runner
    # will re-check the operation status and skip execution.
    reg = request.app.state.registry_loader()
    proj = reg.projects.get(op.project)
    if proj is not None:
        try:
            ctx = ProjectContext.from_config_path(proj.config, root=proj.path)
            OperationQueue(ctx.state_dir).cancel(op_id)
        except Exception:
            pass

    updated = store.update_status(op_id, OperationStatus.CANCELLED)
    return {"op_id": updated.id, "status": updated.status.value}
