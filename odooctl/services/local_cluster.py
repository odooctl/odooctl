"""Reproducible k3d + Tilt production simulation."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from odooctl.adapters.kubernetes import (
    COMPONENT_LABEL,
    kubernetes_name,
    ownership_labels,
    render_kubernetes_resources,
)
from odooctl.config import KubernetesRuntimeConfig
from odooctl.utils.paths import ensure_dir
from odooctl.utils.shell import (
    CommandResult,
    run,
    run_capture_bytes,
    run_pipe_stdin,
)

if TYPE_CHECKING:
    from odooctl.services.context import ServiceContext


OWNERSHIP_SCHEMA = 1


@dataclass(frozen=True)
class LocalSimulationResult:
    cluster_name: str
    directory: Path
    k3d_config: Path
    resources: Path
    tiltfile: Path
    plan: Path
    ownership: Path


@dataclass(frozen=True)
class LocalSmokeResult:
    cluster_name: str
    database: str
    backup_path: Path
    restored_database: str
    rollback_verified: bool


def local_cluster_name(ctx: ServiceContext) -> str:
    cfg = ctx.project.config
    digest = hashlib.sha256(str(ctx.project.root).encode()).hexdigest()[:8]
    stem = kubernetes_name(
        f"{cfg.local_simulation.cluster_prefix}-{cfg.project.name}"
    )
    # k3d is stricter than Kubernetes DNS labels: cluster names are limited to
    # 32 characters. Preserve the path hash so collision resistance survives
    # truncation of long human-readable project names.
    stem = stem[:23].rstrip("-")
    return f"{stem}-{digest}"


def _require_local(ctx: ServiceContext):
    cfg = ctx.project.config
    if not cfg.local_simulation.enabled:
        raise RuntimeError(
            "Local simulation is disabled; set local_simulation.enabled: true"
        )
    if not isinstance(cfg.runtime, KubernetesRuntimeConfig):
        raise RuntimeError("Local simulation requires runtime.type: kubernetes")
    cfg.env(cfg.local_simulation.environment)
    return cfg.local_simulation


def _directory(ctx: ServiceContext, cluster_name: str) -> Path:
    root = ctx.project.resolve_path(
        ctx.project.config.local_simulation.output_path
    ).resolve()
    if not root.is_relative_to(ctx.project.root):
        raise ValueError("Local simulation output must stay inside the project root")
    return root / cluster_name


def _result_paths(ctx: ServiceContext) -> LocalSimulationResult:
    cluster = local_cluster_name(ctx)
    directory = _directory(ctx, cluster)
    return LocalSimulationResult(
        cluster_name=cluster,
        directory=directory,
        k3d_config=directory / "k3d.yaml",
        resources=directory / "resources.yaml",
        tiltfile=directory / "Tiltfile",
        plan=directory / "plan.json",
        ownership=directory / "ownership.json",
    )


def _postgres_resources(
    ctx: ServiceContext,
    environment: str,
    namespace: str,
) -> list[dict[str, Any]]:
    cfg = ctx.project.config
    local = cfg.local_simulation
    labels = ownership_labels(cfg.project.name, environment, "postgres")
    selector = {
        "odooctl.dev/project": labels["odooctl.dev/project"],
        "odooctl.dev/environment": labels["odooctl.dev/environment"],
        COMPONENT_LABEL: labels[COMPONENT_LABEL],
    }
    return [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": "postgres",
                "namespace": namespace,
                "labels": labels,
            },
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": selector},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "containers": [
                            {
                                "name": "postgres",
                                "image": local.postgres_image,
                                "ports": [
                                    {
                                        "name": "postgres",
                                        "containerPort": 5432,
                                    }
                                ],
                                "env": [
                                    {"name": "POSTGRES_USER", "value": "odoo"},
                                    {
                                        "name": "POSTGRES_DB",
                                        "value": cfg.env(environment).db_name,
                                    },
                                    {
                                        "name": "POSTGRES_HOST_AUTH_METHOD",
                                        "value": "trust",
                                    },
                                ],
                                "readinessProbe": {
                                    "exec": {
                                        "command": [
                                            "pg_isready",
                                            "-U",
                                            "odoo",
                                            "-d",
                                            cfg.env(environment).db_name,
                                        ]
                                    },
                                    "periodSeconds": 2,
                                },
                            }
                        ]
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": "postgres",
                "namespace": namespace,
                "labels": labels,
            },
            "spec": {
                "selector": selector,
                "ports": [
                    {
                        "name": "postgres",
                        "port": 5432,
                        "targetPort": "postgres",
                    }
                ],
            },
        },
    ]


def _local_resources(ctx: ServiceContext, environment: str) -> list[dict[str, Any]]:
    resources = deepcopy(render_kubernetes_resources(ctx.project, environment))
    namespace = next(
        item["metadata"]["name"] for item in resources if item["kind"] == "Namespace"
    )
    deployment = next(item for item in resources if item["kind"] == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    deployment["spec"]["template"]["spec"].setdefault(
        "securityContext",
        {"fsGroup": 101},
    )
    configured_names = {item["name"] for item in container.get("env", [])}
    for name, value in (("HOST", "postgres"), ("USER", "odoo")):
        if name not in configured_names:
            container.setdefault("env", []).append({"name": name, "value": value})
    resources.extend(_postgres_resources(ctx, environment, namespace))
    return resources


def _tiltfile(ctx: ServiceContext, environment: str) -> str:
    cfg = ctx.project.config
    local = cfg.local_simulation
    runtime = cfg.runtime
    assert isinstance(runtime, KubernetesRuntimeConfig)
    namespace = kubernetes_name(
        runtime.namespace_template.format(
            project=kubernetes_name(cfg.project.name),
            environment=kubernetes_name(environment),
        )
    )
    syncs = ",\n        ".join(
        f"sync({path!r}, '/mnt/extra-addons')"
        for path in local.live_update_paths
    )
    live_update = (
        f",\n    live_update=[\n        {syncs}\n    ]"
        if syncs
        else ""
    )
    return (
        f"allow_k8s_contexts('k3d-{local_cluster_name(ctx)}')\n"
        "k8s_yaml('resources.yaml')\n\n"
        f"docker_build({cfg.odoo.image!r}, {str(ctx.project.resolve_path(local.build_context))!r},\n"
        f"    dockerfile={str(ctx.project.resolve_path(local.dockerfile))!r}"
        f"{live_update})\n\n"
        f"k8s_resource('postgres', namespace={namespace!r}, "
        f"port_forwards=['{local.postgres_port}:5432'], labels=['database'])\n"
        f"k8s_resource('odoo', namespace={namespace!r}, resource_deps=['postgres'], "
        f"port_forwards=['{local.http_port}:8069'], labels=['application'])\n"
        f"k8s_resource('odoo-ingress', objects=['odoo:ingress:{namespace}'], "
        "resource_deps=['odoo'], labels=['ingress'])\n"
    )


def render_local_simulation(ctx: ServiceContext) -> LocalSimulationResult:
    local = _require_local(ctx)
    environment = local.environment
    result = _result_paths(ctx)
    cluster = result.cluster_name
    directory = result.directory
    ensure_dir(directory)
    dockerfile = ctx.project.resolve_path(local.dockerfile)
    if not dockerfile.is_file():
        raise FileNotFoundError(
            f"Local simulation Dockerfile not found: {dockerfile}"
        )
    for live_path in local.live_update_paths:
        resolved = ctx.project.resolve_path(live_path)
        if not resolved.exists():
            raise FileNotFoundError(
                f"Local simulation live-update path not found: {resolved}"
            )
    k3d_config = {
        "apiVersion": "k3d.io/v1alpha5",
        "kind": "Simple",
        "metadata": {"name": cluster},
        "image": local.k3s_image,
        "servers": 1,
        "agents": 1,
        "ports": [
            {
                "port": f"127.0.0.1:{local.http_port}:80",
                "nodeFilters": ["loadbalancer"],
            }
        ],
    }
    resources = _local_resources(ctx, environment)
    k3d_path = result.k3d_config
    resources_path = result.resources
    tiltfile_path = result.tiltfile
    plan_path = result.plan
    ownership_path = result.ownership
    k3d_path.write_text(yaml.safe_dump(k3d_config, sort_keys=False))
    resources_path.write_text(yaml.safe_dump_all(resources, sort_keys=False))
    tiltfile_path.write_text(_tiltfile(ctx, environment))
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": OWNERSHIP_SCHEMA,
                "cluster_name": cluster,
                "project": ctx.project.config.project.name,
                "project_root": str(ctx.project.root),
                "environment": environment,
                "kubernetes_context": f"k3d-{cluster}",
                "resources": str(resources_path),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return LocalSimulationResult(
        cluster_name=cluster,
        directory=directory,
        k3d_config=k3d_path,
        resources=resources_path,
        tiltfile=tiltfile_path,
        plan=plan_path,
        ownership=ownership_path,
    )


def _cluster_names(result: CommandResult) -> set[str]:
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError("Could not parse k3d cluster list") from exc
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected k3d cluster list response")
    return {
        item["name"]
        for item in payload
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def _list_cluster_names() -> set[str]:
    result = run(["k3d", "cluster", "list", "-o", "json"])
    return _cluster_names(result)


def _load_owned(ctx: ServiceContext, result: LocalSimulationResult) -> dict[str, Any]:
    path = result.ownership
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(
            f"Refusing local cluster mutation without ownership record: {path}"
        )
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid local cluster ownership record: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid local cluster ownership record: {path}")
    expected = {
        "schema_version": OWNERSHIP_SCHEMA,
        "cluster_name": result.cluster_name,
        "project": ctx.project.config.project.name,
        "project_root": str(ctx.project.root),
        "environment": ctx.project.config.local_simulation.environment,
    }
    mismatches = {
        key: (payload.get(key), value)
        for key, value in expected.items()
        if payload.get(key) != value
    }
    if mismatches:
        raise RuntimeError(
            f"Refusing local cluster mutation; ownership mismatch: {mismatches}"
        )
    return payload


def create_local_cluster(ctx: ServiceContext) -> LocalSimulationResult:
    result = render_local_simulation(ctx)
    names = _list_cluster_names()
    if result.cluster_name in names:
        _load_owned(ctx, result)
        return result
    if result.ownership.is_symlink() or result.ownership.exists():
        _load_owned(ctx, result)
        raise RuntimeError(
            "Refusing local cluster creation while a stale ownership record exists; "
            "run `odooctl local down --yes` first"
        )
    try:
        run(
            ["k3d", "cluster", "create", "--config", str(result.k3d_config)],
            cwd=str(ctx.project.root),
            stream=True,
        )
    except Exception:
        after = run(["k3d", "cluster", "list", "-o", "json"], check=False)
        after_names = _cluster_names(after) if after.returncode == 0 else set()
        if result.cluster_name in after_names:
            run(
                ["k3d", "cluster", "delete", result.cluster_name],
                cwd=str(ctx.project.root),
                stream=True,
                check=False,
            )
        raise
    plan = json.loads(result.plan.read_text())
    plan["created_at"] = datetime.now(timezone.utc).isoformat()
    plan["created_by"] = "odooctl"
    result.ownership.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    return result


def delete_local_cluster(ctx: ServiceContext) -> str:
    _require_local(ctx)
    result = _result_paths(ctx)
    _load_owned(ctx, result)
    names = _list_cluster_names()
    if result.cluster_name in names:
        run(
            ["k3d", "cluster", "delete", result.cluster_name],
            cwd=str(ctx.project.root),
            stream=True,
        )
    result.ownership.unlink(missing_ok=True)
    return result.cluster_name


def _kubectl(ctx: ServiceContext, cluster: str, *args: str) -> list[str]:
    return ["kubectl", "--context", f"k3d-{cluster}", *args]


def run_local_smoke(ctx: ServiceContext) -> LocalSmokeResult:
    """Exercise deploy, native neutralize, backup/restore, and rollback."""
    result = render_local_simulation(ctx)
    _load_owned(ctx, result)
    cfg = ctx.project.config
    local = cfg.local_simulation
    environment = local.environment
    env = cfg.env(environment)
    runtime = cfg.runtime
    assert isinstance(runtime, KubernetesRuntimeConfig)
    namespace = kubernetes_name(
        runtime.namespace_template.format(
            project=kubernetes_name(cfg.project.name),
            environment=kubernetes_name(environment),
        )
    )
    base = _kubectl(ctx, result.cluster_name, "--namespace", namespace)
    run(
        [
            "docker",
            "build",
            "--file",
            str(ctx.project.resolve_path(local.dockerfile)),
            "--tag",
            cfg.odoo.image,
            str(ctx.project.resolve_path(local.build_context)),
        ],
        cwd=str(ctx.project.root),
        stream=True,
    )
    run(
        [
            "k3d",
            "image",
            "import",
            cfg.odoo.image,
            "--cluster",
            result.cluster_name,
        ],
        cwd=str(ctx.project.root),
        stream=True,
    )
    run(
        _kubectl(
            ctx,
            result.cluster_name,
            "apply",
            "-f",
            str(result.resources),
        ),
        cwd=str(ctx.project.root),
        stream=True,
    )
    timeout = f"{local.rollout_timeout_seconds}s"
    run([*base, "rollout", "status", "deployment/postgres", f"--timeout={timeout}"])
    run(
        [
            *base,
            "wait",
            "--for=jsonpath={.status.phase}=Running",
            "pod",
            "-l",
            f"{COMPONENT_LABEL}=odoo",
            f"--timeout={timeout}",
        ]
    )
    run(
        [
            *base,
            "exec",
            "deployment/odoo",
            "--",
            cfg.odoo.cli_command,
            "-d",
            env.db_name,
            "-i",
            "base",
            "--stop-after-init",
            "--without-demo=all",
            "--db_host",
            "postgres",
            "--db_user",
            "odoo",
        ]
    )
    run([*base, "rollout", "restart", "deployment/odoo"])
    run([*base, "rollout", "status", "deployment/odoo", f"--timeout={timeout}"])
    run(
        [
            *base,
            "exec",
            "deployment/odoo",
            "--",
            cfg.odoo.cli_command,
            "neutralize",
            "-d",
            env.db_name,
            "--db_host",
            "postgres",
            "--db_user",
            "odoo",
        ]
    )
    smoke_dir = result.directory / "smoke"
    ensure_dir(smoke_dir)
    backup_path = smoke_dir / "db.dump"
    run_capture_bytes(
        [
            *base,
            "exec",
            "deployment/postgres",
            "--",
            "pg_dump",
            "-Fc",
            "-U",
            "odoo",
            env.db_name,
        ],
        stdout_path=backup_path,
        cwd=str(ctx.project.root),
    )
    restored = f"{env.db_name}_smoke_restore"
    run(
        [
            *base,
            "exec",
            "deployment/postgres",
            "--",
            "createdb",
            "-U",
            "odoo",
            restored,
        ]
    )
    try:
        run_pipe_stdin(
            [
                *base,
                "exec",
                "-i",
                "deployment/postgres",
                "--",
                "pg_restore",
                "-U",
                "odoo",
                "-d",
                restored,
            ],
            stdin_path=backup_path,
            cwd=str(ctx.project.root),
        )
        run(
            [
                *base,
                "exec",
                "deployment/postgres",
                "--",
                "psql",
                "-U",
                "odoo",
                "-d",
                restored,
                "-Atc",
                "SELECT 1",
            ]
        )
    finally:
        run(
            [
                *base,
                "exec",
                "deployment/postgres",
                "--",
                "dropdb",
                "--if-exists",
                "-U",
                "odoo",
                restored,
            ],
            check=False,
        )
    run(
        [
            *base,
            "set",
            "image",
            "deployment/odoo",
            "odoo=invalid.local/odooctl-smoke:missing",
        ]
    )
    failed: CommandResult | None = None
    try:
        failed = run(
            [
                *base,
                "rollout",
                "status",
                "deployment/odoo",
                "--timeout=15s",
            ],
            check=False,
        )
    finally:
        run([*base, "rollout", "undo", "deployment/odoo"])
        run([*base, "rollout", "status", "deployment/odoo", f"--timeout={timeout}"])
    if failed.returncode == 0:
        raise RuntimeError("Rollback smoke expected the invalid rollout to fail")
    return LocalSmokeResult(
        cluster_name=result.cluster_name,
        database=env.db_name,
        backup_path=backup_path,
        restored_database=restored,
        rollback_verified=True,
    )
