from __future__ import annotations

from pathlib import Path
from typing import Protocol

from odooctl.adapters.postgres import PostgresAdapter
from odooctl.context import ProjectContext
from odooctl.utils.shell import run


class DbAdapter(Protocol):
    def ping(self, db_name: str) -> None: ...
    def database_exists(self, db_name: str) -> bool: ...
    def dump(self, db_name: str, output: str | Path) -> None: ...
    def restore(self, db_name: str, dump_path: str | Path) -> None: ...
    def create(self, db_name: str) -> None: ...
    def restore_into(self, db_name: str, dump_path: str | Path) -> None: ...
    def drop(self, db_name: str) -> None: ...
    def drop_create(self, db_name: str) -> None: ...
    def psql_file(self, db_name: str, sql_file: str | Path) -> None: ...
    def psql(self, db_name: str, sql: str) -> None: ...
    def psql_scalar(self, db_name: str, sql: str) -> str | None: ...


class HostPostgresAdapter(PostgresAdapter):
    """Host PostgreSQL adapter kept for backward-compatible host execution."""

    def drop(self, db_name: str) -> None:
        from odooctl.odoo.db_swap import drop_database, terminate_connections

        terminate_connections(self, db_name)
        drop_database(self, db_name)


class DockerPostgresAdapter:

    """PostgreSQL adapter that executes client tools inside the compose DB service."""

    def __init__(self, ctx: ProjectContext):
        self.ctx = ctx
        self.config = ctx.config.postgres

    @property
    def project_dir(self) -> str:
        return str(self.ctx.root)

    def _cmd(self, *args: str) -> list[str]:
        return [
            "docker",
            "compose",
            "-f",
            str(self.ctx.compose_file),
            "exec",
            "-T",
            "-e",
            "PGPASSWORD",
            self.config.service,
            *args,
        ]

    def _password_env(self) -> dict[str, str]:
        return {"PGPASSWORD": self.config.service_password()}

    def base_args(self) -> list[str]:
        return ["-h", self.config.internal_host, "-U", self.config.service_user]

    def ping(self, db_name: str) -> None:
        run(self._cmd("pg_isready", "-d", db_name, *self.base_args()), cwd=self.project_dir, env=self._password_env())

    def database_exists(self, db_name: str) -> bool:
        from odooctl.odoo.db_swap import quote_literal

        result = run(
            self._cmd("psql", *self.base_args(), "-d", "postgres", "-tAc",
                      f"SELECT 1 FROM pg_database WHERE datname = {quote_literal(db_name)}"),
            cwd=self.project_dir,
            env=self._password_env(),
        )
        return result.stdout.strip() == "1"

    def dump(self, db_name: str, output: str | Path) -> None:
        from odooctl.utils.shell import run_capture_bytes

        run_capture_bytes(
            self._cmd("pg_dump", *self.base_args(), "-Fc", "-d", db_name),
            cwd=self.project_dir,
            env=self._password_env(),
            stdout_path=output,
        )

    def restore(self, db_name: str, dump_path: str | Path) -> None:
        self.drop_create(db_name)
        self.restore_into(db_name, dump_path)

    def create(self, db_name: str) -> None:
        run(
            self._cmd("createdb", *self.base_args(), db_name),
            cwd=self.project_dir,
            env=self._password_env(),
            stream=True,
        )

    def restore_into(self, db_name: str, dump_path: str | Path) -> None:
        from odooctl.utils.shell import run_pipe_stdin

        run_pipe_stdin(
            self._cmd("pg_restore", *self.base_args(), "-d", db_name),
            cwd=self.project_dir,
            env=self._password_env(),
            stdin_path=dump_path,
        )

    def drop(self, db_name: str) -> None:
        from odooctl.odoo.db_swap import drop_database, terminate_connections

        terminate_connections(self, db_name)
        drop_database(self, db_name)

    def drop_create(self, db_name: str) -> None:
        escaped = db_name.replace("'", "''")
        terminate_sql = (
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{escaped}' AND pid <> pg_backend_pid();"
        )
        run(
            self._cmd("psql", *self.base_args(), "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c", terminate_sql),
            cwd=self.project_dir,
            env=self._password_env(),
            stream=True,
        )
        run(self._cmd("dropdb", *self.base_args(), db_name, "--if-exists"), cwd=self.project_dir, env=self._password_env(), stream=True)
        self.create(db_name)

    def psql_file(self, db_name: str, sql_file: str | Path) -> None:
        from odooctl.utils.shell import run_pipe_stdin

        run_pipe_stdin(
            self._cmd("psql", *self.base_args(), "-d", db_name, "-v", "ON_ERROR_STOP=1", "-f", "-"),
            cwd=self.project_dir,
            env=self._password_env(),
            stdin_path=sql_file,
        )

    def psql(self, db_name: str, sql: str) -> None:
        run(self._cmd("psql", *self.base_args(), "-d", db_name, "-v", "ON_ERROR_STOP=1", "-c", sql), cwd=self.project_dir, env=self._password_env(), stream=True)

    def psql_scalar(self, db_name: str, sql: str) -> str | None:
        result = run(
            self._cmd(
                "psql",
                *self.base_args(),
                "-d",
                db_name,
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                sql,
            ),
            cwd=self.project_dir,
            env=self._password_env(),
        )
        value = result.stdout.strip()
        return value or None

    def clone_db_in_container(self, src: str, dst: str) -> None:
        from odooctl.utils.shell import run_pipe

        self.drop_create(dst)
        run_pipe(
            self._cmd("pg_dump", *self.base_args(), "-Fc", "-d", src),
            self._cmd("pg_restore", *self.base_args(), "-d", dst),
            cwd=self.project_dir,
            env=self._password_env(),
        )


def make_db_adapter(ctx: ProjectContext) -> DbAdapter:
    if ctx.config.runtime.execution_mode == "host":
        return HostPostgresAdapter(ctx.config.postgres)
    return DockerPostgresAdapter(ctx)
