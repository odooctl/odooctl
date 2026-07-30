from __future__ import annotations
import json
import os
import uuid
from pathlib import Path
from odooctl.metadata.models import (
    BackupManifest,
    DeploymentMetadata,
    PitrBaseBackupManifest,
    PitrRecoveryPlan,
    PitrRestoreMetadata,
    SanitizationMetadata,
    SnapshotManifest,
    SnapshotRestoreMetadata,
    WalReceipt,
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
        ensure_dir(self.root / "pitr")
        ensure_dir(self.root / "pitr" / "base")
        ensure_dir(self.root / "pitr" / "wal")
        ensure_dir(self.root / "pitr" / "plans")
        ensure_dir(self.root / "pitr" / "restores")

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

    def save_pitr_base_manifest(self, manifest: PitrBaseBackupManifest) -> Path:
        """Atomically create or update one physical base-backup manifest."""

        path = self._pitr_base_path(manifest.base_backup_id)
        self._guard_existing_pitr_identity(
            path,
            PitrBaseBackupManifest,
            manifest,
            (
                "base_backup_id",
                "project",
                "environment",
                "cluster_id",
                "system_identifier",
            ),
        )
        self._write_atomic(path, manifest.model_dump_json(indent=2))
        return path

    def get_pitr_base_manifest(self, base_backup_id: str) -> PitrBaseBackupManifest:
        path = self._pitr_base_path(base_backup_id)
        manifest = self._load_pitr_model(
            path,
            PitrBaseBackupManifest,
            "PITR base backup manifest",
        )
        if manifest.base_backup_id != base_backup_id:
            raise RuntimeError(
                "PITR base backup manifest identity mismatch: "
                f"requested {base_backup_id!r}, payload contains "
                f"{manifest.base_backup_id!r}"
            )
        return manifest

    def list_pitr_base_manifests(
        self,
        *,
        environment: str | None = None,
        cluster_id: str | None = None,
        system_identifier: str | None = None,
    ) -> list[PitrBaseBackupManifest]:
        if environment is not None:
            self._safe_pitr_component(environment, "environment")
        if cluster_id is not None:
            self._safe_pitr_component(cluster_id, "cluster_id")
        if system_identifier is not None:
            self._safe_system_identifier(system_identifier)
        manifests: list[PitrBaseBackupManifest] = []
        for path in (self.root / "pitr" / "base").glob("*.json"):
            manifest = self._load_pitr_model(
                path,
                PitrBaseBackupManifest,
                "PITR base backup manifest",
            )
            if manifest.base_backup_id != path.stem:
                raise RuntimeError(
                    f"PITR base backup manifest identity mismatch: file {path.name!r} "
                    f"contains {manifest.base_backup_id!r}"
                )
            if environment is not None and manifest.environment != environment:
                continue
            if cluster_id is not None and manifest.cluster_id != cluster_id:
                continue
            if (
                system_identifier is not None
                and manifest.system_identifier != system_identifier
            ):
                continue
            manifests.append(manifest)
        return sorted(
            manifests,
            key=lambda item: (
                item.completed_at or item.started_at,
                item.base_backup_id,
            ),
            reverse=True,
        )

    def save_wal_receipt(self, receipt: WalReceipt) -> Path:
        """Persist one immutable WAL receipt.

        Concurrent or repeated writers may publish the exact same receipt.
        Any different payload for the same cluster/system/filename is a hard
        conflict and never replaces the first receipt.
        """

        path = self._wal_receipt_path(
            receipt.cluster_id,
            receipt.system_identifier,
            receipt.filename,
            create_parent=True,
        )
        payload = receipt.model_dump_json(indent=2)
        existing = self._write_immutable(path, payload)
        if existing is not None:
            try:
                current = WalReceipt.model_validate_json(existing)
            except Exception:
                raise RuntimeError(
                    f"Existing WAL receipt is invalid and cannot be replaced: {path}"
                ) from None
            if current != receipt:
                raise RuntimeError(
                    "Conflicting WAL receipt cannot overwrite immutable receipt "
                    f"for {receipt.filename!r}"
                )
        return path

    def get_wal_receipt(
        self,
        cluster_id: str,
        system_identifier: str,
        filename: str,
    ) -> WalReceipt:
        path = self._wal_receipt_path(cluster_id, system_identifier, filename)
        receipt = self._load_pitr_model(path, WalReceipt, "WAL receipt")
        expected = (cluster_id, system_identifier, filename)
        actual = (
            receipt.cluster_id,
            receipt.system_identifier,
            receipt.filename,
        )
        if actual != expected:
            raise RuntimeError(
                f"WAL receipt identity mismatch: requested {expected!r}, "
                f"payload contains {actual!r}"
            )
        return receipt

    def find_wal_receipt(
        self,
        cluster_id: str,
        system_identifier: str,
        filename: str,
    ) -> WalReceipt | None:
        """Return one WAL receipt, or ``None`` only when it does not exist.

        Invalid files, symlinks, and identity mismatches remain hard errors so
        an archive retry cannot silently replace suspicious local state.
        """

        path = self._wal_receipt_path(cluster_id, system_identifier, filename)
        if not os.path.lexists(path):
            return None
        return self.get_wal_receipt(cluster_id, system_identifier, filename)

    def list_wal_receipts(
        self,
        *,
        environment: str | None = None,
        cluster_id: str | None = None,
        system_identifier: str | None = None,
    ) -> list[WalReceipt]:
        if environment is not None:
            self._safe_pitr_component(environment, "environment")
        if cluster_id is not None:
            self._safe_pitr_component(cluster_id, "cluster_id")
        if system_identifier is not None:
            self._safe_system_identifier(system_identifier)
        receipts: list[WalReceipt] = []
        wal_root = self.root / "pitr" / "wal"
        for path in wal_root.glob("*/*/*.json"):
            relative = path.relative_to(wal_root)
            path_cluster, path_system, receipt_name = relative.parts
            filename = receipt_name.removesuffix(".json")
            receipt = self._load_pitr_model(path, WalReceipt, "WAL receipt")
            expected = (path_cluster, path_system, filename)
            actual = (
                receipt.cluster_id,
                receipt.system_identifier,
                receipt.filename,
            )
            if actual != expected:
                raise RuntimeError(
                    f"WAL receipt identity mismatch: file {relative.as_posix()!r} "
                    f"contains {actual!r}"
                )
            if environment is not None and receipt.environment != environment:
                continue
            if cluster_id is not None and receipt.cluster_id != cluster_id:
                continue
            if (
                system_identifier is not None
                and receipt.system_identifier != system_identifier
            ):
                continue
            receipts.append(receipt)
        return sorted(
            receipts,
            key=lambda item: (item.archived_at, item.filename),
            reverse=True,
        )

    def save_pitr_recovery_plan(self, plan: PitrRecoveryPlan) -> Path:
        path = self._pitr_plan_path(plan.plan_id)
        self._guard_existing_pitr_identity(
            path,
            PitrRecoveryPlan,
            plan,
            (
                "plan_id",
                "project",
                "environment",
                "cluster_id",
                "system_identifier",
                "base_backup_id",
            ),
        )
        self._write_atomic(path, plan.model_dump_json(indent=2))
        return path

    def get_pitr_recovery_plan(self, plan_id: str) -> PitrRecoveryPlan:
        path = self._pitr_plan_path(plan_id)
        plan = self._load_pitr_model(path, PitrRecoveryPlan, "PITR recovery plan")
        if plan.plan_id != plan_id:
            raise RuntimeError(
                f"PITR recovery plan identity mismatch: requested {plan_id!r}, "
                f"payload contains {plan.plan_id!r}"
            )
        return plan

    def list_pitr_recovery_plans(
        self,
        *,
        environment: str | None = None,
    ) -> list[PitrRecoveryPlan]:
        if environment is not None:
            self._safe_pitr_component(environment, "environment")
        plans: list[PitrRecoveryPlan] = []
        for path in (self.root / "pitr" / "plans").glob("*.json"):
            plan = self._load_pitr_model(path, PitrRecoveryPlan, "PITR recovery plan")
            if plan.plan_id != path.stem:
                raise RuntimeError(
                    f"PITR recovery plan identity mismatch: file {path.name!r} "
                    f"contains {plan.plan_id!r}"
                )
            if environment is None or plan.environment == environment:
                plans.append(plan)
        return sorted(
            plans,
            key=lambda item: (item.created_at, item.plan_id),
            reverse=True,
        )

    def save_pitr_restore(self, metadata: PitrRestoreMetadata) -> Path:
        path = self._pitr_restore_path(metadata.restore_id)
        self._guard_existing_pitr_identity(
            path,
            PitrRestoreMetadata,
            metadata,
            (
                "restore_id",
                "plan_id",
                "base_backup_id",
                "project",
                "environment",
                "cluster_id",
                "system_identifier",
            ),
        )
        self._write_atomic(path, metadata.model_dump_json(indent=2))
        return path

    def get_pitr_restore(self, restore_id: str) -> PitrRestoreMetadata:
        path = self._pitr_restore_path(restore_id)
        metadata = self._load_pitr_model(
            path,
            PitrRestoreMetadata,
            "PITR restore metadata",
        )
        if metadata.restore_id != restore_id:
            raise RuntimeError(
                f"PITR restore metadata identity mismatch: requested {restore_id!r}, "
                f"payload contains {metadata.restore_id!r}"
            )
        return metadata

    def list_pitr_restores(
        self,
        *,
        environment: str | None = None,
    ) -> list[PitrRestoreMetadata]:
        if environment is not None:
            self._safe_pitr_component(environment, "environment")
        restores: list[PitrRestoreMetadata] = []
        for path in (self.root / "pitr" / "restores").glob("*.json"):
            metadata = self._load_pitr_model(
                path,
                PitrRestoreMetadata,
                "PITR restore metadata",
            )
            if metadata.restore_id != path.stem:
                raise RuntimeError(
                    f"PITR restore metadata identity mismatch: file {path.name!r} "
                    f"contains {metadata.restore_id!r}"
                )
            if environment is None or metadata.environment == environment:
                restores.append(metadata)
        return sorted(
            restores,
            key=lambda item: (item.timestamp, item.restore_id),
            reverse=True,
        )

    @staticmethod
    def _write_immutable(path: Path, payload: str) -> str | None:
        """Publish *payload* once; return the existing payload on collision."""

        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                # A hard-link publication is atomic and never replaces an
                # existing path, unlike os.replace used for mutable metadata.
                os.link(temporary, path)
            except FileExistsError:
                if path.is_symlink() or not path.is_file():
                    raise RuntimeError(
                        f"Immutable metadata path is not a regular file: {path}"
                    ) from None
                try:
                    return path.read_text()
                except OSError as exc:
                    raise RuntimeError(
                        f"Could not read immutable metadata collision at {path}: {exc}"
                    ) from None
            MetadataStore._fsync_directory(path.parent)
            return None
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _load_pitr_model(path: Path, model_class, label: str):
        if not path.exists():
            raise RuntimeError(f"{label} not found: {path.stem}")
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"{label} is not a regular file: {path}")
        try:
            return model_class.model_validate_json(path.read_text())
        except Exception as exc:
            raise RuntimeError(f"{label} is invalid: {path}: {exc}") from None

    @staticmethod
    def _guard_existing_pitr_identity(
        path: Path,
        model_class,
        candidate,
        identity_fields: tuple[str, ...],
    ) -> None:
        if not path.exists() and not path.is_symlink():
            return
        current = MetadataStore._load_pitr_model(
            path,
            model_class,
            model_class.__name__,
        )
        current_identity = tuple(getattr(current, field) for field in identity_fields)
        candidate_identity = tuple(getattr(candidate, field) for field in identity_fields)
        if current_identity != candidate_identity:
            raise RuntimeError(
                f"{model_class.__name__} identity conflict at {path.name!r}"
            )

    def _pitr_base_path(self, base_backup_id: str) -> Path:
        safe_id = self._safe_pitr_component(base_backup_id, "base_backup_id")
        return self.root / "pitr" / "base" / f"{safe_id}.json"

    def _wal_receipt_path(
        self,
        cluster_id: str,
        system_identifier: str,
        filename: str,
        *,
        create_parent: bool = False,
    ) -> Path:
        safe_cluster = self._safe_pitr_component(cluster_id, "cluster_id")
        safe_system = self._safe_system_identifier(system_identifier)
        safe_filename = self._safe_pitr_component(filename, "filename")
        parent = self.root / "pitr" / "wal" / safe_cluster / safe_system
        if create_parent:
            ensure_dir(parent)
        return parent / f"{safe_filename}.json"

    def _pitr_plan_path(self, plan_id: str) -> Path:
        safe_id = self._safe_pitr_component(plan_id, "plan_id")
        return self.root / "pitr" / "plans" / f"{safe_id}.json"

    def _pitr_restore_path(self, restore_id: str) -> Path:
        safe_id = self._safe_pitr_component(restore_id, "restore_id")
        return self.root / "pitr" / "restores" / f"{safe_id}.json"

    @staticmethod
    def _safe_pitr_component(value: str, label: str) -> str:
        if (
            not value
            or len(value) > 128
            or value in {".", ".."}
            or ".." in value
            or Path(value).name != value
            or any(
                character
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                for character in value
            )
        ):
            raise ValueError(f"{label} contains unsupported characters")
        return value

    @staticmethod
    def _safe_system_identifier(value: str) -> str:
        if (
            not value
            or len(value) > 20
            or value[0] == "0"
            or not value.isascii()
            or not value.isdigit()
        ):
            raise ValueError(
                "system_identifier must be a decimal PostgreSQL system identifier"
            )
        return value

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
