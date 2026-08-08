"""Declarative GitOps overlays and ephemeral pull-request environments."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from odooctl.adapters.kubernetes import (
    KubernetesAdapter,
    kubernetes_name,
    ownership_labels,
    render_kubernetes_resources,
)
from odooctl.config import KubernetesRuntimeConfig
from odooctl.utils.paths import ensure_dir

if TYPE_CHECKING:
    from odooctl.services.context import ServiceContext


EPHEMERAL_LABEL = "odooctl.dev/ephemeral"
PULL_REQUEST_LABEL = "odooctl.dev/pull-request"
EXPIRES_AT_ANNOTATION = "odooctl.dev/expires-at"
PREVIEW_ID_RE = re.compile(r"^pr-[1-9][0-9]*-[0-9a-f]{8}$")


@dataclass(frozen=True)
class GitOpsRenderResult:
    environment: str
    directory: Path
    resources: Path
    kustomization: Path
    metadata: Path | None = None


@dataclass(frozen=True)
class PreviewSpec:
    preview_id: str
    pull_request: int
    revision: str
    source_environment: str
    domain: str
    database: str
    filestore: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class CleanupResult:
    expired: tuple[str, ...]
    deleted: tuple[str, ...]


def _require_gitops(ctx: ServiceContext) -> KubernetesRuntimeConfig:
    cfg = ctx.project.config
    if not cfg.gitops.enabled:
        raise RuntimeError("GitOps rendering is disabled; set gitops.enabled: true")
    if not isinstance(cfg.runtime, KubernetesRuntimeConfig):
        raise RuntimeError("GitOps rendering requires runtime.type: kubernetes")
    return cfg.runtime


def _output_root(ctx: ServiceContext, output: str | Path | None = None) -> Path:
    configured = ctx.project.config.gitops.output_path
    root = ctx.project.resolve_path(output or configured)
    if not root.is_relative_to(ctx.project.root):
        raise ValueError("GitOps output must stay inside the project root")
    return root


def _write_yaml(path: Path, value: Any, *, multi: bool = False) -> Path:
    ensure_dir(path.parent)
    content = (
        yaml.safe_dump_all(value, sort_keys=False)
        if multi
        else yaml.safe_dump(value, sort_keys=False)
    )
    path.write_text(content, encoding="utf-8")
    return path


def _kustomization(resources: list[str]) -> dict[str, Any]:
    return {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "resources": resources,
    }


def render_environment_overlay(
    ctx: ServiceContext,
    environment: str,
    *,
    output: str | Path | None = None,
) -> GitOpsRenderResult:
    """Render an environment without contacting or applying to a cluster."""
    _require_gitops(ctx)
    ctx.project.config.env(environment)
    directory = _output_root(ctx, output) / "environments" / environment
    resources_path = _write_yaml(
        directory / "resources.yaml",
        render_kubernetes_resources(ctx.project, environment),
        multi=True,
    )
    kustomization_path = _write_yaml(
        directory / "kustomization.yaml",
        _kustomization(["resources.yaml"]),
    )
    return GitOpsRenderResult(
        environment=environment,
        directory=directory,
        resources=resources_path,
        kustomization=kustomization_path,
    )


def preview_id(pull_request: int, revision: str) -> str:
    if pull_request < 1:
        raise ValueError("pull_request must be a positive integer")
    normalized_revision = revision.lower()
    if not re.fullmatch(r"[0-9a-f]{8,64}", normalized_revision):
        raise ValueError("revision must be an 8-64 character hexadecimal commit id")
    return f"pr-{pull_request}-{normalized_revision[:8]}"


def build_preview_spec(
    ctx: ServiceContext,
    pull_request: int,
    revision: str,
    *,
    source_environment: str | None = None,
    ttl_hours: int | None = None,
    now: datetime | None = None,
) -> PreviewSpec:
    cfg = ctx.project.config
    _require_gitops(ctx)
    source = source_environment or cfg.gitops.preview_source_environment
    cfg.env(source)
    if cfg.gitops.preview_base_domain is None:
        raise RuntimeError(
            "Pull-request environments require gitops.preview_base_domain"
        )
    ttl = ttl_hours or cfg.gitops.preview_ttl_hours
    if ttl < 1 or ttl > 168:
        raise ValueError("preview TTL must be between 1 and 168 hours")
    created = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    identity = preview_id(pull_request, revision)
    db_prefix = re.sub(r"[^a-z0-9_]+", "_", cfg.project.name.lower()).strip("_")
    database = f"{db_prefix}_{identity.replace('-', '_')}"[:63].rstrip("_")
    return PreviewSpec(
        preview_id=identity,
        pull_request=pull_request,
        revision=revision.lower(),
        source_environment=source,
        domain=f"{identity}.{cfg.gitops.preview_base_domain}",
        database=database,
        filestore=database,
        created_at=created,
        expires_at=created + timedelta(hours=ttl),
    )


def _preview_environment(ctx: ServiceContext, spec: PreviewSpec):
    source = ctx.project.config.env(spec.source_environment)
    return source.model_copy(
        update={
            "tier": "development",
            "protected": False,
            "branch": f"pull/{spec.pull_request}/{spec.revision}",
            "domain": spec.domain,
            "db_name": spec.database,
            "filestore_path": spec.filestore,
            "filestore_volume": f"{spec.preview_id}-filestore",
            "clone_from": spec.source_environment,
            "sanitize": True,
            "promotes_to": None,
            "auto_deploy": False,
            "last_deployed_commit": None,
        }
    )


def _preview_initializer(
    ctx: ServiceContext,
    spec: PreviewSpec,
    namespace: str,
    generated_config: str,
) -> list[dict[str, Any]]:
    cfg = ctx.project.config
    runtime = cfg.runtime
    assert isinstance(runtime, KubernetesRuntimeConfig)
    labels = ownership_labels(cfg.project.name, spec.preview_id, "initializer")
    labels.update(
        {
            EPHEMERAL_LABEL: "true",
            PULL_REQUEST_LABEL: str(spec.pull_request),
        }
    )
    annotations = {
        EXPIRES_AT_ANNOTATION: spec.expires_at.isoformat(),
        "argocd.argoproj.io/sync-wave": "1",
        "argocd.argoproj.io/hook": "PostSync",
        "argocd.argoproj.io/hook-delete-policy": "BeforeHookCreation,HookSucceeded",
    }
    config_name = f"{spec.preview_id}-config"
    secret_env = [
        {
            "name": name,
            "valueFrom": {
                "secretKeyRef": {"name": ref.name, "key": ref.key}
            },
        }
        for name, ref in sorted(runtime.secret_refs.items())
    ]
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": config_name,
                "namespace": namespace,
                "labels": labels,
                "annotations": annotations,
            },
            "data": {"odooctl.yml": generated_config},
        },
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": f"{spec.preview_id}-initialize",
                "namespace": namespace,
                "labels": labels,
                "annotations": annotations,
            },
            "spec": {
                "backoffLimit": 2,
                "ttlSecondsAfterFinished": 3600,
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "name": "initialize",
                                "image": cfg.gitops.initializer_image,
                                "args": [
                                    "clone",
                                    spec.source_environment,
                                    spec.preview_id,
                                    "--sanitize",
                                    "--config",
                                    "/etc/odooctl/odooctl.yml",
                                ],
                                "env": secret_env,
                                "volumeMounts": [
                                    {
                                        "name": "config",
                                        "mountPath": "/etc/odooctl",
                                        "readOnly": True,
                                    }
                                ],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "config",
                                "configMap": {"name": config_name},
                            }
                        ],
                    },
                },
            },
        },
    ]


def render_preview_overlay(
    ctx: ServiceContext,
    pull_request: int,
    revision: str,
    *,
    source_environment: str | None = None,
    ttl_hours: int | None = None,
    now: datetime | None = None,
    output: str | Path | None = None,
) -> GitOpsRenderResult:
    """Render an isolated, expiring, neutralized PR environment."""
    runtime = _require_gitops(ctx)
    spec = build_preview_spec(
        ctx,
        pull_request,
        revision,
        source_environment=source_environment,
        ttl_hours=ttl_hours,
        now=now,
    )
    preview_env = _preview_environment(ctx, spec)
    labels = {
        EPHEMERAL_LABEL: "true",
        PULL_REQUEST_LABEL: str(spec.pull_request),
    }
    annotations = {EXPIRES_AT_ANNOTATION: spec.expires_at.isoformat()}
    preview_image = (
        ctx.project.config.gitops.preview_image_template.format(
            revision=spec.revision,
            pull_request=spec.pull_request,
        )
        if ctx.project.config.gitops.preview_image_template
        else None
    )
    resources = render_kubernetes_resources(
        ctx.project,
        spec.preview_id,
        environment_config=preview_env,
        additional_labels=labels,
        annotations=annotations,
        image=preview_image,
    )
    namespace = kubernetes_name(
        runtime.namespace_template.format(
            project=kubernetes_name(ctx.project.config.project.name),
            environment=spec.preview_id,
        )
    )
    generated = ctx.project.config.model_dump(mode="json", exclude_none=True)
    generated["environments"][spec.preview_id] = preview_env.model_dump(
        mode="json",
        exclude_none=True,
    )
    # The initializer authenticates with its pod ServiceAccount; a workstation
    # kube-context name must not leak into the in-cluster configuration.
    generated["runtime"].pop("context", None)
    generated_config = yaml.safe_dump(generated, sort_keys=False)
    resources.extend(
        _preview_initializer(ctx, spec, namespace, generated_config)
    )

    directory = _output_root(ctx, output) / "previews" / spec.preview_id
    resources_path = _write_yaml(
        directory / "resources.yaml",
        resources,
        multi=True,
    )
    kustomization_path = _write_yaml(
        directory / "kustomization.yaml",
        _kustomization(["resources.yaml"]),
    )
    metadata = {
        "apiVersion": "odooctl.dev/v1alpha1",
        "kind": "PreviewEnvironment",
        "metadata": {
            "name": spec.preview_id,
            "pull_request": spec.pull_request,
            "revision": spec.revision,
            "created_at": spec.created_at.isoformat(),
            "expires_at": spec.expires_at.isoformat(),
        },
        "spec": {
            "source_environment": spec.source_environment,
            "domain": spec.domain,
            "database": spec.database,
            "filestore": spec.filestore,
            "sanitize": True,
            "native_neutralization": ctx.project.config.sanitization.native_neutralize,
        },
    }
    metadata_path = _write_yaml(directory / "metadata.yaml", metadata)
    return GitOpsRenderResult(
        environment=spec.preview_id,
        directory=directory,
        resources=resources_path,
        kustomization=kustomization_path,
        metadata=metadata_path,
    )


def cleanup_expired_previews(
    ctx: ServiceContext,
    *,
    now: datetime | None = None,
    apply: bool = False,
    output: str | Path | None = None,
) -> CleanupResult:
    """Plan or delete expired preview namespaces owned by this project."""
    _require_gitops(ctx)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    previews_root = _output_root(ctx, output) / "previews"
    if not previews_root.exists():
        return CleanupResult(expired=(), deleted=())
    expired: list[str] = []
    deleted: list[str] = []
    for metadata_path in sorted(previews_root.glob("*/metadata.yaml")):
        if metadata_path.is_symlink() or not metadata_path.is_file():
            continue
        payload = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
        metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
        identity = metadata.get("name")
        expires_raw = metadata.get("expires_at")
        if not isinstance(identity, str) or not PREVIEW_ID_RE.fullmatch(identity):
            raise RuntimeError(f"Invalid preview metadata identity in {metadata_path}")
        try:
            expires = datetime.fromisoformat(str(expires_raw))
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid preview expiry in {metadata_path}"
            ) from exc
        if expires.tzinfo is None:
            raise RuntimeError(f"Preview expiry must include a timezone: {metadata_path}")
        if expires.astimezone(timezone.utc) > current:
            continue
        expired.append(identity)
        if apply:
            KubernetesAdapter(ctx.project, identity).delete()
            deleted.append(identity)
    return CleanupResult(expired=tuple(expired), deleted=tuple(deleted))
