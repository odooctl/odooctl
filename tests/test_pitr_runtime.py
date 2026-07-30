from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from odooctl.adapters.pitr_postgres import (
    PhysicalBaseBackup,
    PostgresPitrInspection,
    PostgresTablespace,
    TablespaceBaseBackup,
)
from odooctl.adapters.pitr_runtime import (
    DockerPitrRuntime,
    PitrRecoverySpec,
    PitrRecoveryStatus,
    PitrRuntimeError,
)
from odooctl.utils.shell import CommandResult


RECORDED_IMAGE = (
    "registry.example.invalid/postgres@sha256:"
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
)


def _base_backup(
    tmp_path: Path,
    *,
    with_tablespace: bool = True,
) -> PhysicalBaseBackup:
    pgdata = tmp_path / "base-backup"
    pgdata.mkdir()
    (pgdata / "PG_VERSION").write_text("16\n")
    (pgdata / "backup_manifest").write_text("verified manifest\n")

    inspection_tablespaces: tuple[PostgresTablespace, ...] = ()
    backup_tablespaces: tuple[TablespaceBaseBackup, ...] = ()
    tablespace_root: Path | None = None
    if with_tablespace:
        tablespace_root = tmp_path / "base-backup.tablespaces"
        tablespace_backup = tablespace_root / "16384"
        tablespace_backup.mkdir(parents=True)
        (tablespace_backup / "relation").write_bytes(b"tablespace data")
        source_location = Path("/srv/postgresql/live-tablespaces/attachments")
        inspection_tablespaces = (
            PostgresTablespace(
                oid=16384,
                name="attachments",
                location=source_location,
            ),
        )
        backup_tablespaces = (
            TablespaceBaseBackup(
                oid=16384,
                name="attachments",
                source_location=source_location,
                backup_path=tablespace_backup,
            ),
        )

    inspection = PostgresPitrInspection(
        server_version_num=160004,
        server_major=16,
        system_identifier="7421924587153508191",
        timeline=7,
        wal_segment_size_bytes=16 * 1024 * 1024,
        wal_level="replica",
        archive_mode="on",
        archive_command="archive-wal %p %f",
        archive_library="",
        tablespaces=inspection_tablespaces,
    )
    return PhysicalBaseBackup(
        pgdata=pgdata,
        tablespace_root=tablespace_root,
        tablespaces=backup_tablespaces,
        inspection=inspection,
        manifest_sha256="b" * 64,
        verified=True,
    )


def _spec(
    tmp_path: Path,
    *,
    with_tablespace: bool = True,
) -> PitrRecoverySpec:
    wal_archive = tmp_path / "wal-archive"
    wal_archive.mkdir()
    (wal_archive / "000000070000000000000001").write_bytes(b"wal")
    return PitrRecoverySpec(
        base_backup=_base_backup(
            tmp_path,
            with_tablespace=with_tablespace,
        ),
        wal_archive_dir=wal_archive,
        postgres_image=RECORDED_IMAGE,
        target_time=datetime(
            2026,
            7,
            30,
            15,
            30,
            tzinfo=timezone(timedelta(hours=3)),
        ),
        target_timeline=7,
        database="odoo_prod",
        timeout_seconds=2,
        poll_interval_seconds=1,
    )


def _install_command_fakes(
    monkeypatch,
    *,
    status: str = "t\tt\t7\t0/70000\t2026-07-30 12:29:59+00\n",
    dump_error: BaseException | None = None,
):
    from odooctl.adapters import pitr_runtime

    events: list[tuple[str, list[str]]] = []
    config_payloads: list[str] = []

    def fake_run(args, **kwargs):
        argv = list(args)
        events.append(("run", argv))
        if (
            "--entrypoint" in argv
            and argv[argv.index("--entrypoint") + 1] == "postgres"
            and argv[-1] == "--version"
        ):
            return CommandResult(
                argv,
                0,
                "postgres (PostgreSQL) 16.4\n",
                "",
            )
        if "pg_isready" in argv:
            return CommandResult(argv, 0, "accepting connections\n", "")
        if "psql" in argv:
            return CommandResult(argv, 0, status, "")
        return CommandResult(argv, 0, "", "")

    def fake_pipe(args, **kwargs):
        argv = list(args)
        events.append(("pipe", argv))
        config_payloads.append(
            Path(kwargs["stdin_path"]).read_text()
        )
        return CommandResult(argv, 0, "", "")

    def fake_capture(args, **kwargs):
        argv = list(args)
        events.append(("capture", argv))
        Path(kwargs["stdout_path"]).write_bytes(b"partial dump")
        if dump_error is not None:
            raise dump_error
        return CommandResult(argv, 0, str(kwargs["stdout_path"]), "")

    monkeypatch.setattr(pitr_runtime, "run", fake_run)
    monkeypatch.setattr(pitr_runtime, "run_pipe_stdin", fake_pipe)
    monkeypatch.setattr(pitr_runtime, "run_capture_bytes", fake_capture)
    return events, config_payloads


def _first_event(
    events: list[tuple[str, list[str]]],
    predicate,
) -> int:
    return next(
        index
        for index, (_kind, argv) in enumerate(events)
        if predicate(argv)
    )


def test_recovery_uses_exact_image_isolated_volumes_and_pauses_before_dump(
    tmp_path,
    monkeypatch,
):
    spec = _spec(tmp_path)
    events, config_payloads = _install_command_fakes(monkeypatch)
    runtime = DockerPitrRuntime(tmp_path / "runtime", sleep=lambda _: None)
    cleanup_calls = 0
    original_cleanup = runtime._cleanup

    def counted_cleanup(resources):
        nonlocal cleanup_calls
        cleanup_calls += 1
        return original_cleanup(resources)

    monkeypatch.setattr(runtime, "_cleanup", counted_cleanup)
    output = tmp_path / "restored.dump"

    result = runtime.recover_and_dump(spec, output)

    assert result.dump_path == output
    assert output.read_bytes() == b"partial dump"
    assert result.database == "odoo_prod"
    assert result.target_time == datetime(
        2026,
        7,
        30,
        12,
        30,
        tzinfo=timezone.utc,
    )
    assert result.status.in_recovery is True
    assert result.status.replay_paused is True
    assert cleanup_calls == 1

    docker_runs = [
        argv
        for _kind, argv in events
        if argv[:2] == ["docker", "run"]
    ]
    assert docker_runs
    for argv in docker_runs:
        assert argv.count(RECORDED_IMAGE) == 1
        assert argv[argv.index("--network") + 1] == "none"
        assert "--publish" not in argv
        assert "-p" not in argv

    server_run = next(argv for argv in docker_runs if "--detach" in argv)
    server_mounts = [
        server_run[index + 1]
        for index, value in enumerate(server_run)
        if value == "--mount"
    ]
    pgdata_mount = next(
        mount
        for mount in server_mounts
        if mount.endswith("dst=/var/lib/postgresql/data")
    )
    assert pgdata_mount.startswith(
        "type=volume,src=odooctl-pitr-"
    )
    assert str(spec.base_backup.pgdata) not in " ".join(server_run)
    assert str(spec.base_backup.tablespaces[0].backup_path) not in (
        " ".join(server_run)
    )
    assert "/srv/postgresql/live-tablespaces" not in " ".join(server_run)
    assert any(
        mount.endswith("dst=/odooctl-wal,readonly")
        for mount in server_mounts
    )
    assert any(
        mount.endswith("dst=/odooctl-tablespaces/16384")
        and mount.startswith("type=volume,src=odooctl-pitr-")
        for mount in server_mounts
    )

    assert len(config_payloads) == 1
    config = config_payloads[0]
    assert (
        "recovery_target_time = "
        "'2026-07-30T12:30:00.000000+00:00'"
    ) in config
    assert "recovery_target_timeline = '7'" in config
    assert "recovery_target_action = 'pause'" in config
    assert "primary_conninfo = ''" in config
    assert any(
        "--entrypoint" in argv
        and argv[argv.index("--entrypoint") + 1] == "touch"
        and argv[-1] == "/var/lib/postgresql/data/recovery.signal"
        for _kind, argv in events
    )
    assert not any(
        "pg_ctl" in argv or "promote" in argv
        for _kind, argv in events
    )

    volume_create = _first_event(
        events,
        lambda argv: argv[:3] == ["docker", "volume", "create"],
    )
    base_copy = _first_event(
        events,
        lambda argv: "--entrypoint" in argv
        and argv[argv.index("--entrypoint") + 1] == "cp",
    )
    recovery_signal = _first_event(
        events,
        lambda argv: argv[-1:] == [
            "/var/lib/postgresql/data/recovery.signal"
        ],
    )
    server_start = events.index(("run", server_run))
    status_check = _first_event(events, lambda argv: "psql" in argv)
    dump = _first_event(events, lambda argv: "pg_dump" in argv)
    cleanup = _first_event(
        events,
        lambda argv: argv[:3] == ["docker", "rm", "--force"],
    )
    assert (
        volume_create
        < base_copy
        < recovery_signal
        < server_start
        < status_check
        < dump
        < cleanup
    )
    dump_args = events[dump][1]
    assert dump_args[dump_args.index("--dbname") + 1] == "odoo_prod"


def test_promoted_scratch_cluster_is_rejected_and_cleaned_once(
    tmp_path,
    monkeypatch,
):
    spec = _spec(tmp_path, with_tablespace=False)
    events, _payloads = _install_command_fakes(
        monkeypatch,
        status="f\tf\t7\t0/70000\t2026-07-30 12:29:59+00\n",
    )
    runtime = DockerPitrRuntime(tmp_path / "runtime", sleep=lambda _: None)
    cleanup_calls = 0
    original_cleanup = runtime._cleanup

    def counted_cleanup(resources):
        nonlocal cleanup_calls
        cleanup_calls += 1
        return original_cleanup(resources)

    monkeypatch.setattr(runtime, "_cleanup", counted_cleanup)
    output = tmp_path / "must-not-exist.dump"

    with pytest.raises(PitrRuntimeError, match="left recovery"):
        runtime.recover_and_dump(spec, output)

    assert cleanup_calls == 1
    assert not output.exists()
    assert not any(kind == "capture" for kind, _argv in events)
    assert sum(
        argv[:3] == ["docker", "rm", "--force"]
        for _kind, argv in events
    ) == 1


def test_dump_failure_removes_partial_file_and_cleans_once(
    tmp_path,
    monkeypatch,
):
    spec = _spec(tmp_path, with_tablespace=False)
    _events, _payloads = _install_command_fakes(
        monkeypatch,
        dump_error=RuntimeError("pg_dump failed"),
    )
    runtime = DockerPitrRuntime(tmp_path / "runtime", sleep=lambda _: None)
    cleanup_calls = 0
    original_cleanup = runtime._cleanup

    def counted_cleanup(resources):
        nonlocal cleanup_calls
        cleanup_calls += 1
        return original_cleanup(resources)

    monkeypatch.setattr(runtime, "_cleanup", counted_cleanup)
    output = tmp_path / "failed.dump"

    with pytest.raises(RuntimeError, match="pg_dump failed"):
        runtime.recover_and_dump(spec, output)

    assert cleanup_calls == 1
    assert not output.exists()
    assert not list(tmp_path.glob(".failed.dump.*.partial"))


@pytest.mark.parametrize(
    "failing_stage",
    ["_prepare", "_start", "_wait_for_target", "_dump"],
)
def test_every_runtime_stage_failure_cleans_exactly_once(
    tmp_path,
    monkeypatch,
    failing_stage,
):
    spec = _spec(tmp_path, with_tablespace=False)
    runtime = DockerPitrRuntime(tmp_path / "runtime")
    cleanup_calls = 0

    def stage(name, result=None):
        def callback(*args, **kwargs):
            if name == failing_stage:
                raise RuntimeError(f"{name} failed")
            return result

        return callback

    status = PitrRecoveryStatus(
        in_recovery=True,
        replay_paused=True,
        timeline=7,
        replay_lsn="0/70000",
        last_replay_timestamp=datetime(
            2026,
            7,
            30,
            12,
            29,
            59,
            tzinfo=timezone.utc,
        ),
    )
    monkeypatch.setattr(runtime, "_prepare", stage("_prepare"))
    monkeypatch.setattr(runtime, "_start", stage("_start"))
    monkeypatch.setattr(
        runtime,
        "_wait_for_target",
        stage("_wait_for_target", status),
    )
    monkeypatch.setattr(runtime, "_dump", stage("_dump"))

    def counted_cleanup(resources):
        nonlocal cleanup_calls
        cleanup_calls += 1
        return ()

    monkeypatch.setattr(runtime, "_cleanup", counted_cleanup)

    with pytest.raises(RuntimeError, match=f"{failing_stage} failed"):
        runtime.recover_and_dump(
            spec,
            tmp_path / f"{failing_stage}.dump",
        )

    assert cleanup_calls == 1


def test_recovery_rejects_naive_target_and_symlink_mounts_before_docker(
    tmp_path,
    monkeypatch,
):
    from odooctl.adapters import pitr_runtime

    spec = _spec(tmp_path, with_tablespace=False)
    runtime = DockerPitrRuntime(tmp_path / "runtime")
    monkeypatch.setattr(
        pitr_runtime,
        "run",
        lambda *args, **kwargs: pytest.fail("Docker must not run"),
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        runtime.recover_and_dump(
            replace(
                spec,
                target_time=datetime(2026, 7, 30, 12, 30),
            ),
            tmp_path / "naive.dump",
        )

    wal_link = tmp_path / "wal-link"
    wal_link.symlink_to(spec.wal_archive_dir, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        runtime.recover_and_dump(
            replace(spec, wal_archive_dir=wal_link),
            tmp_path / "symlink.dump",
        )
