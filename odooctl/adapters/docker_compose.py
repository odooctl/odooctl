from __future__ import annotations
from pathlib import Path
from odooctl.utils.shell import run, run_capture_bytes, run_pipe_stdin
from odooctl.utils.shell import CommandResult

class DockerComposeAdapter:
    runtime_type = "docker_compose"

    def __init__(self, compose_file: str = "docker-compose.yml", project_dir: str | None = None):
        self.compose_file = compose_file
        self.project_dir = project_dir

    def _cmd(self, *args: str) -> list[str]:
        return ["docker", "compose", "-f", self.compose_file, *args]

    def pull(self, service: str | None = None) -> None:
        run(self._cmd("pull", *([service] if service else [])), cwd=self.project_dir, stream=True)

    def build(self, service: str | None = None) -> None:
        run(self._cmd("build", *([service] if service else [])), cwd=self.project_dir, stream=True)

    def up(self, service: str | None = None) -> None:
        run(self._cmd("up", "-d", *([service] if service else [])), cwd=self.project_dir, stream=True)

    def restart(self, service: str) -> None:
        run(self._cmd("restart", service), cwd=self.project_dir, stream=True)

    def delete(self, service: str | None = None) -> None:
        args = ["rm", "--force", "--stop"]
        if service:
            args.append(service)
        run(self._cmd(*args), cwd=self.project_dir, stream=True)

    def logs(self, service: str | None = None, *, follow: bool = True, tail: int | None = None) -> None:
        args = ["logs"]
        if follow:
            args.append("-f")
        if tail is not None:
            args.extend(["--tail", str(tail)])
        if service:
            args.append(service)
        run(self._cmd(*args), cwd=self.project_dir, stream=True)

    def ps(self) -> str:
        return run(self._cmd("ps"), cwd=self.project_dir, check=False).stdout

    def exec(
        self,
        service: str,
        args: list[str],
        *,
        stream: bool = True,
        extra_env: dict[str, str] | None = None,
        check: bool = True,
    ) -> CommandResult:
        """Run a command inside a compose service.

        ``extra_env`` values are injected into the container via name-only
        ``-e NAME`` flags, with the actual values supplied through the
        subprocess environment — so secrets never appear on argv.
        """
        env_flags: list[str] = []
        for name in extra_env or {}:
            env_flags.extend(["-e", name])
        return run(
            self._cmd("exec", "-T", *env_flags, service, *args),
            cwd=self.project_dir,
            env=extra_env,
            stream=stream,
            check=check,
        )

    def exec_capture_bytes(self, service: str, args: list[str], *, stdout_path: str | Path) -> None:
        run_capture_bytes(self._cmd("exec", "-T", service, *args), cwd=self.project_dir, stdout_path=stdout_path)

    def exec_pipe_stdin(self, service: str, args: list[str], *, stdin_path: str | Path) -> None:
        run_pipe_stdin(self._cmd("exec", "-T", service, *args), cwd=self.project_dir, stdin_path=stdin_path)
