from pathlib import Path

from odooctl.adapters import filestore as filestore_module
from odooctl.adapters.filestore import DockerVolumeFilestore, FilestoreAdapter, make_filestore_adapter
from odooctl.context import ProjectContext


CONFIG = """project:
  name: demo
  odoo_version: "19.0"
runtime:
  compose_file: docker-compose.yml
  execution_mode: docker
postgres:
  service: db
  user: odoo
odoo:
  image: odoo:19.0
  service: odoo
  filestore_container_path: /var/lib/odoo
environments:
  staging:
    branch: staging
    domain: staging.example.com
    db_name: odoo_staging
    filestore_path: odoo_staging
    filestore_volume: odoo-data
"""


class DummyCompose:
    def __init__(self, compose_file: str, project_dir: str | None = None):
        self.compose_file = compose_file
        self.project_dir = project_dir
        self.calls = []

    def exec_capture_bytes(self, service, args, *, stdout_path):
        self.calls.append(("capture", service, args, Path(stdout_path)))

    def exec_pipe_stdin(self, service, args, *, stdin_path):
        self.calls.append(("stdin", service, args, Path(stdin_path)))

    def exec(self, service, args, *, stream=True):
        self.calls.append(("exec", service, args, stream))


def context(tmp_path: Path) -> ProjectContext:
    (tmp_path / "odooctl.yml").write_text(CONFIG)
    return ProjectContext.from_config_path(tmp_path / "odooctl.yml")


def test_make_filestore_adapter_selects_docker_volume_backend(tmp_path: Path, monkeypatch):
    ctx = context(tmp_path)
    created = []

    def factory(compose_file: str, project_dir: str | None = None):
        compose = DummyCompose(compose_file, project_dir)
        created.append(compose)
        return compose

    monkeypatch.setattr(filestore_module, "DockerComposeAdapter", factory)

    adapter = make_filestore_adapter(ctx, ctx.config.env("staging"))

    assert isinstance(adapter, DockerVolumeFilestore)
    assert created[0].compose_file == "docker-compose.yml"
    assert created[0].project_dir == str(tmp_path)


def test_host_filestore_uses_plain_tar_archive(tmp_path: Path, monkeypatch):
    source = tmp_path / "filestore" / "odoo_staging"
    source.mkdir(parents=True)
    archive = tmp_path / "backups" / "filestore.tar"
    target = tmp_path / "restored" / "odoo_staging"
    archive.parent.mkdir()
    target.parent.mkdir()
    # A real (safe) tar so member validation passes.
    import tarfile
    (source / "file.txt").write_text("x")
    with tarfile.open(archive, "w") as tf:
        tf.add(source, arcname="odoo_staging")
    calls = []

    def fake_run(args, *, stream=True):
        calls.append((args, stream))
        if args[:2] == ["tar", "-cf"]:
            return
        if "-xf" in args:
            extracted = Path(args[-1]) / "odoo_staging"
            extracted.mkdir()

    monkeypatch.setattr(filestore_module, "run", fake_run)

    adapter = FilestoreAdapter()
    adapter.archive(str(source), archive)
    adapter.restore_archive(archive, str(target))

    assert calls[0] == (["tar", "-cf", str(archive), "-C", str(source.parent), source.name], True)
    extract_args = calls[1][0]
    assert extract_args[0] == "tar" and "-xf" in extract_args
    assert "--no-same-owner" in extract_args and "--no-same-permissions" in extract_args
    assert calls[1][1] is True
    assert "--zstd" not in calls[0][0]
    assert "--zstd" not in extract_args


def test_docker_volume_filestore_streams_archive_restore_and_copy(tmp_path: Path, monkeypatch):
    import tarfile

    ctx = context(tmp_path)
    compose = DummyCompose("docker-compose.yml", str(tmp_path))
    monkeypatch.setattr(filestore_module, "DockerComposeAdapter", lambda *args, **kwargs: compose)
    adapter = DockerVolumeFilestore(ctx, ctx.config)
    source = tmp_path / "odoo_prod"
    source.mkdir()
    (source / "attachment").write_text("data")
    archive = tmp_path / "filestore.tar"
    with tarfile.open(archive, "w") as tf:
        tf.add(source, arcname="odoo_prod")

    adapter.archive("odoo_staging", archive)
    adapter.restore_archive(archive, "odoo_staging")
    adapter.copy("odoo_prod", "odoo_staging")

    assert compose.calls[0] == (
        "capture",
        "odoo",
        ["tar", "-cf", "-", "-C", "/var/lib/odoo/filestore", "odoo_staging"],
        archive,
    )
    assert compose.calls[1][0:3] == (
        "exec",
        "odoo",
        ["mkdir", "-p", "/var/lib/odoo/filestore"],
    )
    assert compose.calls[2][0:3] == (
        "exec",
        "odoo",
        ["rm", "-rf", "/var/lib/odoo/filestore/.odooctl-restore-odoo_staging"],
    )
    assert compose.calls[3][0:3] == (
        "exec",
        "odoo",
        ["mkdir", "-p", "/var/lib/odoo/filestore/.odooctl-restore-odoo_staging"],
    )
    assert compose.calls[4] == (
        "stdin",
        "odoo",
        [
            "tar",
            "--no-same-owner",
            "--no-same-permissions",
            "-xf",
            "-",
            "-C",
            "/var/lib/odoo/filestore/.odooctl-restore-odoo_staging",
        ],
        archive,
    )
    assert compose.calls[5][0:3] == (
        "exec",
        "odoo",
        ["rm", "-rf", "/var/lib/odoo/filestore/odoo_staging"],
    )
    assert compose.calls[6][0:3] == (
        "exec",
        "odoo",
        [
            "mv",
            "/var/lib/odoo/filestore/.odooctl-restore-odoo_staging/odoo_prod",
            "/var/lib/odoo/filestore/odoo_staging",
        ],
    )
    assert compose.calls[7][0:3] == (
        "exec",
        "odoo",
        ["rm", "-rf", "/var/lib/odoo/filestore/.odooctl-restore-odoo_staging"],
    )
    assert compose.calls[8][0:3] == (
        "exec",
        "odoo",
        ["mkdir", "-p", "/var/lib/odoo/filestore"],
    )
    assert compose.calls[9][0:3] == (
        "exec",
        "odoo",
        ["rm", "-rf", "/var/lib/odoo/filestore/odoo_staging"],
    )
    assert compose.calls[10][0:3] == (
        "exec",
        "odoo",
        ["cp", "-a", "/var/lib/odoo/filestore/odoo_prod", "/var/lib/odoo/filestore/odoo_staging"],
    )
    assert not any(
        call[2][:2] == ["sh", "-lc"] for call in compose.calls if call[0] == "exec"
    )


def test_docker_volume_drill_restore_cannot_overwrite_archive_named_production(
    tmp_path: Path,
    monkeypatch,
):
    import tarfile

    ctx = context(tmp_path)
    compose = DummyCompose("docker-compose.yml", str(tmp_path))
    monkeypatch.setattr(filestore_module, "DockerComposeAdapter", lambda *args, **kwargs: compose)
    source = tmp_path / "odoo_prod"
    source.mkdir()
    (source / "attachment").write_text("production data")
    archive = tmp_path / "production-filestore.tar"
    with tarfile.open(archive, "w") as tf:
        tf.add(source, arcname="odoo_prod")

    DockerVolumeFilestore(ctx, ctx.config).restore_archive(
        archive,
        "odoo_prod_dr_drill",
    )

    live_path = "/var/lib/odoo/filestore/odoo_prod"
    drill_path = "/var/lib/odoo/filestore/odoo_prod_dr_drill"
    assert not any(
        call[0] == "exec" and call[2] == ["rm", "-rf", live_path]
        for call in compose.calls
    )
    assert any(
        call[0] == "exec"
        and call[2]
        == [
            "mv",
            "/var/lib/odoo/filestore/.odooctl-restore-odoo_prod_dr_drill/odoo_prod",
            drill_path,
        ]
        for call in compose.calls
    )


def test_restore_archive_rejects_traversal_member(tmp_path: Path):
    """Re-scan #1: a filestore archive with a '..' member is rejected."""
    import tarfile

    from odooctl.adapters.filestore import FilestoreAdapter

    archive = tmp_path / "evil.tar"
    payload = tmp_path / "payload"
    payload.write_text("pwned")
    with tarfile.open(archive, "w") as tf:
        tf.add(payload, arcname="../escape/odoo_staging")

    with __import__("pytest").raises(RuntimeError, match="traversal|absolute"):
        FilestoreAdapter().restore_archive(archive, str(tmp_path / "restored" / "odoo_staging"))


def test_restore_archive_rejects_symlink_member(tmp_path: Path):
    import tarfile

    from odooctl.adapters.filestore import FilestoreAdapter

    archive = tmp_path / "evil.tar"
    with tarfile.open(archive, "w") as tf:
        info = tarfile.TarInfo("odoo_staging/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)

    with __import__("pytest").raises(RuntimeError, match="link"):
        FilestoreAdapter().restore_archive(archive, str(tmp_path / "restored" / "odoo_staging"))
