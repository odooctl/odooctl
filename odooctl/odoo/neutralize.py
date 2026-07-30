"""Native Odoo neutralization followed by odooctl extension safeguards."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from odooctl.config import EnvironmentConfig, OdooCtlConfig
from odooctl.odoo.sanitize import profile_sql, sanitize_database, verification_checks
from odooctl.utils.shell import CommandResult, run


NativeStatus = Literal["executed", "unsupported", "disabled"]
NativeCapability = Literal["supported", "unsupported", "disabled"]
UNSUPPORTED_PATTERNS = (
    r"unknown command\s+['\"]?neutralize\b",
    r"unrecognized command\s+['\"]?neutralize\b",
    r"invalid command\s+['\"]?neutralize\b",
    r"no such command[:\s]+['\"]?neutralize\b",
    r"unknown option[:\s]+['\"]?neutralize\b",
)


class ComposeExecutor(Protocol):
    def exec(
        self,
        service: str,
        args: list[str],
        *,
        stream: bool = True,
        extra_env: dict[str, str] | None = None,
        check: bool = True,
    ) -> CommandResult: ...


class PsqlAdapter(Protocol):
    def psql(self, db_name: str, sql: str) -> None: ...
    def psql_file(self, db_name: str, sql_file: str | Path) -> None: ...


@dataclass(frozen=True)
class NeutralizationResult:
    policy: str
    native_status: NativeStatus
    profile: str
    extension_statements: int
    custom_sql_files: int
    verification_checks: tuple[str, ...]


def build_neutralize_args(
    cfg: OdooCtlConfig,
    db_name: str,
    *,
    stdout: bool = False,
) -> list[str]:
    """Build a native Odoo neutralize invocation without putting secrets on argv."""
    args = [cfg.odoo.cli_command]
    if cfg.odoo.addons_paths:
        # The Odoo command dispatcher accepts only the equals form before the
        # subcommand. Other shared options belong after ``neutralize``.
        args.append(f"--addons-path={','.join(cfg.odoo.addons_paths)}")
    args.extend(["neutralize", "-d", db_name])
    if stdout:
        args.append("--stdout")
    if cfg.odoo.config_path:
        args.extend(["-c", cfg.odoo.config_path])
    if cfg.odoo.db_host:
        args.extend(["--db_host", cfg.odoo.db_host])
    if cfg.odoo.db_user:
        args.extend(["--db_user", cfg.odoo.db_user])
    return args


def build_neutralize_probe_args(cfg: OdooCtlConfig) -> list[str]:
    """Build a side-effect-free command capability probe."""
    args = [cfg.odoo.cli_command]
    if cfg.odoo.addons_paths:
        args.append(f"--addons-path={','.join(cfg.odoo.addons_paths)}")
    args.extend(["neutralize", "--help"])
    return args


def _password_env(cfg: OdooCtlConfig) -> dict[str, str] | None:
    env_name = cfg.odoo.db_password_env
    if not env_name:
        return None
    value = os.getenv(env_name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {env_name}")
    return {"PGPASSWORD": value}


def _looks_unsupported(result: CommandResult) -> bool:
    output = f"{result.stdout}\n{result.stderr}".lower()
    return any(re.search(pattern, output) for pattern in UNSUPPORTED_PATTERNS)


def _probe_native_compose(
    compose: ComposeExecutor,
    cfg: OdooCtlConfig,
) -> NativeCapability:
    probe = compose.exec(
        cfg.odoo.service,
        build_neutralize_probe_args(cfg),
        stream=False,
        check=False,
    )
    if probe.returncode != 0:
        if _looks_unsupported(probe):
            return "unsupported"
        raise RuntimeError(
            "Native Odoo neutralization capability probe failed; refusing to "
            f"treat the database as sanitized: {probe.stderr or probe.stdout}"
        )
    return "supported"


def _execute_native_compose(
    compose: ComposeExecutor,
    cfg: OdooCtlConfig,
    db_name: str,
) -> None:
    env = _password_env(cfg)
    compose.exec(
        cfg.odoo.service,
        build_neutralize_args(cfg, db_name),
        stream=True,
        extra_env=env,
    )


def _probe_native_host(cfg: OdooCtlConfig) -> NativeCapability:
    probe = run(
        build_neutralize_probe_args(cfg),
        stream=False,
        check=False,
    )
    if probe.returncode != 0:
        if _looks_unsupported(probe):
            return "unsupported"
        raise RuntimeError(
            "Native Odoo neutralization capability probe failed; refusing to "
            f"treat the database as sanitized: {probe.stderr or probe.stdout}"
        )
    return "supported"


def _execute_native_host(cfg: OdooCtlConfig, db_name: str) -> None:
    run(build_neutralize_args(cfg, db_name), stream=True, env=_password_env(cfg))


def probe_native_neutralization(
    cfg: OdooCtlConfig,
    *,
    compose: ComposeExecutor | None = None,
) -> NativeCapability:
    """Probe native command support before any database or filestore mutation."""
    policy = cfg.sanitization.native_neutralize
    if policy == "disabled":
        return "disabled"
    capability = (
        _probe_native_compose(compose, cfg)
        if compose is not None
        else _probe_native_host(cfg)
    )
    if capability == "unsupported" and policy == "required":
        raise RuntimeError(
            "Native Odoo neutralization is required, but the configured "
            "Odoo runtime does not provide the neutralize command"
        )
    return capability


def neutralize_database(
    *,
    pg: PsqlAdapter,
    db_name: str,
    env: EnvironmentConfig,
    cfg: OdooCtlConfig,
    profile: str = "normal",
    sql_files: list[Path] | None = None,
    compose: ComposeExecutor | None = None,
    native_capability: NativeCapability | None = None,
) -> NeutralizationResult:
    """Neutralize *db_name* and verify odooctl's mandatory safety invariants.

    ``preferred`` falls back only when the runtime explicitly reports that the
    native command is unsupported. Connectivity, configuration, and execution
    failures remain fatal because a partially neutralized clone is unsafe.
    """
    policy = cfg.sanitization.native_neutralize
    native_status: NativeStatus = "disabled"
    capability = native_capability or probe_native_neutralization(cfg, compose=compose)
    if capability == "supported":
        if compose is not None:
            _execute_native_compose(compose, cfg, db_name)
        else:
            _execute_native_host(cfg, db_name)
        native_status = "executed"
    elif capability == "unsupported":
        native_status = "unsupported"

    resolved_files = list(sql_files or [])
    sanitize_database(
        pg,
        db_name,
        env,
        cfg,
        profile,
        sql_files=resolved_files,
    )
    checks = verification_checks(profile, env, cfg)
    for _, sql in checks:
        pg.psql(db_name, sql)
    return NeutralizationResult(
        policy=policy,
        native_status=native_status,
        profile=profile,
        extension_statements=len(profile_sql(profile, env, cfg)),
        custom_sql_files=len(resolved_files),
        verification_checks=tuple(name for name, _ in checks),
    )
