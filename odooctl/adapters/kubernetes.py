"""Kubernetes workload runtime with fail-closed ownership checks."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from odooctl.config import KubernetesRuntimeConfig
from odooctl.utils.paths import ensure_dir
from odooctl.utils.shell import CommandResult, run, run_capture_bytes, run_pipe_stdin

if TYPE_CHECKING:
    from odooctl.context import ProjectContext


MANAGED_BY = "odooctl"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
PROJECT_LABEL = "odooctl.dev/project"
ENVIRONMENT_LABEL = "odooctl.dev/environment"
COMPONENT_LABEL = "app.kubernetes.io/component"


def kubernetes_name(value: str) -> str:
    """Return a stable DNS-label-safe name."""
    normalized = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    normalized = re.sub(r"-+", "-", normalized)
    if not normalized:
        raise ValueError(f"Cannot derive a Kubernetes name from {value!r}")
    return normalized[:63].rstrip("-")


def ownership_labels(project: str, environment: str, component: str) -> dict[str, str]:
    return {
        MANAGED_BY_LABEL: MANAGED_BY,
        PROJECT_LABEL: kubernetes_name(project),
        ENVIRONMENT_LABEL: kubernetes_name(environment),
        COMPONENT_LABEL: kubernetes_name(component),
    }


def render_kubernetes_resources(
    project: ProjectContext,
    environment: str,
) -> list[dict[str, Any]]:
    """Render the canonical resources used by production and local clusters."""
    cfg = project.config
    runtime = cfg.runtime
    if not isinstance(runtime, KubernetesRuntimeConfig):
        raise ValueError("Kubernetes resources require runtime.type: kubernetes")
    env = cfg.env(environment)
    project_name = kubernetes_name(cfg.project.name)
    environment_name = kubernetes_name(environment)
    namespace = kubernetes_name(
        runtime.namespace_template.format(
            project=project_name,
            environment=environment_name,
        )
    )
    workload = kubernetes_name(cfg.odoo.service)
    labels = ownership_labels(cfg.project.name, environment, "odoo")
    selector_labels = {
        PROJECT_LABEL: labels[PROJECT_LABEL],
        ENVIRONMENT_LABEL: labels[ENVIRONMENT_LABEL],
        COMPONENT_LABEL: labels[COMPONENT_LABEL],
    }
    env_refs = [
        {
            "name": name,
            "valueFrom": {
                "secretKeyRef": {
                    "name": ref.name,
                    "key": ref.key,
                }
            },
        }
        for name, ref in sorted(runtime.secret_refs.items())
    ]
    resources: list[dict[str, Any]] = [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": namespace, "labels": labels},
        },
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": f"{workload}-filestore",
                "namespace": namespace,
                "labels": labels,
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {
                    "requests": {"storage": runtime.filestore_claim_size}
                },
                **(
                    {"storageClassName": runtime.storage_class}
                    if runtime.storage_class
                    else {}
                ),
            },
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": workload,
                "namespace": namespace,
                "labels": labels,
            },
            "spec": {
                "replicas": runtime.replicas,
                "selector": {"matchLabels": selector_labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "containers": [
                            {
                                "name": workload,
                                "image": cfg.odoo.image,
                                "imagePullPolicy": runtime.image_pull_policy,
                                "ports": [
                                    {
                                        "name": "http",
                                        "containerPort": runtime.container_port,
                                    }
                                ],
                                "env": env_refs,
                                "volumeMounts": [
                                    {
                                        "name": "filestore",
                                        "mountPath": cfg.odoo.filestore_container_path,
                                    }
                                ],
                                "readinessProbe": {
                                    "httpGet": {
                                        "path": cfg.healthcheck.path,
                                        "port": "http",
                                    },
                                    "timeoutSeconds": cfg.healthcheck.timeout_seconds,
                                    "periodSeconds": max(
                                        1,
                                        cfg.healthcheck.interval_seconds,
                                    ),
                                },
                            }
                        ],
                        "volumes": [
                            {
                                "name": "filestore",
                                "persistentVolumeClaim": {
                                    "claimName": f"{workload}-filestore"
                                },
                            }
                        ],
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": workload,
                "namespace": namespace,
                "labels": labels,
            },
            "spec": {
                "selector": selector_labels,
                "ports": [
                    {
                        "name": "http",
                        "port": runtime.service_port,
                        "targetPort": "http",
                    }
                ],
            },
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {
                "name": workload,
                "namespace": namespace,
                "labels": labels,
                **(
                    {"annotations": {"kubernetes.io/ingress.class": runtime.ingress_class}}
                    if runtime.ingress_class
                    else {}
                ),
            },
            "spec": {
                "rules": [
                    {
                        "host": env.domain,
                        "http": {
                            "paths": [
                                {
                                    "path": "/",
                                    "pathType": "Prefix",
                                    "backend": {
                                        "service": {
                                            "name": workload,
                                            "port": {"name": "http"},
                                        }
                                    },
                                }
                            ]
                        },
                    }
                ]
            },
        },
    ]
    return resources


class KubernetesAdapter:
    runtime_type = "kubernetes"

    def __init__(self, project: ProjectContext, environment: str):
        self.project = project
        self.environment = environment
        runtime = project.config.runtime
        if not isinstance(runtime, KubernetesRuntimeConfig):
            raise ValueError("KubernetesAdapter requires runtime.type: kubernetes")
        self.config = runtime
        project_name = kubernetes_name(project.config.project.name)
        environment_name = kubernetes_name(environment)
        self.namespace = kubernetes_name(
            runtime.namespace_template.format(
                project=project_name,
                environment=environment_name,
            )
        )
        self.workload = kubernetes_name(project.config.odoo.service)
        self._expected_labels = ownership_labels(
            project.config.project.name,
            environment,
            "odoo",
        )

    def _cmd(self, *args: str, namespaced: bool = True) -> list[str]:
        command = [self.config.kubectl_command]
        if self.config.kubectl_command == "k3s":
            command.append("kubectl")
        if self.config.context:
            command.extend(["--context", self.config.context])
        if namespaced:
            command.extend(["--namespace", self.namespace])
        command.extend(args)
        return command

    def _resource_identities(self) -> list[tuple[str, str, bool]]:
        return [
            ("namespace", self.namespace, False),
            ("persistentvolumeclaim", f"{self.workload}-filestore", True),
            ("deployment", self.workload, True),
            ("service", self.workload, True),
            ("ingress", self.workload, True),
        ]

    def _assert_owned_or_absent(
        self,
        kind: str,
        name: str,
        *,
        namespaced: bool,
        absent_ok: bool,
    ) -> None:
        result = run(
            self._cmd("get", kind, name, "-o", "json", namespaced=namespaced),
            cwd=str(self.project.root),
            check=False,
            stream=False,
        )
        if result.returncode != 0:
            output = f"{result.stdout}\n{result.stderr}".lower()
            if absent_ok and ("notfound" in output or "not found" in output):
                return
            raise RuntimeError(
                f"Could not verify ownership for {kind}/{name}: "
                f"{result.stderr or result.stdout}"
            )
        try:
            current = json.loads(result.stdout)
            labels = current["metadata"].get("labels", {})
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RuntimeError(
                f"Could not parse ownership metadata for {kind}/{name}"
            ) from exc
        mismatches = {
            key: (labels.get(key), expected)
            for key, expected in self._expected_labels.items()
            if labels.get(key) != expected
        }
        if mismatches:
            raise RuntimeError(
                f"Refusing to mutate unowned Kubernetes resource {kind}/{name}; "
                f"label mismatch: {mismatches}"
            )

    def _assert_workload_owned(self, workload: str) -> None:
        name = kubernetes_name(workload)
        self._assert_owned_or_absent(
            "deployment",
            name,
            namespaced=True,
            absent_ok=False,
        )

    def render_resources(self, output: str | Path | None = None) -> Path:
        resources = render_kubernetes_resources(self.project, self.environment)
        if output is None:
            output_dir = self.project.resolve_path(self.config.manifests_path)
            output = output_dir / self.environment / "resources.yaml"
        output_path = Path(output)
        ensure_dir(output_path.parent)
        output_path.write_text(
            yaml.safe_dump_all(resources, sort_keys=False),
            encoding="utf-8",
        )
        return output_path

    def pull(self, workload: str | None = None) -> None:
        # Kubernetes pulls according to imagePullPolicy during apply/rollout.
        return None

    def build(self, workload: str | None = None) -> None:
        raise RuntimeError(
            "The Kubernetes runtime does not build images; publish an image "
            "or use the k3d/Tilt development workflow"
        )

    def up(self, workload: str | None = None) -> None:
        for kind, name, namespaced in self._resource_identities():
            self._assert_owned_or_absent(
                kind,
                name,
                namespaced=namespaced,
                absent_ok=True,
            )
        manifest = self.render_resources()
        run(
            self._cmd("apply", "-f", str(manifest), namespaced=False),
            cwd=str(self.project.root),
            stream=True,
        )
        run(
            self._cmd(
                "rollout",
                "status",
                f"deployment/{kubernetes_name(workload or self.workload)}",
            ),
            cwd=str(self.project.root),
            stream=True,
        )

    def restart(self, workload: str) -> None:
        name = kubernetes_name(workload)
        self._assert_workload_owned(name)
        run(
            self._cmd("rollout", "restart", f"deployment/{name}"),
            cwd=str(self.project.root),
            stream=True,
        )
        run(
            self._cmd("rollout", "status", f"deployment/{name}"),
            cwd=str(self.project.root),
            stream=True,
        )

    def delete(self, workload: str | None = None) -> None:
        if workload:
            name = kubernetes_name(workload)
            self._assert_workload_owned(name)
            run(
                self._cmd("delete", "deployment", name),
                cwd=str(self.project.root),
                stream=True,
            )
            return
        self._assert_owned_or_absent(
            "namespace",
            self.namespace,
            namespaced=False,
            absent_ok=False,
        )
        run(
            self._cmd("delete", "namespace", self.namespace, namespaced=False),
            cwd=str(self.project.root),
            stream=True,
        )

    def logs(
        self,
        workload: str | None = None,
        *,
        follow: bool = True,
        tail: int | None = None,
    ) -> None:
        name = kubernetes_name(workload or self.workload)
        self._assert_workload_owned(name)
        args = ["logs", f"deployment/{name}"]
        if follow:
            args.append("--follow")
        if tail is not None:
            args.extend(["--tail", str(tail)])
        run(self._cmd(*args), cwd=str(self.project.root), stream=True)

    def ps(self) -> str:
        selector = (
            f"{MANAGED_BY_LABEL}={MANAGED_BY},"
            f"{PROJECT_LABEL}={self._expected_labels[PROJECT_LABEL]},"
            f"{ENVIRONMENT_LABEL}={self._expected_labels[ENVIRONMENT_LABEL]}"
        )
        result = run(
            self._cmd("get", "deployments", "-l", selector, "-o", "json"),
            cwd=str(self.project.root),
            stream=False,
        )
        payload = json.loads(result.stdout)
        lines: list[str] = []
        for item in payload.get("items", []):
            metadata = item.get("metadata", {})
            labels = metadata.get("labels", {})
            if any(labels.get(key) != value for key, value in self._expected_labels.items()):
                continue
            desired = item.get("spec", {}).get("replicas", 0)
            available = item.get("status", {}).get("availableReplicas", 0)
            state = "running" if desired > 0 and available >= desired else "stopped"
            lines.append(f"{metadata.get('name', 'unknown')} {state}")
        if self.config.postgres_mode == "external":
            lines.append(f"{self.project.config.postgres.service} running external")
        return "\n".join(lines)

    def exec(
        self,
        workload: str,
        args: list[str],
        *,
        stream: bool = True,
        extra_env: dict[str, str] | None = None,
        check: bool = True,
    ) -> CommandResult:
        name = kubernetes_name(workload)
        self._assert_workload_owned(name)
        missing_refs = sorted(set(extra_env or {}) - set(self.config.secret_refs))
        if missing_refs:
            raise RuntimeError(
                "Kubernetes exec refuses local environment values without "
                "configured Secret references: "
                + ", ".join(missing_refs)
            )
        # Referenced variables already exist in the pod environment. Their
        # values are deliberately never copied to kubectl argv or manifests.
        return run(
            self._cmd("exec", f"deployment/{name}", "--", *args),
            cwd=str(self.project.root),
            stream=stream,
            check=check,
        )

    def exec_capture_bytes(
        self,
        workload: str,
        args: list[str],
        *,
        stdout_path: str | Path,
    ) -> None:
        name = kubernetes_name(workload)
        self._assert_workload_owned(name)
        run_capture_bytes(
            self._cmd("exec", f"deployment/{name}", "--", *args),
            cwd=str(self.project.root),
            stdout_path=stdout_path,
        )

    def exec_pipe_stdin(
        self,
        workload: str,
        args: list[str],
        *,
        stdin_path: str | Path,
    ) -> None:
        name = kubernetes_name(workload)
        self._assert_workload_owned(name)
        run_pipe_stdin(
            self._cmd("exec", "-i", f"deployment/{name}", "--", *args),
            cwd=str(self.project.root),
            stdin_path=stdin_path,
        )
