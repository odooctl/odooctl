from __future__ import annotations
import json
import os
import uuid
from pathlib import Path
from odooctl.metadata.models import (
    BackupManifest,
    DeploymentMetadata,
    SanitizationMetadata,
    SnapshotManifest,
    SnapshotRestoreMetadata,
)
from odooctl.utils.paths import ensure_dir


class MetadataStore:
    def __init__(self, root: str | Path = ".odooctl"):
        self.root = ensure_dir(root)
        ensure_dir(self.root / "deployments")
        ensure_dir(self.root / "backups")
        ensure_dir(self.root / "sanitizations")
        ensure_dir(self.root / "snapshots")
        ensure_dir(self.root / "snapshots" / "restores")

    def save_deployment(self, metadata: DeploymentMetadata) -> Path:
        path = (
            self.root
            / "deployments"
            / f"{metadata.environment}-{metadata.timestamp.replace(':', '')}.json"
        )
        path.write_text(metadata.model_dump_json(indent=2))
        (self.root / "deployments" / f"{metadata.environment}-latest.json").write_text(
            metadata.model_dump_json(indent=2)
        )
        return path

    def save_backup_manifest(self, backup_id: str, manifest: BackupManifest) -> Path:
        safe_id = self._safe_backup_component(backup_id, "backup_id")
        if manifest.backup_id != safe_id:
            raise ValueError(
                f"Backup manifest identity mismatch: {safe_id!r} != {manifest.backup_id!r}"
            )
        environment = self._safe_backup_component(
            manifest.environment,
            "environment",
        )
        payload = manifest.model_dump_json(indent=2)
        path = self.root / "backups" / f"{safe_id}.json"
        self._write_atomic(path, payload)
        self._write_atomic(
            self.root / "backups" / f"{environment}-latest.json",
            payload,
        )
        return path

    def update_backup_manifest(self, manifest: BackupManifest) -> Path:
        """Atomically update one backup index without moving ``latest`` backwards.

        The environment latest pointer is refreshed only when it already
        references this backup. This is used for remote-upload/verification
        state transitions that may also target an older retained backup.
        """
        safe_id = self._safe_backup_component(
            manifest.backup_id,
            "backup_id",
        )
        environment = self._safe_backup_component(
            manifest.environment,
            "environment",
        )
        payload = manifest.model_dump_json(indent=2)
        path = self.root / "backups" / f"{safe_id}.json"
        self._write_atomic(path, payload)

        latest = self.root / "backups" / f"{environment}-latest.json"
        if latest.exists():
            try:
                current = BackupManifest.model_validate_json(latest.read_text())
            except Exception:
                current = None
            if current is not None and current.backup_id == safe_id:
                self._write_atomic(latest, payload)
        return path

    def synchronize_backup_manifests(
        self,
        environment: str,
        manifests: list[BackupManifest],
        *,
        latest_backup_id: str | None = None,
    ) -> None:
        """Make one environment's metadata index match published backups."""
        safe_environment = self._safe_backup_component(
            environment,
            "environment",
        )
        desired: dict[str, BackupManifest] = {}
        for manifest in manifests:
            if manifest.environment != safe_environment:
                raise ValueError(
                    f"Backup {manifest.backup_id!r} belongs to environment "
                    f"{manifest.environment!r}, not {safe_environment!r}"
                )
            safe_id = self._safe_backup_component(
                manifest.backup_id,
                "backup_id",
            )
            if safe_id in desired:
                raise ValueError(f"Duplicate backup manifest id: {safe_id}")
            desired[safe_id] = manifest

        backups_dir = self.root / "backups"
        for path in backups_dir.glob("*.json"):
            if path.name.endswith("-latest.json"):
                continue
            try:
                existing = BackupManifest.model_validate_json(path.read_text())
            except Exception:
                continue
            if existing.environment == safe_environment and existing.backup_id not in desired:
                path.unlink(missing_ok=True)

        for backup_id, manifest in desired.items():
            self._write_atomic(
                backups_dir / f"{backup_id}.json",
                manifest.model_dump_json(indent=2),
            )

        latest_path = backups_dir / f"{safe_environment}-latest.json"
        if not desired:
            latest_path.unlink(missing_ok=True)
            self._fsync_directory(backups_dir)
            return
        if latest_backup_id is None:
            latest_backup_id = max(
                desired,
                key=lambda backup_id: (
                    desired[backup_id].timestamp,
                    backup_id,
                ),
            )
        safe_latest_id = self._safe_backup_component(
            latest_backup_id,
            "latest_backup_id",
        )
        if safe_latest_id not in desired:
            raise ValueError(f"Latest backup {safe_latest_id!r} is not retained")
        self._write_atomic(
            latest_path,
            desired[safe_latest_id].model_dump_json(indent=2),
        )

    def save_sanitization(self, metadata: SanitizationMetadata) -> Path:
        timestamp = metadata.timestamp.replace(":", "")
        path = self.root / "sanitizations" / f"{metadata.target_environment}-{timestamp}.json"
        path.write_text(metadata.model_dump_json(indent=2))
        latest = self.root / "sanitizations" / f"{metadata.target_environment}-latest.json"
        latest.write_text(metadata.model_dump_json(indent=2))
        return path

    def save_snapshot_manifest(self, manifest: SnapshotManifest) -> Path:
        path = self._snapshot_path(manifest.snapshot_id)
        environment = self._safe_snapshot_component(
            manifest.environment,
            "environment",
        )
        payload = manifest.model_dump_json(indent=2)
        self._write_atomic(path, payload)
        latest = self.root / "snapshots" / f"{environment}-latest.json"
        self._write_atomic(latest, payload)
        return path

    def get_snapshot(self, snapshot_id: str) -> SnapshotManifest:
        path = self._snapshot_path(snapshot_id)
        if not path.exists():
            raise RuntimeError(f"Snapshot manifest not found: {snapshot_id}")
        manifest = SnapshotManifest.model_validate_json(path.read_text())
        if manifest.snapshot_id != snapshot_id:
            raise RuntimeError(
                f"Snapshot manifest identity mismatch: requested {snapshot_id!r}, "
                f"payload contains {manifest.snapshot_id!r}"
            )
        return manifest

    def list_snapshots(self, environment: str | None = None) -> list[SnapshotManifest]:
        if environment is not None:
            self._safe_snapshot_component(environment, "environment")
        manifests: list[SnapshotManifest] = []
        for path in (self.root / "snapshots").glob("*.json"):
            if path.name.endswith("-latest.json"):
                continue
            manifest = SnapshotManifest.model_validate_json(path.read_text())
            if manifest.snapshot_id != path.stem:
                raise RuntimeError(
                    f"Snapshot manifest identity mismatch: file {path.name!r} "
                    f"contains {manifest.snapshot_id!r}"
                )
            if environment is None or manifest.environment == environment:
                manifests.append(manifest)
        return sorted(manifests, key=lambda item: item.timestamp, reverse=True)

    def save_snapshot_restore(self, metadata: SnapshotRestoreMetadata) -> Path:
        snapshot_id = self._safe_snapshot_component(
            metadata.snapshot_id,
            "snapshot_id",
        )
        timestamp = metadata.timestamp.replace(":", "").replace("+", "")
        path = (
            self.root
            / "snapshots"
            / "restores"
            / f"{snapshot_id}-{timestamp}-{uuid.uuid4().hex[:8]}.json"
        )
        self._write_atomic(path, metadata.model_dump_json(indent=2))
        return path

    @staticmethod
    def _write_atomic(path: Path, payload: str) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        directory_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @staticmethod
    def _safe_backup_component(value: str, label: str) -> str:
        if (
            not value
            or value in {".", ".."}
            or Path(value).name != value
            or any(
                ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                for ch in value
            )
        ):
            raise ValueError(f"{label} contains unsupported characters")
        return value

    def _snapshot_path(self, snapshot_id: str) -> Path:
        safe_id = self._safe_snapshot_component(snapshot_id, "snapshot_id")
        return self.root / "snapshots" / f"{safe_id}.json"

    @staticmethod
    def _safe_snapshot_component(value: str, label: str) -> str:
        if (
            not value
            or value in {".", ".."}
            or Path(value).name != value
            or any(
                ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                for ch in value
            )
        ):
            raise ValueError(f"{label} contains unsupported characters")
        return value

    def latest_deployment(self, environment: str) -> dict | None:
        path = self.root / "deployments" / f"{environment}-latest.json"
        return json.loads(path.read_text()) if path.exists() else None

    def previous_successful_deployment(self, environment: str) -> dict | None:
        deployments_dir = self.root / "deployments"
        history = []
        for path in deployments_dir.glob(f"{environment}-*.json"):
            if path.name == f"{environment}-latest.json":
                continue
            data = json.loads(path.read_text())
            if data.get("environment") != environment:
                continue
            history.append(data)
        history.sort(key=lambda item: item.get("timestamp", ""), reverse=True)
        for data in history[1:]:
            if data.get("status") == "success":
                return data
        return None

    def latest_backup(self, environment: str) -> dict | None:
        path = self.root / "backups" / f"{environment}-latest.json"
        return json.loads(path.read_text()) if path.exists() else None
