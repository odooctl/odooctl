"""Read-only project and environment routes.

All reads come from the registry, project config, metadata store, and
operation store — no Docker/Postgres/git calls. Satisfies the runner contract.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from odooctl.api.auth import enforce_project_scope, require_action
from odooctl.security.rbac import Action

router = APIRouter()


def _registry(request: Request):
    return request.app.state.registry_loader()


def _load_ctx(request: Request, project: str):
    from odooctl.registry import context_from_registered

    # A per-project token must not reach another project's config/state.
    enforce_project_scope(request, project)
    reg = _registry(request)
    proj = reg.projects.get(project)
    if proj is None:
        raise HTTPException(status_code=404, detail=f"Project {project!r} not found")
    try:
        # Path containment: reject a registry config that escapes its root.
        return context_from_registered(proj)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/projects")
def list_projects(
    request: Request,
    principal=Depends(require_action(Action.READ)),
):
    reg = _registry(request)
    names = sorted(reg.projects.keys())
    # A token scoped to one project only learns of that project.
    claim = str(getattr(request.state, "token_project", None) or "")
    if claim != "*":
        names = [n for n in names if n == claim]
    return {"projects": names}


@router.get("/projects/{project}")
def get_project(
    project: str,
    request: Request,
    principal=Depends(require_action(Action.READ)),
):
    enforce_project_scope(request, project)
    reg = _registry(request)
    proj = reg.projects.get(project)
    if proj is None:
        raise HTTPException(status_code=404, detail=f"Project {project!r} not found")
    return {"name": proj.name, "path": str(proj.path)}


@router.get("/projects/{project}/environments")
def list_environments(
    project: str,
    request: Request,
    principal=Depends(require_action(Action.READ)),
):
    ctx = _load_ctx(request, project)
    envs = [
        {
            "name": name,
            "branch": env.branch,
            "domain": env.domain,
            "tier": env.tier,
            "protected": env.protected,
        }
        for name, env in ctx.config.environments.items()
    ]
    return {"environments": envs}


@router.get("/projects/{project}/status")
def get_project_status(
    project: str,
    request: Request,
    principal=Depends(require_action(Action.STATUS)),
):
    from odooctl.metadata.store import MetadataStore
    from odooctl.operations.store import OperationStore

    ctx = _load_ctx(request, project)
    meta = MetadataStore(ctx.state_dir)
    op_store = OperationStore(ctx.state_dir)

    envs = []
    for name in ctx.config.environments:
        dep = meta.latest_deployment(name) or {}
        bak = meta.latest_backup(name) or {}
        envs.append(
            {
                "name": name,
                "last_deployment_status": dep.get("status", "unknown"),
                "last_deployment_commit": dep.get("commit", "unknown"),
                "latest_backup": bak.get("timestamp", "unknown"),
            }
        )

    recent_ops = [
        {
            "op_id": op.id,
            "kind": op.kind.value,
            "environment": op.environment,
            "status": op.status.value,
            "created_at": op.created_at,
        }
        for op in op_store.list_all(limit=10)
    ]

    return {
        "project": ctx.config.project.name,
        "environments": envs,
        "recent_operations": recent_ops,
    }


@router.get("/projects/{project}/backups")
def list_backups(
    project: str,
    request: Request,
    principal=Depends(require_action(Action.BACKUPS)),
):
    from odooctl.metadata.store import MetadataStore

    ctx = _load_ctx(request, project)
    meta = MetadataStore(ctx.state_dir)

    backups = []
    for name in ctx.config.environments:
        bak = meta.latest_backup(name)
        if bak:
            backups.append(bak)

    # Also scan backup manifests directory
    backup_manifests_dir = ctx.state_dir / "backups"
    if backup_manifests_dir.exists():
        for manifest_file in sorted(backup_manifests_dir.glob("*.json")):
            if manifest_file.stem.endswith("-latest"):
                continue
            try:
                import json

                data = json.loads(manifest_file.read_text())
                if data not in backups:
                    backups.append(data)
            except Exception:
                continue

    return {"backups": backups}


@router.get("/projects/{project}/snapshots")
def list_snapshots(
    project: str,
    request: Request,
    environment: str | None = None,
    principal=Depends(require_action(Action.BACKUPS)),
):
    from odooctl.metadata.store import MetadataStore

    ctx = _load_ctx(request, project)
    if environment is not None and environment not in ctx.config.environments:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown environment: {environment!r}",
        )
    manifests = MetadataStore(ctx.state_dir).list_snapshots(environment)
    return {
        "snapshots": [
            manifest.model_dump(mode="json") for manifest in manifests
        ]
    }


@router.get("/projects/{project}/snapshots/{snapshot_id}")
def get_snapshot(
    project: str,
    snapshot_id: str,
    request: Request,
    principal=Depends(require_action(Action.BACKUPS)),
):
    from odooctl.metadata.store import MetadataStore

    ctx = _load_ctx(request, project)
    try:
        manifest = MetadataStore(ctx.state_dir).get_snapshot(snapshot_id)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if manifest.project != ctx.config.project.name:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return manifest.model_dump(mode="json")


@router.get("/projects/{project}/restore-points")
def list_restore_points(
    project: str,
    request: Request,
    environment: str | None = None,
    principal=Depends(require_action(Action.BACKUPS)),
):
    from odooctl.services.restore_points import list_restore_points as _list_rp

    ctx = _load_ctx(request, project)
    points = _list_rp(ctx.backups_dir, environment=environment)
    return {
        "restore_points": [
            {
                "backup_id": p.backup_id,
                "environment": p.environment,
                "timestamp": p.timestamp,
                "integrity": p.integrity,
            }
            for p in points
        ]
    }


@router.get("/projects/{project}/audit")
def get_audit(
    project: str,
    request: Request,
    principal=Depends(require_action(Action.AUDIT)),
):
    from odooctl.operations.audit import AuditStore

    ctx = _load_ctx(request, project)
    audit = AuditStore(ctx.state_dir)
    entries = audit.load_chain()
    return {
        "entries": [
            {
                "actor": e.actor,
                "action": e.action,
                "target": e.target,
                "outcome": e.outcome,
                "op_id": e.op_id,
                "timestamp": e.timestamp,
            }
            for e in entries
        ]
    }
