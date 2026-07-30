from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

import pytest

from odooctl.context import ProjectContext
from odooctl.services.dr import DrDrillTarget
from odooctl.utils.shell import CommandResult


def _context(tmp_path: Path) -> ProjectContext:
    config = tmp_path / "odooctl.yml"
    config.write_text(
        """\
project:
  name: demo
  odoo_version: "19.0"
runtime:
  compose_file: docker-compose.yml
  execution_mode: docker
postgres:
  service: live-db
  host: live-postgres.example
odoo:
  image: odoo:19.0
  service: live-odoo
  cli_command: /usr/bin/odoo
environments:
  production:
    branch: main
    domain: odoo.example.com
    db_name: odoo_prod
    filestore_path: /var/lib/odoo/filestore/odoo_prod
    filestore_volume: live-odoo-data
"""
    )
    return ProjectContext.from_config_path(config)


def _target() -> DrDrillTarget:
    return DrDrillTarget(
        environment="production",
        backup_id="production_2026-07-30_020000",
        database="odoo_prod_dr_drill",
        filestore_path="/var/lib/odoo/filestore/odoo_prod_dr_drill",
    )


def _safe_tar(tmp_path: Path) -> Path:
    source = tmp_path / "odoo_prod"
    source.mkdir()
    (source / "attachment").write_text("data")
    archive = tmp_path / "filestore.tar"
    with tarfile.open(archive, "w") as tf:
        tf.add(source, arcname="odoo_prod")
    return archive


def test_drill_runtime_uses_internal_postgres_and_dedicated_filestore(
    tmp_path,
    monkeypatch,
):
    from odooctl.adapters import dr_runtime

    run_calls = []
    pipe_calls = []
    piped_payloads = []

    def fake_run(args, **kwargs):
        args = list(args)
        run_calls.append((args, kwargs))
        if args[:2] == ["docker", "compose"]:
            return CommandResult(args, 0, "sha256:isolated-postgres-image\n", "")
        if args[:2] == ["docker", "port"]:
            return CommandResult(args, 0, "127.0.0.1:49152\n", "")
        return CommandResult(args, 0, "ok\n", "")

    def fake_pipe(args, **kwargs):
        args = list(args)
        pipe_calls.append((args, kwargs))
        piped_payloads.append(Path(kwargs["stdin_path"]).read_bytes())
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr(dr_runtime, "run", fake_run)
    monkeypatch.setattr(dr_runtime, "run_pipe_stdin", fake_pipe)
    monkeypatch.setattr(
        dr_runtime.secrets,
        "token_urlsafe",
        lambda length: "isolated-random-password",
    )
    runtime = dr_runtime.DockerComposeDrillRuntime(_context(tmp_path))
    target = _target()

    runtime.prepare(target)
    dump = tmp_path / "db.dump"
    dump.write_bytes(b"postgres dump")
    runtime.restore_database(target, dump)
    runtime.restore_filestore(target, _safe_tar(tmp_path))
    url = runtime.start(target)

    argv_strings = [" ".join(args) for args, _ in run_calls + pipe_calls]
    assert not any("isolated-random-password" in argv for argv in argv_strings)

    network_create = next(
        args
        for args, _ in run_calls
        if args[:3] == ["docker", "network", "create"]
    )
    assert "--internal" in network_create
    internal_network = network_create[-1]

    postgres_run = next(
        (args, kwargs)
        for args, kwargs in run_calls
        if args[:2] == ["docker", "run"]
        and "--network-alias" in args
    )
    postgres_args, postgres_kwargs = postgres_run
    assert postgres_args[postgres_args.index("--network") + 1] == internal_network
    assert "--publish" not in postgres_args
    assert postgres_args[postgres_args.index("--network-alias") + 1] == (
        dr_runtime.DATABASE_ALIAS
    )
    assert postgres_args[-1] == "sha256:isolated-postgres-image"
    assert postgres_kwargs["env"]["POSTGRES_PASSWORD"] == (
        "isolated-random-password"
    )
    assert "isolated-random-password" not in postgres_args

    config_payload = next(
        payload for payload in piped_payloads if b"[options]" in payload
    ).decode()
    config_pipe_kwargs = next(
        kwargs
        for (_args, kwargs), payload in zip(pipe_calls, piped_payloads, strict=True)
        if b"[options]" in payload
    )
    assert f"db_host = {dr_runtime.DATABASE_ALIAS}" in config_payload
    assert "db_host = live-postgres.example" not in config_payload
    assert "db_password = isolated-random-password" in config_payload
    assert "env" not in config_pipe_kwargs

    createdb_args = next(
        args for args, _ in run_calls if "createdb" in args
    )
    pg_restore_args = next(
        args for args, _ in pipe_calls if "pg_restore" in args
    )
    isolated_postgres = createdb_args[createdb_args.index("--env") + 2]
    assert isolated_postgres.endswith("-postgres")
    assert isolated_postgres != "live-db"
    assert isolated_postgres in pg_restore_args
    assert "live-db" not in pg_restore_args
    assert "live-postgres.example" not in pg_restore_args
    assert "--exit-on-error" in pg_restore_args

    odoo_run = next(
        args
        for args, _ in run_calls
        if args[:2] == ["docker", "run"]
        and "--publish" in args
    )
    assert "compose" not in odoo_run
    assert odoo_run[odoo_run.index("--network") + 1] == internal_network
    assert odoo_run[odoo_run.index("--database") + 1] == target.database
    assert odoo_run[odoo_run.index("--db-filter") + 1] == (
        "^odoo_prod_dr_drill$"
    )
    assert "live-odoo" not in odoo_run
    assert "live-odoo-data" not in odoo_run
    assert any(
        item.endswith("-filestore:/var/lib/odoo")
        for item in odoo_run
    )
    assert any(
        item.endswith("-config:/odooctl-config:ro")
        for item in odoo_run
    )
    assert url == (
        "http://127.0.0.1:49152/web/health?db=odoo_prod_dr_drill"
    )


def test_drill_filestore_archive_root_cannot_select_live_volume_target(
    tmp_path,
    monkeypatch,
):
    from odooctl.adapters import dr_runtime

    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return CommandResult(list(args), 0, "", "")

    monkeypatch.setattr(dr_runtime, "run", fake_run)
    monkeypatch.setattr(
        dr_runtime,
        "run_pipe_stdin",
        lambda args, **kwargs: CommandResult(list(args), 0, "", ""),
    )
    runtime = dr_runtime.DockerComposeDrillRuntime(_context(tmp_path))
    runtime._prepared_identity = runtime._identity(_target())

    runtime.restore_filestore(_target(), _safe_tar(tmp_path))

    joined = [" ".join(args) for args in calls]
    assert not any("live-odoo-data" in value for value in joined)
    move = next(args for args in calls if "--entrypoint" in args and "mv" in args)
    assert move[-2:] == [
        "/target/.odooctl-restore/odoo_prod",
        "/target/filestore/odoo_prod_dr_drill",
    ]


def test_drill_runtime_rejects_mismatched_filestore_before_docker(
    tmp_path,
    monkeypatch,
):
    from odooctl.adapters import dr_runtime

    called = []
    monkeypatch.setattr(dr_runtime, "run", lambda *args, **kwargs: called.append(args))
    runtime = dr_runtime.DockerComposeDrillRuntime(_context(tmp_path))
    target = DrDrillTarget(
        environment="production",
        backup_id="backup",
        database="expected_db",
        filestore_path="/var/lib/odoo/filestore/different_db",
    )

    with pytest.raises(RuntimeError, match="database-specific path"):
        runtime.prepare(target)

    assert called == []


def test_drill_runtime_cleanup_attempts_both_containers_volumes_and_network(
    tmp_path,
    monkeypatch,
):
    from odooctl.adapters import dr_runtime

    calls = []

    def fake_run(args, **kwargs):
        args = list(args)
        calls.append(args)
        if args[:3] == ["docker", "volume", "rm"] and "filestore" in args[-1]:
            return CommandResult(args, 1, "", "volume is busy")
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr(dr_runtime, "run", fake_run)
    runtime = dr_runtime.DockerComposeDrillRuntime(_context(tmp_path))

    with pytest.raises(RuntimeError, match="filestore volume"):
        runtime.stop(_target())

    assert sum(args[:3] == ["docker", "rm", "-f"] for args in calls) == 2
    assert sum(args[:3] == ["docker", "volume", "rm"] for args in calls) == 2
    assert sum(args[:3] == ["docker", "network", "rm"] for args in calls) == 1


def test_drill_service_cleans_resources_and_host_secret_after_partial_prepare(
    tmp_path,
    monkeypatch,
):
    from odooctl.adapters import dr_runtime
    from odooctl.services.dr import run_dr_drill

    backup_id = "production_2026-07-30_020000"
    backup_dir = tmp_path / "backups" / backup_id
    backup_dir.mkdir(parents=True)
    db_data = b"postgres dump"
    filestore_data = b"filestore"
    (backup_dir / "db.dump").write_bytes(db_data)
    (backup_dir / "filestore.tar").write_bytes(filestore_data)
    (backup_dir / "manifest.json").write_text(
        json.dumps(
            {
                "backup_id": backup_id,
                "project": "demo",
                "environment": "production",
                "db_name": "odoo_prod",
                "odoo_version": "19.0",
                "checksums": {
                    "db_dump": hashlib.sha256(db_data).hexdigest(),
                    "filestore": hashlib.sha256(filestore_data).hexdigest(),
                },
            }
        )
    )

    calls = []
    failed = False

    def fake_run(args, **kwargs):
        nonlocal failed
        args = list(args)
        calls.append(args)
        if args[:2] == ["docker", "compose"]:
            return CommandResult(args, 0, "sha256:postgres\n", "")
        if (
            not failed
            and args[:3] == ["docker", "volume", "create"]
            and args[-1].endswith("-config")
        ):
            failed = True
            raise RuntimeError("config volume create failed")
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr(dr_runtime, "run", fake_run)
    runtime = dr_runtime.DockerComposeDrillRuntime(_context(tmp_path))

    result = run_dr_drill(
        environment="production",
        expected_project="demo",
        backups_root=tmp_path / "backups",
        healthcheck_fn=lambda url: True,
        prepare_runtime_fn=runtime.prepare,
        restore_database_fn=runtime.restore_database,
        restore_filestore_fn=runtime.restore_filestore,
        start_runtime_fn=runtime.start,
        stop_runtime_fn=runtime.stop,
    )

    assert result.status == "failed"
    assert "config volume create failed" in (result.message or "")
    assert sum(args[:3] == ["docker", "rm", "-f"] for args in calls) == 2
    assert sum(args[:3] == ["docker", "volume", "rm"] for args in calls) == 2
    assert sum(args[:3] == ["docker", "network", "rm"] for args in calls) == 1
    assert not list((tmp_path / ".odooctl").glob(".dr-drill-*.conf"))
    assert runtime._password is None
    assert runtime._prepared_identity is None


def test_drill_runtime_cleanup_continues_after_command_exception_and_clears_secret(
    tmp_path,
    monkeypatch,
):
    from odooctl.adapters import dr_runtime

    calls = []
    runtime = dr_runtime.DockerComposeDrillRuntime(_context(tmp_path))
    target = _target()
    resources = runtime._resources(target)
    secret_config = tmp_path / ".dr-secret.conf"
    secret_config.write_text("db_password = still-secret")
    runtime._config_path = secret_config
    runtime._password = "still-secret"
    runtime._prepared_identity = runtime._identity(target)

    def fake_run(args, **kwargs):
        args = list(args)
        calls.append(args)
        if args == ["docker", "rm", "-f", resources.odoo_container]:
            raise OSError("docker client disappeared")
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr(dr_runtime, "run", fake_run)

    with pytest.raises(RuntimeError, match="docker client disappeared"):
        runtime.stop(target)

    assert sum(args[:3] == ["docker", "rm", "-f"] for args in calls) == 2
    assert sum(args[:3] == ["docker", "volume", "rm"] for args in calls) == 2
    assert sum(args[:3] == ["docker", "network", "rm"] for args in calls) == 1
    assert not secret_config.exists()
    assert runtime._config_path is None
    assert runtime._password is None
    assert runtime._prepared_identity is None


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("127.0.0.1:49152\n", 49152),
        ("0.0.0.0:32768\n[::]:32768\n", 32768),
    ],
)
def test_published_port_parsing(output, expected):
    from odooctl.adapters.dr_runtime import _published_port

    assert _published_port(output) == expected
