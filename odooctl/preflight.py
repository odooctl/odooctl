from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from odooctl.context import ProjectContext


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    message: str


def _exists_check(name: str, path: Path, *, kind: str) -> CheckResult:
    if path.exists():
        return CheckResult(name, True, f"{kind} exists: {path}")
    return CheckResult(name, False, f"{kind} missing: {path}")


def run_preflight(ctx: ProjectContext) -> list[CheckResult]:
    """Run side-effect-free project readiness checks.

    These checks intentionally avoid network calls and container execution so
    `odooctl doctor` is safe on developer machines and CI. Deeper execution-mode
    checks will be added as Docker-native backends land.
    """

    cfg = ctx.config
    checks: list[CheckResult] = [
        CheckResult("config", True, f"config loaded: {ctx.config_path}"),
        _exists_check("project_root", ctx.root, kind="project root"),
        _exists_check("compose_file", ctx.compose_file, kind="compose file"),
    ]

    missing_env = cfg.missing_env_vars(include_snapshot=True)
    if missing_env:
        checks.append(CheckResult("environment", False, "missing environment variables: " + ", ".join(missing_env)))
    else:
        checks.append(CheckResult("environment", True, "all referenced environment variables are set"))

    weak_secret_vars: list[str] = []
    ignored_secret_vars: list[str] = []
    ignored_values = set(cfg.redaction.ignore_values)
    for env_name in cfg.referenced_env_vars(include_snapshot=True):
        value = os.getenv(env_name)
        if not value:
            continue
        if len(value) < cfg.redaction.min_secret_length:
            weak_secret_vars.append(env_name)
        if value in ignored_values:
            ignored_secret_vars.append(env_name)
    if weak_secret_vars or ignored_secret_vars:
        details = []
        if weak_secret_vars:
            details.append("short/common length: " + ", ".join(sorted(weak_secret_vars)))
        if ignored_secret_vars:
            details.append("ignored by redaction policy: " + ", ".join(sorted(ignored_secret_vars)))
        checks.append(CheckResult("redaction_secret_quality", True, "warning: " + "; ".join(details)))

    for sql_file in ctx.sanitization_sql_files():
        checks.append(_exists_check(f"sanitization_sql:{sql_file.name}", sql_file, kind="sanitization SQL file"))

    return checks


def checks_ok(checks: list[CheckResult]) -> bool:
    return all(check.ok for check in checks)
