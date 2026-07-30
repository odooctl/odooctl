from __future__ import annotations
from pathlib import Path
from odooctl.config import PostgresConfig
from odooctl.utils.shell import run

class PostgresAdapter:
    def __init__(self, config: PostgresConfig):
        self.config = config

    def env(self) -> dict[str, str]:
        return {"PGPASSWORD": self.config.password()}

    def base_args(self) -> list[str]:
        return ["-h", self.config.host, "-p", str(self.config.port), "-U", self.config.user]

    def ping(self, db_name: str) -> None:
        run(["psql", *self.base_args(), "-d", db_name, "-v", "ON_ERROR_STOP=1", "-c", "SELECT 1"], env=self.env())

    def database_exists(self, db_name: str) -> bool:
        from odooctl.odoo.db_swap import quote_literal

        result = run(
            ["psql", *self.base_args(), "-d", "postgres", "-tAc",
             f"SELECT 1 FROM pg_database WHERE datname = {quote_literal(db_name)}"],
            env=self.env(),
        )
        return result.stdout.strip() == "1"

    def dump(self, db_name: str, output: str | Path) -> None:
        run(["pg_dump", *self.base_args(), "-Fc", "-d", db_name, "-f", str(output)], env=self.env(), stream=True)

    def restore(self, db_name: str, dump_path: str | Path) -> None:
        self.drop_create(db_name)
        self.restore_into(db_name, dump_path)

    def create(self, db_name: str) -> None:
        run(["createdb", *self.base_args(), db_name], env=self.env(), stream=True)

    def restore_into(self, db_name: str, dump_path: str | Path) -> None:
        run(["pg_restore", *self.base_args(), "-d", db_name, str(dump_path)], env=self.env(), stream=True)

    def drop_create(self, db_name: str) -> None:
        run(["dropdb", *self.base_args(), db_name, "--if-exists"], env=self.env(), stream=True)
        self.create(db_name)

    def psql_file(self, db_name: str, sql_file: str | Path) -> None:
        run(["psql", *self.base_args(), "-d", db_name, "-v", "ON_ERROR_STOP=1", "-f", str(sql_file)], env=self.env(), stream=True)

    def psql(self, db_name: str, sql: str) -> None:
        run(["psql", *self.base_args(), "-d", db_name, "-v", "ON_ERROR_STOP=1", "-c", sql], env=self.env(), stream=True)

    def psql_scalar(self, db_name: str, sql: str) -> str | None:
        result = run(
            [
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
            ],
            env=self.env(),
        )
        value = result.stdout.strip()
        return value or None
