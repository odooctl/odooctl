from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from odooctl.context import ProjectContext

SCHEDULE_COMMANDS = (
    "backup",
    "backup-remote-verify",
    "dr-drill",
    "doctor",
    "pitr-base",
    "pitr-reconcile",
)

_UNSAFE_UNIT_COMPONENT = re.compile(r"[^A-Za-z0-9_-]+")


def _unit_component(value: str, *, fallback: str, max_length: int = 40) -> str:
    """Return one deterministic, portable systemd unit-name component."""

    normalized = _UNSAFE_UNIT_COMPONENT.sub("-", value).strip("-_")
    return (normalized or fallback)[:max_length]


@dataclass(frozen=True)
class ScheduleSpec:
    command: str
    environment: str
    project_root: Path
    config_path: Path
    interval: str
    user: str | None = None
    odooctl_bin: str = "odooctl"
    environment_file: Path | None = None
    project_name: str = "project"

    @property
    def unit_name(self) -> str:
        # Human-readable components are lossy. Hash their raw values together
        # with project/root identity so neither separate checkouts nor values
        # such as ``prod.eu`` and ``prod-eu`` can overwrite the same units.
        identity = "\0".join(
            (
                self.project_name,
                str(self.project_root.resolve()),
                self.command,
                self.environment,
            )
        ).encode()
        namespace_digest = hashlib.sha256(identity).hexdigest()[:12]
        project = _unit_component(self.project_name, fallback="project")
        command = _unit_component(self.command, fallback="command")
        environment = _unit_component(self.environment, fallback="environment")
        return f"odooctl-{project}-{namespace_digest}-{command}-{environment}"

    @property
    def invocation_tokens(self) -> tuple[str, ...]:
        command_tokens: tuple[str, ...]
        if self.command == "backup":
            command_tokens = ("backup", self.environment, "--verify")
        elif self.command == "backup-remote-verify":
            command_tokens = ("backup-remote", "verify", self.environment)
        elif self.command == "dr-drill":
            command_tokens = ("dr", "drill", self.environment)
        elif self.command == "doctor":
            # `doctor` is project-wide and does not accept an environment.
            command_tokens = ("doctor",)
        elif self.command == "pitr-base":
            command_tokens = (
                "pitr",
                "base",
                "create",
                self.environment,
            )
        elif self.command == "pitr-reconcile":
            command_tokens = (
                "pitr",
                "retention",
                "reconcile",
                self.environment,
            )
        else:  # pragma: no cover - build_spec validates public construction
            raise ValueError(f"Unsupported schedule command: {self.command}")
        return (
            self.odooctl_bin,
            "--project-dir",
            str(self.project_root),
            *command_tokens,
            "--config",
            str(self.config_path),
        )

    @property
    def invocation(self) -> str:
        """Shell-safe rendering retained for callers of the original property."""
        return shlex.join(self.invocation_tokens)


def build_spec(
    command: str,
    environment: str,
    config_path: str = "odooctl.yml",
    *,
    interval: str = "daily",
    user: str | None = None,
    odooctl_bin: str = "odooctl",
    environment_file: str | Path | None = None,
) -> ScheduleSpec:
    ctx = ProjectContext.from_config_path(config_path)
    cfg = ctx.config
    if command not in SCHEDULE_COMMANDS:
        raise ValueError("schedule command must be one of: " + ", ".join(SCHEDULE_COMMANDS))
    if environment not in cfg.environments:
        raise ValueError(f"Unknown environment: {environment}")
    if command.startswith("pitr-"):
        if not cfg.pitr.enabled:
            raise ValueError("PITR scheduling requires pitr.enabled: true")
        if environment != cfg.pitr.environment:
            raise ValueError(
                "PITR is bound to environment "
                f"{cfg.pitr.environment!r}, not {environment!r}"
            )
    resolved_environment_file = None
    if environment_file is not None:
        candidate = Path(environment_file).expanduser()
        resolved_environment_file = (
            candidate if candidate.is_absolute() else (ctx.root / candidate).resolve()
        )
    return ScheduleSpec(
        command=command,
        environment=environment,
        project_root=ctx.root,
        config_path=ctx.config_path,
        interval=interval,
        project_name=cfg.project.name,
        user=user,
        odooctl_bin=odooctl_bin,
        environment_file=resolved_environment_file,
    )


def render_systemd(spec: ScheduleSpec) -> str:
    user_line = f"User={spec.user}\n" if spec.user else ""
    environment_line = (
        f"EnvironmentFile={shlex.quote(str(spec.environment_file))}\n"
        if spec.environment_file
        else ""
    )
    return f"""# /etc/systemd/system/{spec.unit_name}.service
[Unit]
Description=Run odooctl {spec.command} for {spec.environment}

[Service]
Type=oneshot
WorkingDirectory={spec.project_root}
{user_line}{environment_line}ExecStart={spec.invocation}

# /etc/systemd/system/{spec.unit_name}.timer
[Unit]
Description=Schedule odooctl {spec.command} for {spec.environment}

[Timer]
OnCalendar={spec.interval}
Persistent=true

[Install]
WantedBy=timers.target
"""


def render_cron(spec: ScheduleSpec) -> str:
    cron_expr = _cron_expression(spec.interval)
    environment_load = ""
    if spec.environment_file:
        environment_load = f"set -a && . {shlex.quote(str(spec.environment_file))} && set +a && "
    cd_and_run = f"cd {shlex.quote(str(spec.project_root))} && {environment_load}{spec.invocation}"
    if spec.user:
        return f"{cron_expr} {spec.user} {cd_and_run}\n"
    return f"{cron_expr} {cd_and_run}\n"


def _cron_expression(interval: str) -> str:
    aliases = {
        "hourly": "0 * * * *",
        "daily": "0 2 * * *",
        "weekly": "0 2 * * 0",
    }
    return aliases.get(interval, interval)


def render(
    command: str,
    environment: str,
    config_path: str = "odooctl.yml",
    *,
    format: str = "systemd",
    interval: str = "daily",
    user: str | None = None,
    odooctl_bin: str = "odooctl",
    environment_file: str | Path | None = None,
) -> str:
    spec = build_spec(
        command,
        environment,
        config_path,
        interval=interval,
        user=user,
        odooctl_bin=odooctl_bin,
        environment_file=environment_file,
    )
    if format == "systemd":
        return render_systemd(spec)
    if format == "cron":
        return render_cron(spec)
    raise ValueError("schedule format must be one of: systemd, cron")
