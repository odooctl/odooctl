from __future__ import annotations
import shutil
import tempfile
from pathlib import Path
from typing import Protocol

from odooctl.adapters.docker_compose import DockerComposeAdapter
from odooctl.config import EnvironmentConfig, OdooCtlConfig
from odooctl.context import ProjectContext
from odooctl.utils.paths import ensure_dir
from odooctl.utils.shell import run


def _validate_tar_members(archive: Path) -> None:
    """Reject a filestore archive with unsafe members before extraction.

    Codex re-scan #1: a crafted/imported backup could carry absolute paths,
    ``..`` traversal, or symlink/hardlink/device members that escape the
    extraction directory and overwrite sibling filestores or container files
    (manifest checksums prove consistency, not safety). We reject any such
    member up front; extraction still stages into a private temp dir.
    """
    import tarfile
    from pathlib import PurePosixPath

    try:
        with tarfile.open(archive, "r:*") as tf:
            for member in tf.getmembers():
                name = member.name
                if name.startswith("/") or PurePosixPath(name).is_absolute():
                    raise RuntimeError(f"Unsafe absolute path in archive: {name!r}")
                if ".." in PurePosixPath(name).parts:
                    raise RuntimeError(f"Unsafe '..' traversal in archive: {name!r}")
                if member.islnk() or member.issym():
                    raise RuntimeError(f"Refusing archive with link member: {name!r}")
                if member.isdev() or member.ischr() or member.isblk() or member.isfifo():
                    raise RuntimeError(f"Refusing archive with special-file member: {name!r}")
    except tarfile.TarError as exc:
        raise RuntimeError(f"Could not read filestore archive {archive}: {exc}") from exc


def _archive_root_name(archive: Path) -> str:
    """Return the archive's single top-level member after safety validation."""
    import tarfile
    from pathlib import PurePosixPath

    roots: set[str] = set()
    try:
        with tarfile.open(archive, "r:*") as tf:
            for member in tf.getmembers():
                parts = tuple(part for part in PurePosixPath(member.name).parts if part != ".")
                if parts:
                    roots.add(parts[0])
    except tarfile.TarError as exc:
        raise RuntimeError(f"Could not read filestore archive {archive}: {exc}") from exc
    if len(roots) != 1:
        raise RuntimeError(
            "Filestore archive must contain exactly one top-level directory; "
            f"found: {', '.join(sorted(roots)) or 'none'}"
        )
    return next(iter(roots))


class FilestoreBackend(Protocol):
    def archive(self, filestore_path: str, output: str | Path) -> None: ...
    def restore_archive(self, archive_path: str | Path, target_path: str) -> None: ...
    def copy(self, source: str, target: str) -> None: ...
    def delete(self, filestore_path: str) -> None: ...


class FilestoreAdapter:
    def archive(self, filestore_path: str, output: str | Path) -> None:
        source = Path(filestore_path)
        if not source.exists():
            raise FileNotFoundError(f"Filestore path does not exist: {filestore_path}")
        ensure_dir(Path(output).parent)
        run(["tar", "-cf", str(output), "-C", str(source.parent), source.name], stream=True)

    def restore_archive(self, archive_path: str | Path, target_path: str) -> None:
        archive = Path(archive_path)
        if not archive.exists():
            raise FileNotFoundError(f"Filestore archive does not exist: {archive_path}")
        target = Path(target_path)
        _validate_tar_members(archive)
        ensure_dir(target.parent)
        with tempfile.TemporaryDirectory(dir=target.parent, prefix=f".{target.name}.restore-") as tmpdir:
            # --no-same-owner/-permissions avoid honoring attacker-set metadata;
            # members were already validated to be traversal- and link-free.
            run(["tar", "--no-same-owner", "--no-same-permissions", "-xf", str(archive), "-C", tmpdir], stream=True)
            extracted = Path(tmpdir) / target.name
            if not extracted.exists():
                children = list(Path(tmpdir).iterdir())
                if len(children) != 1:
                    raise RuntimeError(f"Archive did not contain expected filestore directory: {target.name}")
                extracted = children[0]
            if target.exists():
                shutil.rmtree(target)
            shutil.move(str(extracted), target)

    def copy(self, source: str, target: str) -> None:
        src = Path(source)
        if not src.exists():
            raise FileNotFoundError(f"Source filestore path does not exist: {source}")
        dst = Path(target)
        ensure_dir(dst.parent)
        with tempfile.TemporaryDirectory(dir=dst.parent, prefix=f".{dst.name}.copy-") as tmpdir:
            staged = Path(tmpdir) / dst.name
            shutil.copytree(src, staged)
            if dst.exists():
                shutil.rmtree(dst)
            shutil.move(str(staged), dst)

    def delete(self, filestore_path: str) -> None:
        path = Path(filestore_path)
        if path.exists():
            shutil.rmtree(path)


class DockerVolumeFilestore:
    """Filestore backend for Odoo filestores stored in a Docker named volume.

    Odoo's official image stores filestores below ``/var/lib/odoo/filestore``.
    Archive/restore stream tar bytes through ``docker compose exec -T`` so hosts do
    not need a bind-mounted filestore path.
    """

    def __init__(self, context: ProjectContext, cfg: OdooCtlConfig):
        self.compose = DockerComposeAdapter(cfg.runtime.compose_file, project_dir=str(context.root))
        self.service = cfg.odoo.service
        self.root = cfg.odoo.filestore_container_path.rstrip("/")

    def _relative_name(self, filestore_path: str) -> str:
        return Path(filestore_path).name

    def _container_filestore_dir(self, filestore_path: str) -> str:
        return f"{self.root}/filestore/{self._relative_name(filestore_path)}"

    def archive(self, filestore_path: str, output: str | Path) -> None:
        ensure_dir(Path(output).parent)
        name = self._relative_name(filestore_path)
        self.compose.exec_capture_bytes(
            self.service,
            ["tar", "-cf", "-", "-C", f"{self.root}/filestore", name],
            stdout_path=output,
        )

    def restore_archive(self, archive_path: str | Path, target_path: str) -> None:
        archive = Path(archive_path)
        if not archive.exists():
            raise FileNotFoundError(f"Filestore archive does not exist: {archive_path}")
        _validate_tar_members(archive)
        archive_root = _archive_root_name(archive)
        name = self._relative_name(target_path)
        parent = f"{self.root}/filestore"
        stage = f"{parent}/.odooctl-restore-{name}"
        self.compose.exec(self.service, ["mkdir", "-p", parent], stream=True)
        self.compose.exec(self.service, ["rm", "-rf", stage], stream=True)
        self.compose.exec(self.service, ["mkdir", "-p", stage], stream=True)
        try:
            self.compose.exec_pipe_stdin(
                self.service,
                [
                    "tar",
                    "--no-same-owner",
                    "--no-same-permissions",
                    "-xf",
                    "-",
                    "-C",
                    stage,
                ],
                stdin_path=archive,
            )
            # Extract into a private directory first. The archive carries the
            # source DB's top-level name, which must never be written directly
            # into the live filestore root during a cross-name restore/drill.
            self.compose.exec(
                self.service,
                ["rm", "-rf", f"{parent}/{name}"],
                stream=True,
            )
            self.compose.exec(
                self.service,
                ["mv", f"{stage}/{archive_root}", f"{parent}/{name}"],
                stream=True,
            )
        finally:
            self.compose.exec(self.service, ["rm", "-rf", stage], stream=True)

    def copy(self, source: str, target: str) -> None:
        src = self._container_filestore_dir(source)
        dst = self._container_filestore_dir(target)
        self.compose.exec(self.service, ["mkdir", "-p", f"{self.root}/filestore"], stream=True)
        self.compose.exec(self.service, ["rm", "-rf", dst], stream=True)
        self.compose.exec(self.service, ["cp", "-a", src, dst], stream=True)

    def delete(self, filestore_path: str) -> None:
        target = self._container_filestore_dir(filestore_path)
        self.compose.exec(self.service, ["rm", "-rf", target], stream=True)


def make_filestore_adapter(context: ProjectContext, env: EnvironmentConfig) -> FilestoreBackend:
    if env.filestore_volume:
        return DockerVolumeFilestore(context, context.config)
    return FilestoreAdapter()
