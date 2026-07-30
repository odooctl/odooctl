from __future__ import annotations

from pathlib import Path

import pytest

from odooctl.config import OdooCtlConfig
from odooctl.odoo.neutralize import (
    build_neutralize_args,
    build_neutralize_probe_args,
    neutralize_database,
)
from odooctl.utils.shell import CommandResult


def _config(policy: str = "preferred") -> OdooCtlConfig:
    return OdooCtlConfig.model_validate(
        {
            "project": {"name": "demo", "odoo_version": "19.0"},
            "runtime": {"compose_file": "docker-compose.yml"},
            "postgres": {"password_env": "ODOO_DB_PASSWORD"},
            "odoo": {
                "image": "odoo:19.0",
                "service": "odoo",
                "config_path": "/etc/odoo/odoo.conf",
                "addons_paths": ["/mnt/extra-addons", "/opt/odoo/addons"],
            },
            "sanitization": {"native_neutralize": policy},
            "environments": {
                "staging": {
                    "branch": "staging",
                    "domain": "staging.example.com",
                    "db_name": "odoo_staging",
                    "filestore_path": "/var/lib/odoo/filestore/odoo_staging",
                    "sanitize": True,
                }
            },
        }
    )


class FakePostgres:
    def __init__(self) -> None:
        self.sql: list[tuple[str, str]] = []
        self.files: list[tuple[str, Path]] = []

    def psql(self, db_name: str, sql: str) -> None:
        self.sql.append((db_name, sql))

    def psql_file(self, db_name: str, sql_file: str | Path) -> None:
        self.files.append((db_name, Path(sql_file)))


class FakeCompose:
    def __init__(
        self,
        *,
        probe_returncode: int = 0,
        probe_stdout: str = "BEGIN;\nCOMMIT;\n",
        probe_stderr: str = "",
    ) -> None:
        self.probe_returncode = probe_returncode
        self.probe_stdout = probe_stdout
        self.probe_stderr = probe_stderr
        self.calls: list[tuple[str, list[str], dict]] = []

    def exec(self, service: str, args: list[str], **kwargs) -> CommandResult:
        self.calls.append((service, list(args), kwargs))
        is_probe = "--help" in args
        return CommandResult(
            list(args),
            self.probe_returncode if is_probe else 0,
            self.probe_stdout if is_probe else "",
            self.probe_stderr if is_probe else "",
        )


def test_build_neutralize_args_uses_native_cli_and_addons_without_password():
    cfg = _config()

    args = build_neutralize_args(cfg, "odoo_staging_incoming", stdout=True)

    assert args == [
        "odoo",
        "--addons-path=/mnt/extra-addons,/opt/odoo/addons",
        "neutralize",
        "-d",
        "odoo_staging_incoming",
        "--stdout",
        "-c",
        "/etc/odoo/odoo.conf",
        "--db_host",
        "postgres",
        "--db_user",
        "odoo",
    ]
    assert "--db_password" not in args


def test_probe_uses_dispatcher_compatible_side_effect_free_help_command():
    cfg = _config()

    assert build_neutralize_probe_args(cfg) == [
        "odoo",
        "--addons-path=/mnt/extra-addons,/opt/odoo/addons",
        "neutralize",
        "--help",
    ]


def test_preferred_native_neutralization_runs_probe_command_extensions_and_checks(
    monkeypatch,
):
    cfg = _config()
    monkeypatch.setenv("ODOO_DB_PASSWORD", "native-secret-value")
    pg = FakePostgres()
    compose = FakeCompose()

    result = neutralize_database(
        pg=pg,
        db_name="odoo_staging_incoming",
        env=cfg.env("staging"),
        cfg=cfg,
        compose=compose,
        sql_files=[],
    )

    assert result.native_status == "executed"
    assert len(compose.calls) == 2
    assert "--help" in compose.calls[0][1]
    assert "--help" not in compose.calls[1][1]
    assert compose.calls[0][2]["check"] is False
    assert "extra_env" not in compose.calls[0][2]
    assert compose.calls[1][2]["extra_env"] == {"PGPASSWORD": "native-secret-value"}
    assert all("native-secret-value" not in arg for _, args, _ in compose.calls for arg in args)
    assert result.extension_statements > 0
    assert result.verification_checks
    assert any("neutralization - disable emails" in sql for _, sql in pg.sql)
    assert any("database.is_neutralized" in sql for _, sql in pg.sql)


def test_preferred_policy_falls_back_only_for_explicitly_unsupported_command(
    monkeypatch,
):
    cfg = _config("preferred")
    monkeypatch.setenv("ODOO_DB_PASSWORD", "native-secret-value")
    compose = FakeCompose(
        probe_returncode=2,
        probe_stderr="Unknown command 'neutralize'",
    )

    result = neutralize_database(
        pg=FakePostgres(),
        db_name="odoo_staging_incoming",
        env=cfg.env("staging"),
        cfg=cfg,
        compose=compose,
        sql_files=[],
    )

    assert result.native_status == "unsupported"
    assert len(compose.calls) == 1


def test_required_policy_rejects_unsupported_native_command(monkeypatch):
    cfg = _config("required")
    monkeypatch.setenv("ODOO_DB_PASSWORD", "native-secret-value")
    compose = FakeCompose(
        probe_returncode=2,
        probe_stderr="Unknown command 'neutralize'",
    )

    with pytest.raises(RuntimeError, match="is required"):
        neutralize_database(
            pg=FakePostgres(),
            db_name="odoo_staging_incoming",
            env=cfg.env("staging"),
            cfg=cfg,
            compose=compose,
            sql_files=[],
        )


def test_probe_connectivity_failure_is_fatal_even_when_policy_is_preferred(
    monkeypatch,
):
    cfg = _config("preferred")
    monkeypatch.setenv("ODOO_DB_PASSWORD", "native-secret-value")
    compose = FakeCompose(
        probe_returncode=1,
        probe_stderr="could not connect to server",
    )

    with pytest.raises(RuntimeError, match="capability probe failed"):
        neutralize_database(
            pg=FakePostgres(),
            db_name="odoo_staging_incoming",
            env=cfg.env("staging"),
            cfg=cfg,
            compose=compose,
            sql_files=[],
        )


def test_unrelated_unknown_command_error_is_not_misclassified_as_unsupported(
    monkeypatch,
):
    cfg = _config("preferred")
    monkeypatch.setenv("ODOO_DB_PASSWORD", "native-secret-value")
    compose = FakeCompose(
        probe_returncode=1,
        probe_stderr="docker compose: unknown command 'exec'",
    )

    with pytest.raises(RuntimeError, match="capability probe failed"):
        neutralize_database(
            pg=FakePostgres(),
            db_name="odoo_staging_incoming",
            env=cfg.env("staging"),
            cfg=cfg,
            compose=compose,
            sql_files=[],
        )


def test_disabled_policy_skips_native_runtime_but_keeps_extensions(monkeypatch):
    cfg = _config("disabled")
    monkeypatch.delenv("ODOO_DB_PASSWORD", raising=False)
    pg = FakePostgres()
    compose = FakeCompose()

    result = neutralize_database(
        pg=pg,
        db_name="odoo_staging_incoming",
        env=cfg.env("staging"),
        cfg=cfg,
        compose=compose,
        sql_files=[],
    )

    assert result.native_status == "disabled"
    assert compose.calls == []
    assert pg.sql
    assert any(
        "ir_act_server" in sql and "neutralization - disable webhook" in sql
        for _, sql in pg.sql
    )


def test_invalid_native_policy_is_rejected_by_configuration():
    with pytest.raises(ValueError, match="native_neutralize"):
        _config("sometimes")


def test_empty_temp_database_suffix_is_rejected_before_any_restore():
    cfg = _config().model_dump()
    cfg["sanitization"]["temp_db_suffix"] = ""

    with pytest.raises(ValueError, match="temp_db_suffix"):
        OdooCtlConfig.model_validate(cfg)


def test_blank_native_cli_command_is_rejected():
    cfg = _config().model_dump()
    cfg["odoo"]["cli_command"] = " "

    with pytest.raises(ValueError, match="cli_command"):
        OdooCtlConfig.model_validate(cfg)
