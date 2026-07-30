from __future__ import annotations

from pathlib import Path

from odooctl.commands import validate as validate_cmd


class DummyConsole:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, *objects, **kwargs) -> None:
        self.lines.append(" ".join(str(obj) for obj in objects))


def test_validate_reports_missing_environment_variables(monkeypatch, tmp_path: Path):
    config = tmp_path / "odooctl.yml"
    config.write_text(
        """project:\n  name: demo\n  odoo_version: \"19.0\"\nruntime:\n  compose_file: docker-compose.yml\nenvironments:\n  production:\n    branch: main\n    domain: odoo.example.com\n    db_name: odoo_prod\n    filestore_path: /var/lib/odoo/filestore/odoo_prod\npostgres:\n  password_env: ODOO_DB_PASSWORD\nodoo:\n  image: registry/odoo:latest\nbackups:\n  remote:\n    bucket: demo-backups\n    endpoint_env: S3_ENDPOINT\n    access_key_env: S3_ACCESS_KEY\n    secret_key_env: S3_SECRET_KEY\n"""
    )
    dummy_console = DummyConsole()
    monkeypatch.setattr(validate_cmd, "success", lambda message: dummy_console.print(message))
    monkeypatch.setattr(validate_cmd, "warn", lambda message: dummy_console.print(message))
    monkeypatch.delenv("ODOO_DB_PASSWORD", raising=False)
    monkeypatch.delenv("S3_ENDPOINT", raising=False)
    monkeypatch.delenv("S3_ACCESS_KEY", raising=False)
    monkeypatch.delenv("S3_SECRET_KEY", raising=False)

    validate_cmd.execute(str(config))

    joined = "\n".join(dummy_console.lines)
    assert "Config valid: demo (production)" in joined
    assert "Missing referenced environment variables: ODOO_DB_PASSWORD, S3_ACCESS_KEY, S3_ENDPOINT, S3_SECRET_KEY" in joined
