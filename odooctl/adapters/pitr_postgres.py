"""Low-level PostgreSQL inspection and physical base-backup primitives for PITR."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from odooctl.config import PostgresConfig
from odooctl.utils.shell import run


INSPECTION_SQL = r"""
SELECT json_build_object(
    'server_version_num', current_setting('server_version_num')::integer,
    'system_identifier', (pg_control_system()).system_identifier::text,
    'timeline_id', (pg_control_checkpoint()).timeline_id,
    'wal_segment_size_bytes',
        pg_size_bytes(current_setting('wal_segment_size')),
    'wal_level', current_setting('wal_level'),
    'archive_mode', current_setting('archive_mode'),
    'archive_command', current_setting('archive_command'),
    'archive_library', current_setting('archive_library', true),
    'tablespaces', COALESCE(
        (
            SELECT json_agg(
                json_build_object(
                    'oid', oid::text,
                    'name', spcname,
                    'location', pg_tablespace_location(oid)
                )
                ORDER BY spcname
            )
            FROM pg_tablespace
            WHERE spcname NOT IN ('pg_default', 'pg_global')
              AND pg_tablespace_location(oid) <> ''
        ),
        '[]'::json
    )
)::text;
""".strip()


class PitrPostgresError(RuntimeError):
    """PostgreSQL source inspection or physical-backup failure."""


@dataclass(frozen=True)
class PostgresTablespace:
    oid: int
    name: str
    location: Path


@dataclass(frozen=True)
class PostgresPitrInspection:
    server_version_num: int
    server_major: int
    system_identifier: str
    timeline: int
    wal_segment_size_bytes: int
    wal_level: str
    archive_mode: str
    archive_command: str
    archive_library: str
    tablespaces: tuple[PostgresTablespace, ...]

    @property
    def archiving_configured(self) -> bool:
        archive_sink = bool(
            self.archive_command.strip()
            and self.archive_command.strip() != "(disabled)"
        ) or bool(self.archive_library.strip())
        return self.archive_mode in {"on", "always"} and archive_sink


@dataclass(frozen=True)
class TablespaceBaseBackup:
    oid: int
    name: str
    source_location: Path
    backup_path: Path


@dataclass(frozen=True)
class PhysicalBaseBackup:
    pgdata: Path
    tablespace_root: Path | None
    tablespaces: tuple[TablespaceBaseBackup, ...]
    inspection: PostgresPitrInspection
    manifest_sha256: str
    verified: bool = True


class PhysicalBackupSource(Protocol):
    def inspect(self) -> PostgresPitrInspection: ...

    def create_base_backup(
        self,
        destination: str | Path,
        *,
        inspection: PostgresPitrInspection | None = None,
        label: str = "odooctl-pitr",
    ) -> PhysicalBaseBackup: ...


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_new_path(value: str | Path, label: str) -> Path:
    path = Path(value)
    if "\x00" in str(path):
        raise ValueError(f"{label} contains a NUL byte")
    if os.path.lexists(path):
        raise FileExistsError(f"{label} already exists: {path}")
    parent = path.parent.resolve(strict=True)
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError(f"{label} parent is not a real directory: {parent}")
    resolved = parent / path.name
    if not path.name or resolved == parent:
        raise ValueError(f"{label} must name a new directory")
    return resolved


def _remove_new_tree(path: Path) -> None:
    if path.is_symlink():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _tablespace_mapping_value(source: Path, destination: Path) -> str:
    """Encode pg_basebackup's olddir=newdir mapping grammar."""

    def escaped(path: Path) -> str:
        return str(path).replace("\\", "\\\\").replace("=", "\\=")

    return f"{escaped(source)}={escaped(destination)}"


class PitrPostgresAdapter:
    """Inspect a source cluster and create a verified, portable plain backup."""

    def __init__(
        self,
        config: PostgresConfig,
        *,
        database: str = "postgres",
        psql_executable: str = "psql",
        pg_basebackup_executable: str = "pg_basebackup",
        pg_verifybackup_executable: str = "pg_verifybackup",
        pg_controldata_executable: str = "pg_controldata",
    ):
        self.config = config
        self.database = database
        self.psql_executable = psql_executable
        self.pg_basebackup_executable = pg_basebackup_executable
        self.pg_verifybackup_executable = pg_verifybackup_executable
        self.pg_controldata_executable = pg_controldata_executable

    def _env(self) -> dict[str, str]:
        return {"PGPASSWORD": self.config.password()}

    def _connection_args(self) -> list[str]:
        return [
            "--host",
            self.config.host,
            "--port",
            str(self.config.port),
            "--username",
            self.config.user,
            "--no-password",
        ]

    def inspect(self) -> PostgresPitrInspection:
        result = run(
            [
                self.psql_executable,
                *self._connection_args(),
                "--dbname",
                self.database,
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                "--set",
                "ON_ERROR_STOP=1",
                "--command",
                INSPECTION_SQL,
            ],
            env=self._env(),
        )
        payload = result.stdout.strip()
        try:
            raw = json.loads(payload)
            if not isinstance(raw, dict):
                raise TypeError("inspection payload is not an object")
            version_num = int(raw["server_version_num"])
            system_identifier = str(raw["system_identifier"])
            timeline = int(raw["timeline_id"])
            wal_size = int(raw["wal_segment_size_bytes"])
            raw_tablespaces = raw["tablespaces"]
            if not isinstance(raw_tablespaces, list):
                raise TypeError("tablespaces is not a list")
            tablespaces = tuple(
                PostgresTablespace(
                    oid=int(item["oid"]),
                    name=str(item["name"]),
                    location=Path(str(item["location"])),
                )
                for item in raw_tablespaces
                if isinstance(item, dict)
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PitrPostgresError(
                "PostgreSQL PITR inspection returned invalid metadata"
            ) from exc

        if (
            version_num < 100000
            or not system_identifier.isdigit()
            or timeline < 1
            or wal_size <= 0
            or len(tablespaces) != len(raw_tablespaces)
            or any(
                item.oid <= 0
                or not item.name
                or not item.location.is_absolute()
                for item in tablespaces
            )
        ):
            raise PitrPostgresError(
                "PostgreSQL PITR inspection returned unsafe metadata"
            )
        return PostgresPitrInspection(
            server_version_num=version_num,
            server_major=version_num // 10000,
            system_identifier=system_identifier,
            timeline=timeline,
            wal_segment_size_bytes=wal_size,
            wal_level=str(raw["wal_level"]),
            archive_mode=str(raw["archive_mode"]),
            archive_command=str(raw.get("archive_command") or ""),
            archive_library=str(raw.get("archive_library") or ""),
            tablespaces=tablespaces,
        )

    def create_base_backup(
        self,
        destination: str | Path,
        *,
        inspection: PostgresPitrInspection | None = None,
        label: str = "odooctl-pitr",
    ) -> PhysicalBaseBackup:
        if not label or "\x00" in label or "\n" in label or "\r" in label:
            raise ValueError("base-backup label contains unsupported characters")
        source = inspection or self.inspect()
        if source.server_major < 13:
            raise PitrPostgresError(
                "pg_verifybackup requires PostgreSQL 13 or newer"
            )

        pgdata = _safe_new_path(destination, "base-backup destination")
        tablespace_root = (
            _safe_new_path(
                pgdata.with_name(f"{pgdata.name}.tablespaces"),
                "tablespace backup root",
            )
            if source.tablespaces
            else None
        )
        mappings: list[TablespaceBaseBackup] = []
        args = [
            self.pg_basebackup_executable,
            *self._connection_args(),
            "--pgdata",
            str(pgdata),
            "--format",
            "plain",
            "--wal-method",
            "stream",
            "--checkpoint",
            "fast",
            "--manifest-checksums",
            "SHA256",
            "--label",
            label,
        ]
        if tablespace_root is not None:
            tablespace_root.mkdir(mode=0o700)
            for tablespace in source.tablespaces:
                target = tablespace_root / str(tablespace.oid)
                mappings.append(
                    TablespaceBaseBackup(
                        oid=tablespace.oid,
                        name=tablespace.name,
                        source_location=tablespace.location,
                        backup_path=target,
                    )
                )
                args.extend(
                    [
                        "--tablespace-mapping",
                        _tablespace_mapping_value(
                            tablespace.location,
                            target,
                        ),
                    ]
                )

        try:
            run(args, env=self._env())
            manifest = pgdata / "backup_manifest"
            pg_version = pgdata / "PG_VERSION"
            if (
                not pgdata.is_dir()
                or pgdata.is_symlink()
                or not manifest.is_file()
                or manifest.is_symlink()
                or not pg_version.is_file()
                or pg_version.is_symlink()
            ):
                raise PitrPostgresError(
                    "pg_basebackup did not create a complete physical backup"
                )
            run(
                [
                    self.pg_verifybackup_executable,
                    "--exit-on-error",
                    str(pgdata),
                ]
            )
            version = pg_version.read_text().strip()
            if version != str(source.server_major):
                raise PitrPostgresError(
                    "physical backup PG_VERSION does not match the "
                    "inspected PostgreSQL major"
                )
            try:
                manifest_payload = json.loads(manifest.read_text())
            except (OSError, UnicodeError, json.JSONDecodeError):
                raise PitrPostgresError(
                    "verified PostgreSQL backup manifest is not valid JSON"
                ) from None
            if not isinstance(manifest_payload, dict):
                raise PitrPostgresError(
                    "verified PostgreSQL backup manifest is not an object"
                )
            manifest_system_id = manifest_payload.get(
                "System-Identifier"
            )
            if source.server_major >= 17 and manifest_system_id is None:
                raise PitrPostgresError(
                    "PostgreSQL 17+ backup manifest omitted System-Identifier"
                )
            if (
                manifest_system_id is not None
                and str(manifest_system_id) != source.system_identifier
            ):
                raise PitrPostgresError(
                    "physical backup manifest system identifier does not "
                    "match the inspected source"
                )

            control = run(
                [self.pg_controldata_executable, str(pgdata)]
            ).stdout

            def control_value(label: str) -> int:
                match = re.search(
                    rf"^{re.escape(label)}:\s*([0-9]+)\s*$",
                    control,
                    flags=re.MULTILINE,
                )
                if match is None:
                    raise PitrPostgresError(
                        "pg_controldata omitted required physical-backup "
                        f"identity: {label}"
                    )
                return int(match.group(1))

            if (
                str(control_value("Database system identifier"))
                != source.system_identifier
                or control_value("Latest checkpoint's TimeLineID")
                != source.timeline
                or control_value("Bytes per WAL segment")
                != source.wal_segment_size_bytes
            ):
                raise PitrPostgresError(
                    "physical backup control identity does not match the "
                    "inspected PostgreSQL source"
                )
            return PhysicalBaseBackup(
                pgdata=pgdata,
                tablespace_root=tablespace_root,
                tablespaces=tuple(mappings),
                inspection=source,
                manifest_sha256=_sha256_file(manifest),
            )
        except BaseException:
            _remove_new_tree(pgdata)
            if tablespace_root is not None:
                _remove_new_tree(tablespace_root)
            raise
