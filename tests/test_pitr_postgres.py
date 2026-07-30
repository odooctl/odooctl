from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from odooctl.adapters.pitr_postgres import (
    PitrPostgresAdapter,
    PostgresPitrInspection,
    PostgresTablespace,
)
from odooctl.config import PostgresConfig
from odooctl.utils.shell import CommandResult


def _inspection(*, tablespaces=()):
    return PostgresPitrInspection(
        server_version_num=160004,
        server_major=16,
        system_identifier="7421924587153508191",
        timeline=7,
        wal_segment_size_bytes=16 * 1024 * 1024,
        wal_level="replica",
        archive_mode="on",
        archive_command="archive-wal %p %f",
        archive_library="",
        tablespaces=tuple(tablespaces),
    )


def _adapter(monkeypatch):
    monkeypatch.setenv("PITR_DATABASE_PASSWORD", "pitr-secret-password")
    return PitrPostgresAdapter(
        PostgresConfig(
            host="postgres.internal",
            port=5433,
            user="backup_user",
            password_env="PITR_DATABASE_PASSWORD",
        )
    )


def test_inspect_captures_cluster_identity_archive_and_tablespaces(
    tmp_path,
    monkeypatch,
):
    from odooctl.adapters import pitr_postgres

    calls = []
    payload = {
        "server_version_num": 160004,
        "system_identifier": "7421924587153508191",
        "timeline_id": 7,
        "wal_segment_size_bytes": 16777216,
        "wal_level": "replica",
        "archive_mode": "on",
        "archive_command": "archive-wal %p %f",
        "archive_library": "",
        "tablespaces": [
            {
                "oid": "16384",
                "name": "attachments",
                "location": str(tmp_path / "source-tablespace"),
            }
        ],
    }

    def fake_run(args, **kwargs):
        calls.append((list(args), kwargs))
        return CommandResult(list(args), 0, json.dumps(payload), "")

    monkeypatch.setattr(pitr_postgres, "run", fake_run)
    inspected = _adapter(monkeypatch).inspect()

    assert inspected.server_major == 16
    assert inspected.system_identifier == payload["system_identifier"]
    assert inspected.timeline == 7
    assert inspected.wal_segment_size_bytes == 16777216
    assert inspected.archiving_configured is True
    assert inspected.tablespaces == (
        PostgresTablespace(
            oid=16384,
            name="attachments",
            location=tmp_path / "source-tablespace",
        ),
    )
    argv, kwargs = calls[0]
    assert argv[0] == "psql"
    assert "pg_control_system()" in argv[-1]
    assert "pg_control_checkpoint()" in argv[-1]
    assert "wal_segment_size" in argv[-1]
    assert "archive_command" in argv[-1]
    assert "pg_tablespace_location" in argv[-1]
    assert "pitr-secret-password" not in " ".join(argv)
    assert kwargs["env"] == {"PGPASSWORD": "pitr-secret-password"}


def test_create_base_backup_maps_tablespaces_then_verifies(
    tmp_path,
    monkeypatch,
):
    from odooctl.adapters import pitr_postgres

    source_tablespace = tmp_path / "source=space"
    source_tablespace.mkdir()
    inspection = _inspection(
        tablespaces=(
            PostgresTablespace(
                oid=16384,
                name="attachments",
                location=source_tablespace,
            ),
        )
    )
    destination = tmp_path / "base"
    calls = []

    def fake_run(args, **kwargs):
        argv = list(args)
        calls.append((argv, kwargs))
        if argv[0] == "pg_basebackup":
            pgdata = Path(argv[argv.index("--pgdata") + 1])
            pgdata.mkdir()
            (pgdata / "PG_VERSION").write_text("16\n")
            (pgdata / "backup_manifest").write_text(
                json.dumps(
                    {
                        "PostgreSQL-Backup-Manifest-Version": 1,
                        "WAL-Ranges": [
                            {
                                "Timeline": 7,
                                "Start-LSN": "0/01000000",
                                "End-LSN": "0/02000000",
                            }
                        ],
                    }
                )
            )
            mapping = argv[argv.index("--tablespace-mapping") + 1]
            mapped_path = mapping.rsplit("=", 1)[1]
            Path(mapped_path).mkdir()
        if argv[0] == "pg_controldata":
            return CommandResult(
                argv,
                0,
                "\n".join(
                    [
                        "Database system identifier: 7421924587153508191",
                        "Latest checkpoint's TimeLineID: 7",
                        "Bytes per WAL segment: 16777216",
                    ]
                ),
                "",
            )
        return CommandResult(argv, 0, "", "")

    monkeypatch.setattr(pitr_postgres, "run", fake_run)
    backup = _adapter(monkeypatch).create_base_backup(
        destination,
        inspection=inspection,
        label="nightly-pitr",
    )

    assert [call[0][0] for call in calls] == [
        "pg_basebackup",
        "pg_verifybackup",
        "pg_controldata",
    ]
    base_args = calls[0][0]
    assert base_args[base_args.index("--format") + 1] == "plain"
    assert base_args[base_args.index("--wal-method") + 1] == "stream"
    assert base_args[base_args.index("--manifest-checksums") + 1] == "SHA256"
    assert base_args[base_args.index("--label") + 1] == "nightly-pitr"
    mapping = base_args[base_args.index("--tablespace-mapping") + 1]
    assert "\\=" in mapping
    assert "pitr-secret-password" not in " ".join(base_args)
    assert calls[0][1]["env"] == {
        "PGPASSWORD": "pitr-secret-password"
    }
    assert calls[1][0] == [
        "pg_verifybackup",
        "--exit-on-error",
        str(destination),
    ]
    assert calls[2][0] == ["pg_controldata", str(destination)]
    assert backup.verified is True
    assert backup.manifest_sha256 == hashlib.sha256(
        (destination / "backup_manifest").read_bytes()
    ).hexdigest()
    assert backup.tablespaces[0].backup_path.is_dir()


def test_failed_base_backup_verification_removes_only_new_outputs(
    tmp_path,
    monkeypatch,
):
    from odooctl.adapters import pitr_postgres

    source_tablespace = tmp_path / "source-tablespace"
    source_tablespace.mkdir()
    inspection = _inspection(
        tablespaces=(
            PostgresTablespace(
                oid=16384,
                name="attachments",
                location=source_tablespace,
            ),
        )
    )
    destination = tmp_path / "failed-base"

    def fake_run(args, **kwargs):
        argv = list(args)
        if argv[0] == "pg_basebackup":
            pgdata = Path(argv[argv.index("--pgdata") + 1])
            pgdata.mkdir()
            (pgdata / "PG_VERSION").write_text("16")
            (pgdata / "backup_manifest").write_text("bad manifest")
            mapping = argv[argv.index("--tablespace-mapping") + 1]
            Path(mapping.split("=", 1)[1]).mkdir()
            return CommandResult(argv, 0, "", "")
        raise RuntimeError("pg_verifybackup rejected the backup")

    monkeypatch.setattr(pitr_postgres, "run", fake_run)
    with pytest.raises(RuntimeError, match="pg_verifybackup"):
        _adapter(monkeypatch).create_base_backup(
            destination,
            inspection=inspection,
        )

    assert not destination.exists()
    assert not (tmp_path / "failed-base.tablespaces").exists()
    assert source_tablespace.is_dir()


def test_base_backup_never_overwrites_existing_destination(
    tmp_path,
    monkeypatch,
):
    from odooctl.adapters import pitr_postgres

    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "keep").write_text("user data")
    monkeypatch.setattr(
        pitr_postgres,
        "run",
        lambda *args, **kwargs: pytest.fail("command must not run"),
    )

    with pytest.raises(FileExistsError):
        _adapter(monkeypatch).create_base_backup(
            destination,
            inspection=_inspection(),
        )
    assert (destination / "keep").read_text() == "user data"
