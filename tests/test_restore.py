import json
from pathlib import Path

import pytest

from odooctl.commands.restore import (
    execute,
    resolve_backup_dir,
    run_restore,
    sha256_file,
    validate_backup_dir,
)


def write_manifest(path: Path, *, project: str = "p", environment: str = "staging") -> None:
    backup = path.parent
    path.write_text(
        json.dumps(
            {
                "project": project,
                "environment": environment,
                "backup_id": backup.name,
                "schema_version": 1,
                "backup_mode": "full",
                "db_name": "odoo_staging",
                "filestore_path": "/var/lib/odoo/filestore/odoo_staging",
                "odoo_version": "19.0",
                "checksums": {
                    "db_dump": sha256_file(backup / "db.dump"),
                    "filestore": sha256_file(backup / "filestore.tar"),
                },
            }
        )
    )


def make_backup(root: Path, name: str, *, project: str = "p") -> Path:
    backup = root / name
    backup.mkdir(parents=True)
    (backup / "db.dump").write_bytes(b"db")
    (backup / "filestore.tar").write_bytes(b"fs")
    write_manifest(backup / "manifest.json", project=project)
    return backup


def test_latest_restore_uses_requested_environment(tmp_path: Path):
    make_backup(tmp_path, "production_2026-01-01_000000")
    staging = make_backup(tmp_path, "staging_2026-01-02_000000")
    assert resolve_backup_dir("staging", "latest", tmp_path) == staging


def test_restore_preflight_requires_backup_directory(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        validate_backup_dir(tmp_path / "missing", expected_project="p")


def test_restore_preflight_requires_db_dump_before_destructive_restore(tmp_path: Path):
    backup = make_backup(tmp_path, "staging_1")
    (backup / "db.dump").unlink()
    with pytest.raises(FileNotFoundError, match="db.dump"):
        validate_backup_dir(backup, expected_project="p")


def test_restore_preflight_requires_filestore_before_destructive_restore(tmp_path: Path):
    backup = make_backup(tmp_path, "staging_1")
    (backup / "filestore.tar").unlink()
    with pytest.raises(FileNotFoundError, match="filestore.tar"):
        validate_backup_dir(backup, expected_project="p")


def test_restore_preflight_rejects_wrong_project(tmp_path: Path):
    backup = make_backup(tmp_path, "staging_1", project="other")
    with pytest.raises(RuntimeError, match="Backup project mismatch"):
        validate_backup_dir(backup, expected_project="p")


def test_restore_preflight_rejects_wrong_environment(tmp_path: Path):
    backup = make_backup(tmp_path, "staging_1", project="p")
    with pytest.raises(RuntimeError, match="Backup environment mismatch"):
        validate_backup_dir(backup, expected_project="p", expected_environment="production")


def test_restore_preflight_rejects_unsupported_mode(tmp_path: Path):
    backup = make_backup(tmp_path, "staging_1", project="p")
    manifest = json.loads((backup / "manifest.json").read_text())
    manifest["backup_mode"] = "db-only"
    (backup / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="Unsupported backup mode"):
        validate_backup_dir(backup, expected_project="p", expected_environment="staging", restore_mode="full")


def test_restore_preflight_rejects_missing_checksums(tmp_path: Path):
    backup = make_backup(tmp_path, "staging_1")
    manifest = json.loads((backup / "manifest.json").read_text())
    manifest.pop("checksums")
    (backup / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="missing checksum"):
        validate_backup_dir(backup, expected_project="p")


def test_restore_preflight_rejects_checksum_mismatch(tmp_path: Path):
    backup = make_backup(tmp_path, "staging_1")
    manifest = json.loads((backup / "manifest.json").read_text())
    manifest["checksums"] = {"db_dump": "bad", "filestore": "bad"}
    (backup / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        validate_backup_dir(backup, expected_project="p")


@pytest.mark.parametrize(
    "hostile",
    [
        "../../etc",
        "..",
        "../staging_2026-01-02_000000",
        "staging/../../../etc",
        "a/b",
        "/etc/passwd",
        "..\\..\\etc",
        ".",
        "",
    ],
)
def test_resolve_backup_dir_rejects_path_traversal(tmp_path: Path, hostile: str):
    """F10: client-suppliable backup ids must not escape the backups root."""
    with pytest.raises(ValueError, match="backup id"):
        resolve_backup_dir("staging", hostile, tmp_path / "backups")


def test_resolve_backup_dir_accepts_plain_backup_name(tmp_path: Path):
    backup = make_backup(tmp_path, "staging_2026-01-02_000000")
    resolved = resolve_backup_dir("staging", backup.name, tmp_path)
    assert resolved == backup.resolve()
    assert resolved.name == "staging_2026-01-02_000000"


def test_resolve_backup_dir_rejects_symlink_escape(tmp_path: Path):
    """F10 defense-in-depth: a backup dir symlinked outside the root is rejected."""
    root = tmp_path / "backups"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "staging_evil").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes the backups root"):
        resolve_backup_dir("staging", "staging_evil", root)


def _write_service_config(tmp_path: Path) -> Path:
    config = tmp_path / "odooctl.yml"
    config.write_text(
        """project:\n  name: demo\n  odoo_version: \"19.0\"\nruntime:\n  compose_file: docker-compose.yml\nbackups:\n  local_path: backups\npostgres:\n  host: localhost\n  port: 5432\n  user: odoo\n  password_env: ODOO_DB_PASSWORD\nenvironments:\n  staging:\n    branch: staging\n    domain: staging.example.com\n    db_name: odoo_staging\n    filestore_path: /var/lib/odoo/filestore/odoo_staging\nodoo:\n  image: registry/odoo:latest\n"""
    )
    return config


def test_run_restore_rejects_backup_id_escape_via_service_layer(tmp_path: Path):
    """F10: a hostile backup id through the service layer never touches the DB."""
    from odooctl.services.context import ServiceContext

    config = _write_service_config(tmp_path)
    ctx = ServiceContext.from_config_path(str(config))
    with pytest.raises(ValueError, match="backup id"):
        run_restore(ctx, "staging", "../../etc")


def test_restore_to_env_rejects_backup_id_escape_via_service_layer(tmp_path: Path):
    """F10: cross-env restore rejects hostile backup ids before any DB work."""
    from odooctl.services.context import ServiceContext
    from odooctl.services.restore import restore_to_env

    config = _write_service_config(tmp_path)
    ctx = ServiceContext.from_config_path(str(config))
    with pytest.raises(ValueError, match="backup id"):
        restore_to_env(
            source_environment="staging",
            target_environment="staging",
            backup="../../../etc",
            ctx=ctx,
        )


def test_restore_reports_backup_name_after_successful_restore(tmp_path: Path, monkeypatch):
    config = tmp_path / "odooctl.yml"
    config.write_text(
        """project:\n  name: demo\n  odoo_version: \"19.0\"\nruntime:\n  compose_file: docker-compose.yml\nbackups:\n  local_path: backups\nhealthcheck:\n  path: /web/health\n  timeout_seconds: 10\n  retries: 3\n  interval_seconds: 1\npostgres:\n  host: localhost\n  port: 5432\n  user: odoo\n  password_env: ODOO_DB_PASSWORD\nenvironments:\n  staging:\n    branch: staging\n    domain: staging.example.com\n    db_name: odoo_staging\n    filestore_path: /var/lib/odoo/filestore/odoo_staging\nodoo:\n  image: registry/odoo:latest\n"""
    )
    backup = make_backup(tmp_path / "backups", "staging_2026-01-02_000000", project="demo")

    events: list[tuple[str, tuple[object, ...]]] = []

    class DummyPostgres:
        def __init__(self, config):
            events.append(("postgres_init", (config.host, config.port, config.user)))

        def restore(self, db_name, dump_path):
            events.append(("restore", (db_name, Path(dump_path).name)))

        def psql(self, db_name, sql):
            events.append(("psql", (db_name, sql.split()[0])))

    class DummyFilestore:
        def restore_archive(self, archive_path, target_path):
            events.append(("filestore_restore", (Path(archive_path).name, target_path)))

    monkeypatch.setattr("odooctl.services.restore.PostgresAdapter", DummyPostgres)
    monkeypatch.setattr("odooctl.services.restore.FilestoreAdapter", DummyFilestore)
    monkeypatch.setattr("odooctl.services.restore.check_url", lambda *args, **kwargs: events.append(("healthcheck", args)))
    monkeypatch.chdir(tmp_path)

    assert execute("staging", backup.name, str(config)) == backup.name
    assert events[0] == ("postgres_init", ("localhost", 5432, "odoo"))
    # Verify-before-destroy: restore lands in a temp DB, then swap replaces the live DB
    assert events[1] == ("restore", ("odoo_staging_incoming", "db.dump"))
    assert events[2] == ("filestore_restore", ("filestore.tar", "/var/lib/odoo/filestore/odoo_staging"))
    swap_sql = [e for e in events if e[0] == "psql"]
    assert [s[1][1] for s in swap_sql] == ["SELECT", "DROP", "SELECT", "ALTER"]
    assert events[-1][0] == "healthcheck"
