"""Disposable Docker runtime for PostgreSQL point-in-time recovery drills."""

from __future__ import annotations

import hashlib
import math
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Callable, Protocol

from odooctl.adapters.pitr_postgres import PhysicalBaseBackup
from odooctl.config import validate_identifier
from odooctl.utils.shell import run, run_capture_bytes, run_pipe_stdin


PGDATA_MOUNT = "/var/lib/postgresql/data"
WAL_ARCHIVE_MOUNT = "/odooctl-wal"
TABLESPACE_MOUNT_ROOT = "/odooctl-tablespaces"


class PitrRuntimeError(RuntimeError):
    """Disposable recovery failed or could not be cleaned up."""


@dataclass(frozen=True)
class PitrRecoverySpec:
    base_backup: PhysicalBaseBackup
    wal_archive_dir: Path
    postgres_image: str
    target_time: datetime
    target_timeline: int | str
    database: str
    postgres_os_user: str = "postgres"
    postgres_db_user: str = "postgres"
    timeout_seconds: float = 120
    poll_interval_seconds: float = 1


@dataclass(frozen=True)
class PitrRecoveryStatus:
    in_recovery: bool
    replay_paused: bool
    timeline: int
    replay_lsn: str | None
    last_replay_timestamp: datetime | None


@dataclass(frozen=True)
class PitrRecoveryResult:
    dump_path: Path
    database: str
    target_time: datetime
    target_timeline: str
    status: PitrRecoveryStatus
    postgres_image: str


class PitrRecoveryRuntime(Protocol):
    def recover_and_dump(
        self,
        spec: PitrRecoverySpec,
        output: str | Path,
    ) -> PitrRecoveryResult: ...


@dataclass(frozen=True)
class _RuntimeResources:
    container: str
    pgdata_volume: str
    tablespace_volumes: tuple[tuple[int, str], ...]
    label: str

    @property
    def volumes(self) -> tuple[str, ...]:
        return (
            self.pgdata_volume,
            *(volume for _, volume in self.tablespace_volumes),
        )


def _real_directory(path: Path, label: str) -> Path:
    if "\x00" in str(path) or "," in str(path):
        raise ValueError(f"{label} contains unsupported mount characters")
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symbolic link: {path}")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir() or resolved.is_symlink():
        raise ValueError(f"{label} is not a real directory: {path}")
    return resolved


def _new_output_path(path: str | Path) -> Path:
    output = Path(path)
    if "\x00" in str(output) or not output.name:
        raise ValueError("PITR dump output path is invalid")
    if os.path.lexists(output):
        raise FileExistsError(f"PITR dump output already exists: {output}")
    parent = output.parent.resolve(strict=True)
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("PITR dump output parent is not a real directory")
    return parent / output.name


def _safe_image(image: str) -> str:
    if (
        not image
        or image.startswith("-")
        or image.strip() != image
        or any(character.isspace() or ord(character) < 32 for character in image)
    ):
        raise ValueError("recorded PostgreSQL image reference is invalid")
    return image


def _target_values(spec: PitrRecoverySpec) -> tuple[datetime, str]:
    target = spec.target_time
    if target.tzinfo is None or target.utcoffset() is None:
        raise ValueError("PITR target_time must be timezone-aware")
    target_utc = target.astimezone(timezone.utc)
    timeline = str(spec.target_timeline)
    if timeline != "latest":
        try:
            parsed_timeline = int(timeline)
        except ValueError:
            raise ValueError(
                "PITR target_timeline must be a positive integer or 'latest'"
            ) from None
        if parsed_timeline < 1 or str(parsed_timeline) != timeline:
            raise ValueError(
                "PITR target_timeline must be a positive integer or 'latest'"
            )
    return target_utc, timeline


def _bind_mount(path: Path, destination: str, *, readonly: bool) -> str:
    suffix = ",readonly" if readonly else ""
    return f"type=bind,src={path},dst={destination}{suffix}"


def _volume_mount(volume: str, destination: str) -> str:
    return f"type=volume,src={volume},dst={destination}"


class DockerPitrRuntime:
    """Recover into disposable volumes without networking or live mounts."""

    def __init__(
        self,
        work_dir: str | Path,
        *,
        docker_executable: str = "docker",
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.work_dir = Path(work_dir)
        self.docker_executable = docker_executable
        self._sleep = sleep

    def _validate(
        self,
        spec: PitrRecoverySpec,
        output: str | Path,
    ) -> tuple[Path, Path, Path, datetime, str]:
        if not self.work_dir.exists():
            self.work_dir.mkdir(parents=True, mode=0o700)
        work_dir = _real_directory(self.work_dir, "PITR work directory")
        pgdata = _real_directory(
            Path(spec.base_backup.pgdata),
            "physical base backup",
        )
        manifest = pgdata / "backup_manifest"
        if (
            not manifest.is_file()
            or manifest.is_symlink()
            or not spec.base_backup.verified
        ):
            raise PitrRuntimeError("physical base backup is not verified")
        for tablespace in spec.base_backup.tablespaces:
            _real_directory(
                tablespace.backup_path,
                f"tablespace backup {tablespace.oid}",
            )
        wal_archive = _real_directory(
            Path(spec.wal_archive_dir),
            "WAL archive",
        )
        _safe_image(spec.postgres_image)
        validate_identifier(spec.database, "PITR database")
        validate_identifier(spec.postgres_os_user, "PostgreSQL OS user")
        validate_identifier(spec.postgres_db_user, "PostgreSQL DB user")
        if spec.timeout_seconds <= 0 or spec.poll_interval_seconds <= 0:
            raise ValueError("PITR timeout and poll interval must be positive")
        target, timeline = _target_values(spec)
        return (
            work_dir,
            pgdata,
            wal_archive,
            target,
            timeline,
        )

    def _resources(self, spec: PitrRecoverySpec) -> _RuntimeResources:
        identity = hashlib.sha256(
            (
                f"{spec.base_backup.inspection.system_identifier}:"
                f"{spec.target_time.isoformat()}:{uuid.uuid4().hex}"
            ).encode()
        ).hexdigest()[:16]
        prefix = f"odooctl-pitr-{identity}"
        return _RuntimeResources(
            container=f"{prefix}-postgres",
            pgdata_volume=f"{prefix}-pgdata",
            tablespace_volumes=tuple(
                (
                    tablespace.oid,
                    f"{prefix}-tblspc-{tablespace.oid}",
                )
                for tablespace in spec.base_backup.tablespaces
            ),
            label=f"odooctl.pitr={identity}",
        )

    def _helper_args(
        self,
        spec: PitrRecoverySpec,
        resources: _RuntimeResources,
        *,
        entrypoint: str,
        args: list[str],
        mounts: list[str],
        interactive: bool = False,
    ) -> list[str]:
        command = [
            self.docker_executable,
            "run",
            "--rm",
        ]
        if interactive:
            command.append("--interactive")
        command.extend(
            [
                "--network",
                "none",
                "--user",
                "0:0",
                "--security-opt",
                "no-new-privileges",
            ]
        )
        for mount in mounts:
            command.extend(["--mount", mount])
        command.extend(
            [
                "--entrypoint",
                entrypoint,
                spec.postgres_image,
                *args,
            ]
        )
        return command

    def _run_helper(
        self,
        spec: PitrRecoverySpec,
        resources: _RuntimeResources,
        *,
        entrypoint: str,
        args: list[str],
        mounts: list[str],
    ) -> None:
        run(
            self._helper_args(
                spec,
                resources,
                entrypoint=entrypoint,
                args=args,
                mounts=mounts,
            ),
            cwd=str(self.work_dir),
        )

    def _prepare(
        self,
        spec: PitrRecoverySpec,
        resources: _RuntimeResources,
        *,
        pgdata: Path,
        target: datetime,
        timeline: str,
    ) -> None:
        version_result = run(
            self._helper_args(
                spec,
                resources,
                entrypoint="postgres",
                args=["--version"],
                mounts=[],
            ),
            cwd=str(self.work_dir),
        )
        match = re.search(
            r"\bPostgreSQL\)\s+([0-9]+)(?:[.\s]|$)",
            version_result.stdout,
        )
        if (
            match is None
            or int(match.group(1))
            != spec.base_backup.inspection.server_major
        ):
            raise PitrRuntimeError(
                "recorded recovery image PostgreSQL major does not match "
                "the physical base backup"
            )

        for volume in resources.volumes:
            run(
                [
                    self.docker_executable,
                    "volume",
                    "create",
                    "--label",
                    resources.label,
                    volume,
                ],
                cwd=str(self.work_dir),
            )

        self._run_helper(
            spec,
            resources,
            entrypoint="cp",
            args=["--archive", "/source/.", "/target/"],
            mounts=[
                _bind_mount(pgdata, "/source", readonly=True),
                _volume_mount(resources.pgdata_volume, "/target"),
            ],
        )
        volumes_by_oid = dict(resources.tablespace_volumes)
        for tablespace in spec.base_backup.tablespaces:
            volume = volumes_by_oid[tablespace.oid]
            self._run_helper(
                spec,
                resources,
                entrypoint="cp",
                args=["--archive", "/source/.", "/target/"],
                mounts=[
                    _bind_mount(
                        tablespace.backup_path,
                        "/source",
                        readonly=True,
                    ),
                    _volume_mount(volume, "/target"),
                ],
            )
            main_mount = [
                _volume_mount(resources.pgdata_volume, "/target"),
            ]
            self._run_helper(
                spec,
                resources,
                entrypoint="rm",
                args=[
                    "--force",
                    f"/target/pg_tblspc/{tablespace.oid}",
                ],
                mounts=main_mount,
            )
            self._run_helper(
                spec,
                resources,
                entrypoint="ln",
                args=[
                    "--symbolic",
                    f"{TABLESPACE_MOUNT_ROOT}/{tablespace.oid}",
                    f"/target/pg_tblspc/{tablespace.oid}",
                ],
                mounts=main_mount,
            )

        recovery_config = "\n".join(
            [
                "# Generated by odooctl for an isolated PITR drill.",
                (
                    "restore_command = "
                    f"'cp {WAL_ARCHIVE_MOUNT}/%f %p'"
                ),
                (
                    "recovery_target_time = "
                    f"'{target.isoformat(timespec='microseconds')}'"
                ),
                f"recovery_target_timeline = '{timeline}'",
                "recovery_target_inclusive = 'true'",
                "recovery_target_action = 'pause'",
                "primary_conninfo = ''",
                "primary_slot_name = ''",
                "promote_trigger_file = ''",
                "archive_cleanup_command = ''",
                "hot_standby = 'on'",
                "",
            ]
        )
        fd, raw_config_path = tempfile.mkstemp(
            prefix=".pitr-recovery-",
            suffix=".conf",
            dir=self.work_dir,
            text=True,
        )
        config_path = Path(raw_config_path)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(recovery_config)
                handle.flush()
                os.fsync(handle.fileno())
            run_pipe_stdin(
                self._helper_args(
                    spec,
                    resources,
                    entrypoint="tee",
                    args=[
                        "--append",
                        f"{PGDATA_MOUNT}/postgresql.auto.conf",
                    ],
                    mounts=[
                        _volume_mount(
                            resources.pgdata_volume,
                            PGDATA_MOUNT,
                        )
                    ],
                    interactive=True,
                ),
                cwd=str(self.work_dir),
                stdin_path=config_path,
            )
        finally:
            config_path.unlink(missing_ok=True)

        pgdata_mount = [
            _volume_mount(resources.pgdata_volume, PGDATA_MOUNT),
        ]
        self._run_helper(
            spec,
            resources,
            entrypoint="rm",
            args=["--force", f"{PGDATA_MOUNT}/standby.signal"],
            mounts=pgdata_mount,
        )
        self._run_helper(
            spec,
            resources,
            entrypoint="touch",
            args=[f"{PGDATA_MOUNT}/recovery.signal"],
            mounts=pgdata_mount,
        )
        for volume in resources.volumes:
            self._run_helper(
                spec,
                resources,
                entrypoint="chown",
                args=[
                    "--recursive",
                    f"{spec.postgres_os_user}:{spec.postgres_os_user}",
                    "/target",
                ],
                mounts=[_volume_mount(volume, "/target")],
            )

    def _start(
        self,
        spec: PitrRecoverySpec,
        resources: _RuntimeResources,
        wal_archive: Path,
    ) -> None:
        command = [
            self.docker_executable,
            "run",
            "--detach",
            "--name",
            resources.container,
            "--network",
            "none",
            "--read-only",
            "--user",
            spec.postgres_os_user,
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev",
            "--tmpfs",
            "/var/run/postgresql:rw,nosuid,nodev",
            "--mount",
            _volume_mount(resources.pgdata_volume, PGDATA_MOUNT),
            "--mount",
            _bind_mount(
                wal_archive,
                WAL_ARCHIVE_MOUNT,
                readonly=True,
            ),
        ]
        for oid, volume in resources.tablespace_volumes:
            command.extend(
                [
                    "--mount",
                    _volume_mount(
                        volume,
                        f"{TABLESPACE_MOUNT_ROOT}/{oid}",
                    ),
                ]
            )
        command.extend(
            [
                "--entrypoint",
                "postgres",
                spec.postgres_image,
                "-D",
                PGDATA_MOUNT,
                "-c",
                "listen_addresses=",
                "-c",
                "archive_mode=off",
                "-c",
                "unix_socket_directories=/var/run/postgresql",
            ]
        )
        run(command, cwd=str(self.work_dir))

    def _exec_args(
        self,
        spec: PitrRecoverySpec,
        resources: _RuntimeResources,
        *args: str,
    ) -> list[str]:
        return [
            self.docker_executable,
            "exec",
            "--user",
            spec.postgres_os_user,
            resources.container,
            *args,
        ]

    @staticmethod
    def _parse_status(output: str) -> PitrRecoveryStatus:
        fields = output.strip().split("\t")
        if len(fields) != 5:
            raise PitrRuntimeError(
                "scratch PostgreSQL returned invalid recovery status"
            )
        in_recovery, paused, timeline, replay_lsn, replay_time = fields
        if in_recovery not in {"t", "f"} or paused not in {"t", "f"}:
            raise PitrRuntimeError(
                "scratch PostgreSQL returned invalid recovery flags"
            )
        try:
            parsed_timeline = int(timeline)
            parsed_replay_time = (
                datetime.fromisoformat(replay_time).astimezone(timezone.utc)
                if replay_time
                else None
            )
        except (ValueError, TypeError):
            raise PitrRuntimeError(
                "scratch PostgreSQL returned invalid recovery metadata"
            ) from None
        return PitrRecoveryStatus(
            in_recovery=in_recovery == "t",
            replay_paused=paused == "t",
            timeline=parsed_timeline,
            replay_lsn=replay_lsn or None,
            last_replay_timestamp=parsed_replay_time,
        )

    def _wait_for_target(
        self,
        spec: PitrRecoverySpec,
        resources: _RuntimeResources,
        *,
        target: datetime,
        timeline: str,
    ) -> PitrRecoveryStatus:
        attempts = max(
            1,
            math.ceil(
                spec.timeout_seconds / spec.poll_interval_seconds
            ),
        )
        status_sql = (
            "SELECT pg_is_in_recovery()::text, "
            "pg_is_wal_replay_paused()::text, "
            "(pg_control_checkpoint()).timeline_id::text, "
            "COALESCE(pg_last_wal_replay_lsn()::text, ''), "
            "COALESCE(pg_last_xact_replay_timestamp()::text, '');"
        )
        for attempt in range(attempts):
            ready = run(
                self._exec_args(
                    spec,
                    resources,
                    "pg_isready",
                    "--dbname",
                    "postgres",
                    "--username",
                    spec.postgres_db_user,
                ),
                cwd=str(self.work_dir),
                check=False,
            )
            if ready.returncode == 0:
                status_result = run(
                    self._exec_args(
                        spec,
                        resources,
                        "psql",
                        "--dbname",
                        "postgres",
                        "--username",
                        spec.postgres_db_user,
                        "--no-psqlrc",
                        "--tuples-only",
                        "--no-align",
                        "--field-separator",
                        "\t",
                        "--set",
                        "ON_ERROR_STOP=1",
                        "--command",
                        status_sql,
                    ),
                    cwd=str(self.work_dir),
                    check=False,
                )
                if status_result.returncode == 0:
                    status = self._parse_status(status_result.stdout)
                    if not status.in_recovery:
                        raise PitrRuntimeError(
                            "scratch PostgreSQL left recovery; refusing a promoted cluster"
                        )
                    if timeline != "latest" and status.timeline != int(
                        timeline
                    ):
                        raise PitrRuntimeError(
                            "scratch PostgreSQL recovered on an unexpected timeline"
                        )
                    if status.replay_paused:
                        if (
                            status.last_replay_timestamp is not None
                            and status.last_replay_timestamp > target
                        ):
                            raise PitrRuntimeError(
                                "scratch PostgreSQL replayed past the requested target"
                            )
                        return status
            if attempt + 1 < attempts:
                self._sleep(spec.poll_interval_seconds)
        raise PitrRuntimeError(
            "scratch PostgreSQL did not pause at the requested recovery target"
        )

    def _dump(
        self,
        spec: PitrRecoverySpec,
        resources: _RuntimeResources,
        output: Path,
    ) -> None:
        temporary = output.with_name(
            f".{output.name}.{uuid.uuid4().hex}.partial"
        )
        try:
            run_capture_bytes(
                self._exec_args(
                    spec,
                    resources,
                    "pg_dump",
                    "--format",
                    "custom",
                    "--no-password",
                    "--dbname",
                    spec.database,
                    "--username",
                    spec.postgres_db_user,
                ),
                cwd=str(self.work_dir),
                stdout_path=temporary,
            )
            if not temporary.is_file() or temporary.stat().st_size <= 0:
                raise PitrRuntimeError(
                    "scratch PostgreSQL produced an empty database dump"
                )
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, output)
            descriptor = os.open(output.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    def _cleanup(self, resources: _RuntimeResources) -> tuple[str, ...]:
        errors: list[str] = []
        removals = [
            (
                [
                    self.docker_executable,
                    "rm",
                    "--force",
                    resources.container,
                ],
                ("No such container", "no such container"),
                "container",
            ),
            *(
                (
                    [
                        self.docker_executable,
                        "volume",
                        "rm",
                        volume,
                    ],
                    ("No such volume", "no such volume"),
                    f"volume {volume}",
                )
                for volume in resources.volumes
            ),
        ]
        for command, absent_markers, label in removals:
            try:
                result = run(
                    command,
                    cwd=str(self.work_dir),
                    check=False,
                )
            except Exception as exc:
                errors.append(f"{label}: {exc}")
                continue
            combined = f"{result.stdout}\n{result.stderr}"
            if result.returncode != 0 and not any(
                marker in combined for marker in absent_markers
            ):
                errors.append(
                    f"{label}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
        return tuple(errors)

    def recover_and_dump(
        self,
        spec: PitrRecoverySpec,
        output: str | Path,
    ) -> PitrRecoveryResult:
        (
            _work_dir,
            pgdata,
            wal_archive,
            target,
            timeline,
        ) = self._validate(spec, output)
        dump_path = _new_output_path(output)
        resources = self._resources(spec)
        result: PitrRecoveryResult | None = None
        primary_error: BaseException | None = None
        primary_traceback: TracebackType | None = None
        try:
            self._prepare(
                spec,
                resources,
                pgdata=pgdata,
                target=target,
                timeline=timeline,
            )
            self._start(spec, resources, wal_archive)
            status = self._wait_for_target(
                spec,
                resources,
                target=target,
                timeline=timeline,
            )
            self._dump(spec, resources, dump_path)
            result = PitrRecoveryResult(
                dump_path=dump_path,
                database=spec.database,
                target_time=target,
                target_timeline=timeline,
                status=status,
                postgres_image=spec.postgres_image,
            )
        except BaseException as exc:
            primary_error = exc
            primary_traceback = exc.__traceback__

        cleanup_errors = self._cleanup(resources)
        if primary_error is not None:
            if cleanup_errors:
                primary_error.add_note(
                    "PITR cleanup also failed: "
                    + "; ".join(cleanup_errors)
                )
            raise primary_error.with_traceback(primary_traceback)
        if cleanup_errors:
            raise PitrRuntimeError(
                "PITR recovery succeeded but cleanup failed: "
                + "; ".join(cleanup_errors)
            )
        assert result is not None
        return result
