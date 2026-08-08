"""Disposable PostgreSQL + Odoo boundary for disaster-recovery drills."""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from odooctl.adapters.filestore import _archive_root_name, _validate_tar_members
from odooctl.context import ProjectContext
from odooctl.odoo.healthcheck import with_db_selector
from odooctl.utils.shell import run, run_pipe_stdin

if TYPE_CHECKING:
    from odooctl.services.dr import DrDrillTarget


DATABASE_ALIAS = "odooctl-dr-db"
DATABASE_USER = "odooctl_dr"
DATABASE_PORT = "5432"
CONTAINER_CONFIG_PATH = "/odooctl-config/odoo.conf"


@dataclass(frozen=True)
class _Resources:
    network: str
    postgres_container: str
    odoo_container: str
    filestore_volume: str
    config_volume: str


class DockerComposeDrillRuntime:
    """Create a network-isolated, disposable PostgreSQL/Odoo restore target.

    The live Compose services are never joined to the drill's internal network.
    PostgreSQL has no published port and stores its cluster on tmpfs. Odoo is
    the only peer on that network and exposes only an ephemeral loopback HTTP
    port for the health probe. The restored filestore uses a dedicated volume.
    """

    def __init__(self, context: ProjectContext):
        self.context = context
        self.cfg = context.config
        self._password: str | None = None
        self._config_path: Path | None = None
        self._prepared_identity: str | None = None

    def _identity(self, target: DrDrillTarget) -> str:
        value = (
            f"{self.cfg.project.name}:{target.environment}:"
            f"{target.backup_id}:{target.database}"
        )
        return hashlib.sha256(value.encode()).hexdigest()[:12]

    def _resources(self, target: DrDrillTarget) -> _Resources:
        digest = self._identity(target)
        project = re.sub(r"[^A-Za-z0-9_.-]", "-", self.cfg.project.name)[:24]
        prefix = f"odooctl-{project}-dr-{digest}"
        return _Resources(
            network=f"{prefix}-net",
            postgres_container=f"{prefix}-postgres",
            odoo_container=f"{prefix}-odoo",
            filestore_volume=f"{prefix}-filestore",
            config_volume=f"{prefix}-config",
        )

    def _assert_target(self, target: DrDrillTarget) -> None:
        expected = (
            f"{self.cfg.odoo.filestore_container_path.rstrip('/')}"
            f"/filestore/{target.database}"
        )
        if target.filestore_path != expected:
            raise RuntimeError(
                "DR filestore must be the isolated runtime's database-specific path: "
                f"expected {expected!r}, got {target.filestore_path!r}"
            )
        if (
            self._prepared_identity is not None
            and self._prepared_identity != self._identity(target)
        ):
            raise RuntimeError("DR runtime callbacks received different target identities")

    def _postgres_image(self) -> str:
        result = run(
            [
                "docker",
                "compose",
                "-f",
                str(self.context.compose_file),
                "images",
                "-q",
                self.cfg.postgres.service,
            ],
            cwd=str(self.context.root),
        )
        images = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(images) != 1:
            raise RuntimeError(
                "Could not resolve exactly one PostgreSQL image from the configured "
                f"Compose service {self.cfg.postgres.service!r}"
            )
        return images[0]

    def _write_odoo_config(self, target: DrDrillTarget) -> None:
        assert self._password is not None
        state_dir = self.context.state_dir
        state_dir.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(
            prefix=".dr-drill-",
            suffix=".conf",
            dir=state_dir,
            text=True,
        )
        os.fchmod(fd, 0o600)
        config_lines = [
            "[options]",
            f"db_host = {DATABASE_ALIAS}",
            f"db_port = {DATABASE_PORT}",
            f"db_user = {DATABASE_USER}",
            f"db_password = {self._password}",
            f"data_dir = {self.cfg.odoo.filestore_container_path}",
            "list_db = False",
            "proxy_mode = False",
        ]
        if self.cfg.odoo.addons_paths:
            config_lines.append(f"addons_path = {','.join(self.cfg.odoo.addons_paths)}")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write("\n".join(config_lines) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            Path(raw_path).unlink(missing_ok=True)
            raise
        self._config_path = Path(raw_path)

    def _install_odoo_config(self, target: DrDrillTarget) -> None:
        """Copy the secret config through stdin into its disposable volume."""
        if self._config_path is None or self._password is None:
            raise RuntimeError("Temporary DR Odoo config has not been created")
        resources = self._resources(target)
        try:
            run_pipe_stdin(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--interactive",
                    "--network",
                    "none",
                    "--user",
                    "0:0",
                    "--volume",
                    f"{resources.config_volume}:/config",
                    "--entrypoint",
                    "dd",
                    self.cfg.odoo.image,
                    "of=/config/odoo.conf",
                    "status=none",
                ],
                cwd=str(self.context.root),
                stdin_path=self._config_path,
            )
            run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--user",
                    "0:0",
                    "--volume",
                    f"{resources.config_volume}:/config",
                    "--entrypoint",
                    "chmod",
                    self.cfg.odoo.image,
                    "0444",
                    "/config/odoo.conf",
                ],
                cwd=str(self.context.root),
            )
        finally:
            self._config_path.unlink(missing_ok=True)
            self._config_path = None

    def prepare(self, target: DrDrillTarget) -> None:
        """Create the internal network, empty filestore, and fresh PostgreSQL."""
        self._assert_target(target)
        self._prepared_identity = self._identity(target)
        resources = self._resources(target)
        # Resolve the configured database image before creating any Docker
        # resources or materializing the generated drill credential.
        postgres_image = self._postgres_image()
        self._password = secrets.token_urlsafe(32)
        self._write_odoo_config(target)

        label = f"odooctl.drill={self._identity(target)}"
        run(
            [
                "docker",
                "network",
                "create",
                "--internal",
                "--label",
                label,
                resources.network,
            ],
            cwd=str(self.context.root),
        )
        run(
            [
                "docker",
                "volume",
                "create",
                "--label",
                label,
                resources.filestore_volume,
            ],
            cwd=str(self.context.root),
        )
        run(
            [
                "docker",
                "volume",
                "create",
                "--label",
                label,
                resources.config_volume,
            ],
            cwd=str(self.context.root),
        )
        self._install_odoo_config(target)

        postgres_env = {
            "POSTGRES_USER": DATABASE_USER,
            "POSTGRES_PASSWORD": self._password,
            "POSTGRES_DB": "postgres",
            "PGDATA": "/var/lib/postgresql/data/pgdata",
        }
        env_args = [
            item
            for name in postgres_env
            for item in ("--env", name)
        ]
        run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                resources.postgres_container,
                "--network",
                resources.network,
                "--network-alias",
                DATABASE_ALIAS,
                "--tmpfs",
                "/var/lib/postgresql/data:rw,nosuid,nodev",
                *env_args,
                postgres_image,
            ],
            cwd=str(self.context.root),
            env=postgres_env,
        )
        self._wait_for_postgres(resources)

    def _wait_for_postgres(self, resources: _Resources) -> None:
        assert self._password is not None
        password_env = {"PGPASSWORD": self._password}
        args = [
            "docker",
            "exec",
            "--env",
            "PGPASSWORD",
            resources.postgres_container,
            "pg_isready",
            "--host",
            "127.0.0.1",
            "--username",
            DATABASE_USER,
            "--dbname",
            "postgres",
        ]
        for _ in range(30):
            result = run(
                args,
                cwd=str(self.context.root),
                env=password_env,
                check=False,
            )
            if result.returncode == 0:
                return
            time.sleep(1)
        raise RuntimeError("Disposable DR PostgreSQL did not become ready")

    def restore_database(self, target: DrDrillTarget, dump_path: Path) -> None:
        """Restore the dump only into the disposable PostgreSQL container."""
        self._assert_target(target)
        if self._password is None or self._prepared_identity is None:
            raise RuntimeError("Disposable DR PostgreSQL has not been prepared")
        resources = self._resources(target)
        password_env = {"PGPASSWORD": self._password}
        base = [
            "docker",
            "exec",
            "--env",
            "PGPASSWORD",
            resources.postgres_container,
        ]
        run(
            [
                *base,
                "createdb",
                "--host",
                "127.0.0.1",
                "--username",
                DATABASE_USER,
                target.database,
            ],
            cwd=str(self.context.root),
            env=password_env,
        )
        run_pipe_stdin(
            [
                "docker",
                "exec",
                "--interactive",
                "--env",
                "PGPASSWORD",
                resources.postgres_container,
                "pg_restore",
                "--host",
                "127.0.0.1",
                "--username",
                DATABASE_USER,
                "--dbname",
                target.database,
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
            ],
            cwd=str(self.context.root),
            env=password_env,
            stdin_path=dump_path,
        )

    def _volume_helper(
        self,
        target: DrDrillTarget,
        entrypoint: str,
        args: list[str],
    ) -> None:
        resources = self._resources(target)
        run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                "0:0",
                "--volume",
                f"{resources.filestore_volume}:/target",
                "--entrypoint",
                entrypoint,
                self.cfg.odoo.image,
                *args,
            ],
            cwd=str(self.context.root),
        )

    def restore_filestore(self, target: DrDrillTarget, archive_path: Path) -> None:
        """Restore the archive into a new volume, never the live Odoo volume."""
        self._assert_target(target)
        if self._prepared_identity is None:
            raise RuntimeError("Disposable DR filestore has not been prepared")
        archive = Path(archive_path)
        _validate_tar_members(archive)
        archive_root = _archive_root_name(archive)
        resources = self._resources(target)
        stage = "/target/.odooctl-restore"
        destination = f"/target/filestore/{target.database}"
        self._volume_helper(
            target,
            "mkdir",
            ["-p", stage, "/target/filestore"],
        )
        run_pipe_stdin(
            [
                "docker",
                "run",
                "--rm",
                "--interactive",
                "--network",
                "none",
                "--user",
                "0:0",
                "--volume",
                f"{resources.filestore_volume}:/target",
                "--entrypoint",
                "tar",
                self.cfg.odoo.image,
                "--no-same-owner",
                "--no-same-permissions",
                "-xf",
                "-",
                "-C",
                stage,
            ],
            cwd=str(self.context.root),
            stdin_path=archive,
        )
        self._volume_helper(
            target,
            "mv",
            [f"{stage}/{archive_root}", destination],
        )
        # Match the image's configured data-directory ownership without
        # assuming a numeric Odoo uid/gid.
        self._volume_helper(
            target,
            "chown",
            [
                "--recursive",
                "--reference",
                self.cfg.odoo.filestore_container_path,
                "/target",
            ],
        )

    def start(self, target: DrDrillTarget) -> str:
        """Start isolated Odoo and return its ephemeral loopback health URL."""
        self._assert_target(target)
        if (
            self._password is None
            or self._prepared_identity is None
        ):
            raise RuntimeError("Disposable DR environment has not been prepared")

        resources = self._resources(target)
        odoo = self.cfg.odoo
        run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                resources.odoo_container,
                "--network",
                resources.network,
                "--publish",
                "127.0.0.1::8069",
                "--volume",
                (
                    f"{resources.filestore_volume}:"
                    f"{odoo.filestore_container_path}"
                ),
                "--volume",
                f"{resources.config_volume}:/odooctl-config:ro",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev",
                "--security-opt",
                "no-new-privileges",
                "--cap-drop",
                "ALL",
                "--entrypoint",
                odoo.cli_command,
                odoo.image,
                "--config",
                CONTAINER_CONFIG_PATH,
                "--database",
                target.database,
                "--db-filter",
                f"^{re.escape(target.database)}$",
                "--data-dir",
                odoo.filestore_container_path,
                "--http-interface",
                "0.0.0.0",
                "--http-port",
                "8069",
                "--max-cron-threads",
                "0",
                "--workers",
                "0",
                "--no-database-list",
            ],
            cwd=str(self.context.root),
        )

        port_result = run(
            ["docker", "port", resources.odoo_container, "8069/tcp"],
            cwd=str(self.context.root),
        )
        port = _published_port(port_result.stdout)
        url = f"http://127.0.0.1:{port}{self.cfg.healthcheck.path}"
        return with_db_selector(url, target.database)

    def stop(self, target: DrDrillTarget) -> None:
        """Remove both containers, the volume, network, and secret config."""
        resources = self._resources(target)
        errors: list[str] = []
        removals = (
            (
                ["docker", "rm", "-f", resources.odoo_container],
                ("No such container",),
                "Odoo container",
            ),
            (
                ["docker", "rm", "-f", resources.postgres_container],
                ("No such container",),
                "PostgreSQL container",
            ),
            (
                ["docker", "volume", "rm", resources.filestore_volume],
                ("no such volume", "No such volume"),
                "filestore volume",
            ),
            (
                ["docker", "volume", "rm", resources.config_volume],
                ("no such volume", "No such volume"),
                "secret config volume",
            ),
            (
                ["docker", "network", "rm", resources.network],
                ("not found", "No such network"),
                "internal network",
            ),
        )
        for args, absent_markers, label in removals:
            try:
                result = run(
                    args,
                    cwd=str(self.context.root),
                    check=False,
                )
            except Exception as exc:
                # Keep trying every independent resource and always clear the
                # in-process credential/config state, even if Docker itself
                # becomes unavailable partway through teardown.
                errors.append(f"{label}: cleanup command failed: {exc}")
                continue
            combined = f"{result.stdout}\n{result.stderr}"
            if result.returncode != 0 and not any(
                marker in combined for marker in absent_markers
            ):
                errors.append(
                    f"{label}: {result.stderr.strip() or result.stdout.strip()}"
                )
        if self._config_path is not None:
            try:
                self._config_path.unlink(missing_ok=True)
            except OSError as exc:
                errors.append(f"secret config: {exc}")
        self._config_path = None
        self._password = None
        self._prepared_identity = None
        if errors:
            raise RuntimeError("; ".join(errors))


def _published_port(output: str) -> int:
    """Extract Docker's ephemeral host port from ``docker port`` output."""
    for line in output.splitlines():
        candidate = line.strip().rsplit(":", 1)[-1]
        if candidate.isdigit():
            port = int(candidate)
            if 1 <= port <= 65535:
                return port
    raise RuntimeError(f"Could not determine isolated DR runtime port from: {output!r}")
