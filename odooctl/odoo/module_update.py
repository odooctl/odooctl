from __future__ import annotations
import os

from odooctl.adapters.runtime import RuntimeAdapter
from odooctl.utils.shell import join_csv, run


def build_update_modules_args(
    db_name: str,
    modules: list[str],
    *,
    cli_command: str = "odoo",
    db_host: str | None = None,
    db_user: str | None = None,
    config_path: str | None = None,
) -> list[str]:
    args = [cli_command, "-d", db_name, "-u", join_csv(modules), "--stop-after-init"]
    if config_path:
        args.extend(["-c", config_path])
    if db_host:
        args.extend(["--db_host", db_host])
    if db_user:
        args.extend(["--db_user", db_user])
    return args


def _resolve_password_env(db_password_env: str | None) -> dict[str, str] | None:
    """Resolve the configured password env var to a PGPASSWORD mapping.

    The database password never appears on argv. It is handed to the Odoo
    process through the PGPASSWORD environment variable, which psycopg2
    honours natively.
    """
    if not db_password_env:
        return None
    value = os.getenv(db_password_env)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {db_password_env}")
    return {"PGPASSWORD": value}


def update_modules_local(
    db_name: str,
    modules: list[str],
    *,
    cli_command: str = "odoo",
    db_host: str | None = None,
    db_user: str | None = None,
    db_password_env: str | None = None,
    config_path: str | None = None,
) -> None:
    if not modules:
        return
    env = _resolve_password_env(db_password_env)
    run(
        build_update_modules_args(
            db_name,
            modules,
            cli_command=cli_command,
            db_host=db_host,
            db_user=db_user,
            config_path=config_path,
        ),
        stream=True,
        env=env,
    )


def update_modules_runtime(
    runtime: RuntimeAdapter,
    service: str,
    db_name: str,
    modules: list[str],
    *,
    cli_command: str = "odoo",
    db_host: str | None = None,
    db_user: str | None = None,
    db_password_env: str | None = None,
    config_path: str | None = None,
) -> None:
    if not modules:
        return
    extra_env = _resolve_password_env(db_password_env)
    runtime.exec(
        service,
        build_update_modules_args(
            db_name,
            modules,
            cli_command=cli_command,
            db_host=db_host,
            db_user=db_user,
            config_path=config_path,
        ),
        stream=True,
        extra_env=extra_env,
    )


# Compatibility alias for integrations importing the pre-R6 helper.
update_modules_compose = update_modules_runtime
