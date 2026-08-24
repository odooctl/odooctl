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


@router.get("/runner/status")
def runner_status(
    request: Request,
    principal=Depends(require_action(Action.READ)),
):
    """Report whether a privileged runner is live and processing operations.

    Read from the runner's heartbeat file (written next to the registry). When
    offline, enqueued operations stay ``queued`` until a runner is started with
    ``odooctl runner``; the UI surfaces this so the queue does not look broken.
    """
    from odooctl.operations.runner_heartbeat import read_status

    reg = _registry(request)
    status = read_status(reg.path)
    status["hint"] = None if status["online"] else "odooctl runner"
    return status


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
    return {"name": proj.name, "path": str(proj.path), "owner": proj.owner}


@router.patch("/projects/{project}/owner")
def set_project_owner(
    project: str,
    body: dict,
    request: Request,
    principal=Depends(require_action(Action.USERS)),
):
    """Record the owning user/team for a project (admin+). Empty string clears."""
    import dataclasses

    from odooctl.registry import Registry, save_registry

    enforce_project_scope(request, project)
    reg = _registry(request)
    proj = reg.projects.get(project)
    if proj is None:
        raise HTTPException(status_code=404, detail=f"Project {project!r} not found")
    updated = dataclasses.replace(proj, owner=str(body.get("owner", "")))
    projects = dict(reg.projects)
    projects[project] = updated
    save_registry(Registry(path=reg.path, active=reg.active, projects=projects))
    return {"name": updated.name, "owner": updated.owner}


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
            "owner": env.owner,
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


@router.get("/projects/{project}/containers")
def get_containers(
    project: str,
    request: Request,
    principal=Depends(require_action(Action.STATUS)),
):
    """Live container status for the project's compose stack.

    Served from the snapshot the privileged runner refreshes every
    ``PROBE_INTERVAL_SECONDS`` — the API itself never touches Docker. When no
    runner has probed yet, ``available`` is false; a snapshot older than the
    staleness window is flagged ``stale`` (runner stopped or stack unreachable).
    """
    from odooctl.operations.container_status import read_snapshot

    ctx = _load_ctx(request, project)
    snapshot = read_snapshot(ctx.state_dir)
    snapshot["services"] = {
        "odoo": ctx.config.odoo.service,
        "postgres": ctx.config.postgres.service,
    }
    return snapshot


@router.get("/rbac/matrix")
def get_rbac_matrix(
    request: Request,
    principal=Depends(require_action(Action.READ)),
):
    """The role → action matrix plus protected-environment policy, for the UI."""
    from odooctl.security import rbac

    return {
        "matrix": rbac.role_matrix(),
        "read_actions": sorted(a.value for a in rbac.READ_ACTIONS),
        "write_actions": sorted(a.value for a in rbac.WRITE_ACTIONS),
        "destructive_on_protected": sorted(a.value for a in rbac.DESTRUCTIVE_ON_PROTECTED),
        "protected_floor": "admin",
        "roles": [role.value for role in principal.roles],
    }


@router.post("/tokens", status_code=201)
def mint_token(
    body: dict,
    request: Request,
    principal=Depends(require_action(Action.SECRETS)),
):
    """Mint a scoped API bearer token (admin/owner only).

    This is how access is managed from the UI: an admin issues viewer/operator/
    admin tokens for teammates without shell access to the server. Guardrails:
    the minted role may not outrank the minter's own highest role, and the TTL
    is capped at 7 days. The token value is returned once and never stored.
    """
    from odooctl.security import tokens
    from odooctl.security.principals import Role, role_rank

    role_raw = str(body.get("role", "viewer"))
    try:
        role = Role(role_raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown role: {role_raw!r}")

    minter_rank = max((role_rank(r) for r in principal.roles), default=0)
    if role_rank(role) > minter_rank:
        raise HTTPException(
            status_code=403,
            detail=f"Cannot mint a {role.value!r} token above your own role",
        )

    try:
        ttl = int(body.get("ttl_seconds", 86400))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="ttl_seconds must be an integer")
    if not 60 <= ttl <= 7 * 86400:
        raise HTTPException(status_code=400, detail="ttl_seconds must be between 60 and 604800 (7 days)")

    project = str(body.get("project") or "*")
    environment = str(body.get("environment") or "*")
    subject = str(body.get("subject") or f"minted-by:{principal.id}")

    token = tokens.mint(
        request.app.state.api_key,
        action="api",
        environment=environment,
        project=project,
        ttl_seconds=ttl,
        subject=subject,
        roles=[role.value],
    )
    return {
        "token": token,
        "role": role.value,
        "project": project,
        "environment": environment,
        "ttl_seconds": ttl,
        "subject": subject,
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
