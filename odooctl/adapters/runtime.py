"""Workload runtime contract and adapter factory.

The service layer depends on this module instead of constructing a concrete
orchestrator.  Runtime implementations intentionally expose workload concepts
(deploy, restart, exec, logs, status) rather than Docker-specific containers.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from odooctl.utils.shell import CommandResult

if TYPE_CHECKING:
    from odooctl.context import ProjectContext


@runtime_checkable
class RuntimeAdapter(Protocol):
    """Operations required by Odoo lifecycle services."""

    runtime_type: str

    def pull(self, workload: str | None = None) -> None: ...

    def build(self, workload: str | None = None) -> None: ...

    def up(self, workload: str | None = None) -> None: ...

    def restart(self, workload: str) -> None: ...

    def delete(self, workload: str | None = None) -> None: ...

    def logs(
        self,
        workload: str | None = None,
        *,
        follow: bool = True,
        tail: int | None = None,
    ) -> None: ...

    def ps(self) -> str: ...

    def exec(
        self,
        workload: str,
        args: list[str],
        *,
        stream: bool = True,
        extra_env: dict[str, str] | None = None,
        check: bool = True,
    ) -> CommandResult: ...

    def exec_capture_bytes(
        self,
        workload: str,
        args: list[str],
        *,
        stdout_path: str | Path,
    ) -> None: ...

    def exec_pipe_stdin(
        self,
        workload: str,
        args: list[str],
        *,
        stdin_path: str | Path,
    ) -> None: ...


def make_runtime_adapter(
    project: ProjectContext,
    *,
    environment: str | None = None,
    compose_adapter_cls: type | None = None,
) -> RuntimeAdapter:
    """Create the configured runtime without leaking provider choices.

    ``compose_adapter_cls`` exists for dependency injection in callers and
    tests.  Kubernetes support is registered here by R7, keeping the public
    service contract stable.
    """
    runtime = project.config.runtime
    if runtime.type == "docker_compose":
        if compose_adapter_cls is None:
            from odooctl.adapters.docker_compose import DockerComposeAdapter

            compose_adapter_cls = DockerComposeAdapter
        return compose_adapter_cls(runtime.compose_file, project_dir=str(project.root))
    if runtime.type == "kubernetes":
        if environment is None:
            raise ValueError("The Kubernetes runtime requires an environment")
        from odooctl.adapters.kubernetes import KubernetesAdapter

        return KubernetesAdapter(project, environment)
    raise ValueError(f"Unsupported runtime type: {runtime.type}")


def validate_runtime_definition(project: ProjectContext) -> None:
    """Validate provider-owned files before a lifecycle mutation."""
    if project.config.runtime.type == "docker_compose":
        compose_path = project.compose_file
        if not compose_path.is_file():
            raise FileNotFoundError(f"Compose file not found: {compose_path}")
