from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

from odooctl.adapters.s3 import S3IntegrityError, S3ObjectInfo
from odooctl.config import OdooCtlConfig
from odooctl.metadata.models import BackupManifest
from odooctl.metadata.store import MetadataStore
from odooctl.services import remote_backup as remote_svc


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _context(
    tmp_path: Path,
    *,
    policy: str = "required",
    verify_after_upload: bool = True,
    retention: dict[str, int] | None = None,
    project: str = "demo",
    orphan_grace_hours: int = 24,
):
    config = OdooCtlConfig.model_validate(
        {
            "project": {
                "name": project,
                "odoo_version": "19.0",
            },
            "odoo": {"image": "odoo:19.0"},
            "environments": {
                "production": {
                    "branch": "main",
                    "domain": "odoo.example.com",
                    "db_name": "odoo_prod",
                    "filestore_path": "./filestore/odoo_prod",
                },
                "staging": {
                    "branch": "staging",
                    "domain": "staging.example.com",
                    "db_name": "odoo_staging",
                    "filestore_path": "./filestore/odoo_staging",
                },
            },
            "backups": {
                "local_path": "backups",
                "retention": retention
                or {
                    "daily": 7,
                    "weekly": 4,
                    "monthly": 6,
                },
                "remote": {
                    "bucket": "demo-backups",
                    "prefix": "demo",
                    "policy": policy,
                    "verify_after_upload": verify_after_upload,
                    "orphan_grace_hours": orphan_grace_hours,
                    "secret_key_env": "ODOO_S3_SECRET_KEY",
                    "access_key_env": "ODOO_S3_ACCESS_KEY",
                },
            },
        }
    )
    project = SimpleNamespace(
        config=config,
        root=tmp_path,
        backups_dir=tmp_path / "backups",
        state_dir=tmp_path / ".odooctl",
        odoo_config_path=tmp_path / "missing-odoo.conf",
        resolve_path=lambda value: (tmp_path / value).resolve(),
    )
    return SimpleNamespace(project=project)


def _local_backup(ctx, backup_id: str = "production_2026-07-30_120000"):
    backup_dir = ctx.project.backups_dir / backup_id
    backup_dir.mkdir(parents=True)
    db = b"database dump"
    filestore = b"filestore archive"
    (backup_dir / "db.dump").write_bytes(db)
    (backup_dir / "filestore.tar").write_bytes(filestore)
    manifest = BackupManifest(
        backup_id=backup_id,
        project="demo",
        environment="production",
        timestamp="2026-07-30T12:00:00Z",
        db_name="odoo_prod",
        odoo_version="19.0",
        artifact_paths=["db.dump", "filestore.tar"],
        checksums={
            "db_dump": _sha(db),
            "filestore": _sha(filestore),
        },
    )
    (backup_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2))
    MetadataStore(ctx.project.state_dir).save_backup_manifest(
        backup_id,
        manifest,
    )
    return backup_dir, manifest


class MemoryS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.last_modified: dict[str, datetime] = {}
        self.payload_observed_status: str | None = None
        self.fail_upload: Exception | None = None
        self.fail_after_payload_count: int | None = None
        self.fail_verify: Exception | None = None
        self.interrupt_after_manifest = False
        self.corrupt_download = False
        self.fail_delete_once: set[str] = set()
        self.deleted: list[str] = []
        self.list_calls = 0
        self.fail_list_on_call: int | None = None
        self.abandonment_head_calls: list[str] = []
        self.before_delete = None

    @staticmethod
    def _prefix(backup_id: str) -> str:
        return f"demo/{backup_id}/"

    def manifest_key(self, backup_id: str) -> str:
        return self._prefix(backup_id) + "manifest.json"

    def abandonment_key(self, backup_id: str) -> str:
        return self._prefix(backup_id) + "abandoned.json"

    def abandonment_fence_exists(self, backup_id: str) -> bool:
        self.abandonment_head_calls.append(backup_id)
        return self.abandonment_key(backup_id) in self.objects

    def assert_not_abandoned(self, backup_id: str) -> None:
        if self.abandonment_fence_exists(backup_id):
            raise S3IntegrityError(
                "An abandoned remote backup id cannot be republished; "
                "create a new globally unique backup"
            )

    def upload_object(self, source: Path, key: str):
        data = Path(source).read_bytes()
        self.objects[key] = data
        self.last_modified[key] = datetime.now(timezone.utc)
        return S3ObjectInfo(
            key=key,
            size=len(data),
            sha256=_sha(data),
            last_modified=self.last_modified[key],
        )

    def download_object(self, key: str, destination: Path):
        data = self.objects[key]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return S3ObjectInfo(
            key=key,
            size=len(data),
            sha256=_sha(data),
            last_modified=self.last_modified.get(key),
        )

    def upload_backup_payload(self, backup_dir: Path):
        self.payload_observed_status = json.loads((backup_dir / "manifest.json").read_text())[
            "remote_status"
        ]
        self.remove_completion_marker(backup_dir.name)
        if self.fail_upload is not None:
            raise self.fail_upload
        infos = []
        for name in ("db.dump", "filestore.tar"):
            data = (backup_dir / name).read_bytes()
            key = self._prefix(backup_dir.name) + name
            self.objects[key] = data
            self.last_modified[key] = datetime.now(timezone.utc)
            infos.append(
                S3ObjectInfo(
                    key=key,
                    size=len(data),
                    sha256=_sha(data),
                )
            )
            if (
                self.fail_after_payload_count is not None
                and len(infos) >= self.fail_after_payload_count
            ):
                raise RuntimeError("interrupted payload upload")
        return infos

    def upload_manifest(self, source: Path, *, backup_name: str | None = None):
        assert backup_name is not None
        data = Path(source).read_bytes()
        key = self.manifest_key(backup_name)
        self.objects[key] = data
        self.last_modified[key] = datetime.now(timezone.utc)
        if self.interrupt_after_manifest:
            raise KeyboardInterrupt
        return S3ObjectInfo(
            key=key,
            size=len(data),
            sha256=_sha(data),
        )

    def remove_completion_marker(self, backup_id: str) -> bool:
        key = self.manifest_key(backup_id)
        existed = key in self.objects
        self.objects.pop(key, None)
        self.last_modified.pop(key, None)
        return existed

    def verify_backup(self, backup_id: str):
        if self.fail_verify is not None:
            raise self.fail_verify
        prefix = self._prefix(backup_id)
        if self.manifest_key(backup_id) not in self.objects:
            raise S3IntegrityError("missing manifest")
        return [
            S3ObjectInfo(
                key=key,
                size=len(data),
                sha256=_sha(data),
            )
            for key, data in sorted(self.objects.items())
            if key.startswith(prefix)
        ]

    def verify_object_content(
        self,
        key: str,
        *,
        expected_sha256: str | None = None,
    ):
        data = self.objects[key]
        digest = _sha(data)
        if expected_sha256 is not None and digest != expected_sha256:
            raise S3IntegrityError("content checksum mismatch")
        return S3ObjectInfo(
            key=key,
            size=len(data),
            sha256=digest,
        )

    def list_backups(self):
        return sorted(
            key.removeprefix("demo/").removesuffix("/manifest.json")
            for key in self.objects
            if key.endswith("/manifest.json")
        )

    def list_objects(self, backup_name: str | None = None):
        self.list_calls += 1
        if self.fail_list_on_call == self.list_calls:
            raise RuntimeError("provider inventory unavailable")
        prefix = self._prefix(backup_name) if backup_name is not None else "demo/"
        return [
            S3ObjectInfo(
                key=key,
                size=len(data),
                last_modified=self.last_modified.get(key),
            )
            for key, data in sorted(self.objects.items())
            if key.startswith(prefix)
        ]

    def download_manifest(
        self,
        backup_id: str,
        destination: Path,
        **kwargs,
    ):
        data = self.objects[self.manifest_key(backup_id)]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return S3ObjectInfo(
            key=self.manifest_key(backup_id),
            size=len(data),
            sha256=_sha(data),
        )

    def delete_backup(self, backup_id: str):
        marker = self.manifest_key(backup_id)
        self.objects.pop(marker, None)
        self.last_modified.pop(marker, None)
        if backup_id in self.fail_delete_once:
            self.fail_delete_once.remove(backup_id)
            raise RuntimeError("transient delete failure")
        if self.before_delete is not None:
            callback = self.before_delete
            self.before_delete = None
            callback()
        prefix = self._prefix(backup_id)
        abandonment = self.abandonment_key(backup_id)
        keys = [
            key
            for key in self.objects
            if key.startswith(prefix) and key != abandonment
        ]
        for key in keys:
            self.objects.pop(key)
            self.last_modified.pop(key, None)
        self.deleted.append(backup_id)
        return keys

    def download_backup(self, backup_id: str, destination_root: Path):
        target = destination_root / backup_id
        target.mkdir(parents=True)
        prefix = self._prefix(backup_id)
        for key, data in self.objects.items():
            if key.startswith(prefix):
                relative = key[len(prefix) :]
                path = target / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if self.corrupt_download and relative == "filestore.tar":
                    path.write_bytes(b"corrupt downloaded filestore")
                else:
                    path.write_bytes(data)
        return target


def test_required_publish_uses_pending_local_state_and_final_marker(tmp_path):
    ctx = _context(tmp_path)
    backup_dir, manifest = _local_backup(ctx)
    adapter = MemoryS3()

    result = remote_svc.publish_remote_backup(
        ctx,
        backup_dir,
        manifest,
        adapter=adapter,
    )

    assert adapter.payload_observed_status == "pending"
    assert result.status == "complete"
    remote_manifest = json.loads(adapter.objects[adapter.manifest_key(manifest.backup_id)])
    assert remote_manifest["remote_status"] == "complete"
    local = BackupManifest.model_validate_json((backup_dir / "manifest.json").read_text())
    assert local.remote_status == "complete"
    assert local.remote_verified_at is not None


def test_project_namespace_prevents_shared_bucket_prefix_collisions(
    tmp_path,
):
    first = _context(tmp_path / "one", project="alpha")
    second = _context(tmp_path / "two", project="beta")
    first_adapter = remote_svc.make_remote_adapter(first)
    second_adapter = remote_svc.make_remote_adapter(second)
    backup_id = "production_2026-07-30_120000_same"

    assert first_adapter.manifest_key(backup_id) != second_adapter.manifest_key(backup_id)
    assert remote_svc.remote_backup_uri(
        first.project.config.backups.remote,
        backup_id,
        project="alpha",
    ) != remote_svc.remote_backup_uri(
        second.project.config.backups.remote,
        backup_id,
        project="beta",
    )


def test_interruption_after_marker_never_advertises_unverified_payload(
    tmp_path,
):
    ctx = _context(tmp_path)
    backup_dir, manifest = _local_backup(ctx)
    adapter = MemoryS3()
    adapter.interrupt_after_manifest = True

    with pytest.raises(KeyboardInterrupt):
        remote_svc.publish_remote_backup(
            ctx,
            backup_dir,
            manifest,
            adapter=adapter,
        )

    remote_manifest = json.loads(adapter.objects[adapter.manifest_key(manifest.backup_id)])
    assert remote_manifest["remote_status"] == "complete"
    assert remote_manifest["remote_verified_at"] is not None
    for name, checksum_key in (
        ("db.dump", "db_dump"),
        ("filestore.tar", "filestore"),
    ):
        data = adapter.objects[adapter._prefix(manifest.backup_id) + name]
        assert _sha(data) == remote_manifest["checksums"][checksum_key]
    local = BackupManifest.model_validate_json((backup_dir / "manifest.json").read_text())
    assert local.remote_status == "pending"


def test_required_publish_failure_is_durable_and_secret_redacted(
    tmp_path,
    monkeypatch,
):
    ctx = _context(tmp_path)
    backup_dir, manifest = _local_backup(ctx)
    adapter = MemoryS3()
    secret = "highly-secret-provider-value"
    monkeypatch.setenv("ODOO_S3_ACCESS_KEY", "access-identifier")
    monkeypatch.setenv("ODOO_S3_SECRET_KEY", secret)
    adapter.fail_upload = RuntimeError(f"provider rejected {secret}")

    with pytest.raises(
        remote_svc.RemoteBackupPolicyError,
        match="Required remote backup failed",
    ) as exc_info:
        remote_svc.publish_remote_backup(
            ctx,
            backup_dir,
            manifest,
            adapter=adapter,
        )

    assert secret not in str(exc_info.value)
    local = BackupManifest.model_validate_json((backup_dir / "manifest.json").read_text())
    assert local.remote_status == "failed"
    assert local.remote_error is not None
    assert secret not in local.remote_error
    assert backup_dir.is_dir()
    assert adapter.manifest_key(manifest.backup_id) not in adapter.objects


def test_best_effort_publish_failure_returns_degraded_local_backup(tmp_path):
    ctx = _context(tmp_path, policy="best_effort")
    backup_dir, manifest = _local_backup(ctx)
    adapter = MemoryS3()
    adapter.fail_upload = RuntimeError("provider unavailable")

    result = remote_svc.publish_remote_backup(
        ctx,
        backup_dir,
        manifest,
        adapter=adapter,
    )

    assert result.status == "degraded"
    assert result.error == "provider unavailable"
    assert backup_dir.is_dir()


def test_remote_verify_streams_objects_and_checks_manifest_identity(tmp_path):
    ctx = _context(tmp_path)
    backup_dir, manifest = _local_backup(ctx)
    adapter = MemoryS3()
    remote_svc.publish_remote_backup(
        ctx,
        backup_dir,
        manifest,
        adapter=adapter,
    )

    result = remote_svc.verify_remote_backup(
        ctx,
        "production",
        manifest.backup_id,
        adapter=adapter,
    )

    assert result.object_count == 3
    assert result.manifest.remote_status == "complete"
    assert result.uri.endswith(f"/{manifest.backup_id}")


def test_remote_verify_rejects_payload_that_disagrees_with_manifest(tmp_path):
    ctx = _context(tmp_path, policy="best_effort")
    backup_dir, manifest = _local_backup(ctx)
    adapter = MemoryS3()
    remote_svc.publish_remote_backup(
        ctx,
        backup_dir,
        manifest,
        adapter=adapter,
    )
    adapter.objects[adapter._prefix(manifest.backup_id) + "db.dump"] = b"wrong database bytes"

    with pytest.raises(S3IntegrityError, match="does not match"):
        remote_svc.verify_remote_backup(
            ctx,
            "production",
            manifest.backup_id,
            adapter=adapter,
        )

    local = BackupManifest.model_validate_json((backup_dir / "manifest.json").read_text())
    assert local.remote_status == "degraded"


def test_remote_verify_does_not_link_same_id_with_different_local_checksums(
    tmp_path,
):
    ctx = _context(tmp_path, policy="best_effort")
    backup_dir, manifest = _local_backup(ctx)
    adapter = MemoryS3()
    remote_svc.publish_remote_backup(
        ctx,
        backup_dir,
        manifest,
        adapter=adapter,
    )
    local = BackupManifest.model_validate_json(
        (backup_dir / "manifest.json").read_text()
    ).model_copy(
        update={
            "checksums": {
                **manifest.checksums,
                "db_dump": "0" * 64,
            }
        }
    )
    (backup_dir / "manifest.json").write_text(local.model_dump_json(indent=2))

    with pytest.raises(
        S3IntegrityError,
        match="same artifacts",
    ):
        remote_svc.verify_remote_backup(
            ctx,
            "production",
            manifest.backup_id,
            adapter=adapter,
        )

    persisted = BackupManifest.model_validate_json((backup_dir / "manifest.json").read_text())
    assert persisted.remote_status == "degraded"
    assert persisted.checksums["db_dump"] == "0" * 64


def test_remote_download_recovers_despite_corrupt_same_id_local_manifest(
    tmp_path,
):
    ctx = _context(tmp_path, policy="best_effort")
    backup_dir, manifest = _local_backup(ctx)
    adapter = MemoryS3()
    remote_svc.publish_remote_backup(
        ctx,
        backup_dir,
        manifest,
        adapter=adapter,
    )
    corrupt_local = BackupManifest.model_validate_json(
        (backup_dir / "manifest.json").read_text()
    ).model_copy(
        update={
            "checksums": {
                **manifest.checksums,
                "db_dump": "0" * 64,
            },
            "remote_status": "degraded",
            "remote_error": "local manifest mismatch",
        }
    )
    (backup_dir / "manifest.json").write_text(corrupt_local.model_dump_json(indent=2))
    recovery_root = tmp_path / "recovery"

    recovered = remote_svc.download_remote_backup(
        ctx,
        "production",
        manifest.backup_id,
        destination_root=recovery_root,
        adapter=adapter,
    )

    assert recovered == recovery_root / manifest.backup_id
    recovered_manifest = BackupManifest.model_validate_json(
        (recovered / "manifest.json").read_text()
    )
    assert recovered_manifest.checksums == manifest.checksums
    unchanged_local = BackupManifest.model_validate_json((backup_dir / "manifest.json").read_text())
    assert unchanged_local.remote_status == "degraded"
    assert unchanged_local.checksums["db_dump"] == "0" * 64


def _seed_remote_marker(
    adapter: MemoryS3,
    *,
    backup_id: str,
    timestamp: str,
    project: str = "demo",
    environment: str = "production",
) -> BackupManifest:
    manifest = BackupManifest(
        backup_id=backup_id,
        project=project,
        environment=environment,
        timestamp=timestamp,
        db_name="odoo_prod",
        odoo_version="19.0",
        artifact_paths=["db.dump", "filestore.tar"],
        checksums={
            "db_dump": "a" * 64,
            "filestore": "b" * 64,
        },
        remote_status="complete",
    )
    adapter.objects[adapter.manifest_key(backup_id)] = manifest.model_dump_json(indent=2).encode()
    for name, data in (
        ("db.dump", b"seed database"),
        ("filestore.tar", b"seed filestore"),
    ):
        adapter.objects[adapter._prefix(backup_id) + name] = data
    modified = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    for key in tuple(adapter.objects):
        if key.startswith(adapter._prefix(backup_id)):
            adapter.last_modified[key] = modified
    return manifest


def test_remote_list_filters_shared_legacy_prefix_by_manifest_ownership(
    tmp_path,
    monkeypatch,
):
    ctx = _context(tmp_path)
    scoped = MemoryS3()
    legacy = MemoryS3()
    scoped_owned = _seed_remote_marker(
        scoped,
        backup_id="production_2026-07-30_100000",
        timestamp="2026-07-30T10:00:00Z",
    )
    legacy_owned = _seed_remote_marker(
        legacy,
        backup_id="production_2026-07-29_100000",
        timestamp="2026-07-29T10:00:00Z",
    )
    foreign = _seed_remote_marker(
        legacy,
        backup_id="production_2026-07-31_100000",
        timestamp="2026-07-31T10:00:00Z",
        project="another-project",
    )
    foreign_payload = json.loads(legacy.objects[legacy.manifest_key(foreign.backup_id)])
    foreign_payload.pop("db_name")
    legacy.objects[legacy.manifest_key(foreign.backup_id)] = json.dumps(foreign_payload).encode()
    _seed_remote_marker(
        legacy,
        backup_id="production_2026-07-30_110000",
        timestamp="2026-07-30T11:00:00Z",
        environment="staging",
    )
    monkeypatch.setattr(
        remote_svc,
        "_remote_read_adapters",
        lambda service_ctx: [scoped, legacy],
    )

    assert remote_svc.list_remote_backups(
        ctx,
        "production",
    ) == [
        scoped_owned.backup_id,
        legacy_owned.backup_id,
    ]


def test_remote_latest_skips_newer_foreign_legacy_marker(
    tmp_path,
    monkeypatch,
):
    ctx = _context(tmp_path)
    scoped = MemoryS3()
    legacy = MemoryS3()
    _seed_remote_marker(
        scoped,
        backup_id="production_z_lexically_newer_but_older",
        timestamp="2026-07-28T10:00:00Z",
    )
    owned = _seed_remote_marker(
        legacy,
        backup_id="production_a_newest_owned",
        timestamp="2026-07-30T10:00:00Z",
    )
    foreign = _seed_remote_marker(
        legacy,
        backup_id="production_zz_newest_foreign",
        timestamp="2026-07-31T10:00:00Z",
        project="another-project",
    )
    monkeypatch.setattr(
        remote_svc,
        "_remote_read_adapters",
        lambda service_ctx: [scoped, legacy],
    )

    assert (
        remote_svc.resolve_remote_backup_id(
            ctx,
            "production",
            "latest",
        )
        == owned.backup_id
    )
    with pytest.raises(
        remote_svc.RemoteBackupOwnershipError,
        match="different project",
    ):
        remote_svc.resolve_remote_backup_id(
            ctx,
            "production",
            foreign.backup_id,
        )


def test_remote_gfs_pruning_checks_project_ownership(tmp_path):
    ctx = _context(
        tmp_path,
        policy="best_effort",
        retention={"daily": 0, "weekly": 0, "monthly": 0},
        orphan_grace_hours=1,
    )
    adapter = MemoryS3()
    newest = _seed_remote_marker(
        adapter,
        backup_id="production_newest",
        timestamp="2026-07-30T12:00:00Z",
    )
    old = _seed_remote_marker(
        adapter,
        backup_id="production_old",
        timestamp="2026-07-29T12:00:00Z",
    )
    foreign = _seed_remote_marker(
        adapter,
        backup_id="production_foreign",
        timestamp="2026-07-28T12:00:00Z",
        project="another-project",
    )

    result = remote_svc.prune_remote_backups(
        ctx,
        environment="production",
        adapter=adapter,
    )

    assert result.deleted_backup_ids == (old.backup_id,)
    assert result.error is None
    assert adapter.deleted == [old.backup_id]
    assert adapter.manifest_key(newest.backup_id) in adapter.objects
    assert adapter.manifest_key(foreign.backup_id) in adapter.objects


def test_remote_retention_grace_prevents_cross_publisher_mutual_deletion(
    tmp_path,
):
    ctx = _context(
        tmp_path,
        policy="best_effort",
        retention={
            "daily": 0,
            "weekly": 0,
            "monthly": 0,
            "grace_hours": 1,
        },
    )
    adapter = MemoryS3()
    first = _seed_remote_marker(
        adapter,
        backup_id="production_publisher_a",
        timestamp="2026-07-30T12:00:00Z",
    )
    second = _seed_remote_marker(
        adapter,
        backup_id="production_publisher_b",
        timestamp="2026-07-30T12:00:01Z",
    )
    fresh = datetime.now(timezone.utc)
    for key in adapter.last_modified:
        adapter.last_modified[key] = fresh

    first_pass = remote_svc.prune_remote_backups(
        ctx,
        environment="production",
        adapter=adapter,
        protected_backup_ids=(first.backup_id,),
    )
    second_pass = remote_svc.prune_remote_backups(
        ctx,
        environment="production",
        adapter=adapter,
        protected_backup_ids=(second.backup_id,),
    )

    assert first_pass.deleted_backup_ids == ()
    assert second_pass.deleted_backup_ids == ()
    assert adapter.manifest_key(first.backup_id) in adapter.objects
    assert adapter.manifest_key(second.backup_id) in adapter.objects

    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    for key in adapter.last_modified:
        adapter.last_modified[key] = old
    reconciled = remote_svc.prune_remote_backups(
        ctx,
        environment="production",
        adapter=adapter,
    )

    assert reconciled.deleted_backup_ids == (
        first.backup_id,
    )
    assert adapter.manifest_key(first.backup_id) not in adapter.objects
    assert adapter.manifest_key(second.backup_id) in adapter.objects


def test_remote_retention_retries_provider_delete_on_next_run(tmp_path):
    ctx = _context(
        tmp_path,
        policy="best_effort",
        retention={"daily": 0, "weekly": 0, "monthly": 0},
        orphan_grace_hours=1,
    )
    adapter = MemoryS3()
    _seed_remote_marker(
        adapter,
        backup_id="production_newest",
        timestamp="2026-07-30T12:00:00Z",
    )
    old = _seed_remote_marker(
        adapter,
        backup_id="production_old",
        timestamp="2026-07-29T12:00:00Z",
    )
    adapter.fail_delete_once.add(old.backup_id)

    first = remote_svc.prune_remote_backups(
        ctx,
        environment="production",
        adapter=adapter,
    )
    assert first.error is not None
    assert adapter.manifest_key(old.backup_id) not in adapter.objects
    assert adapter._prefix(old.backup_id) + "db.dump" in adapter.objects
    assert adapter.abandonment_key(old.backup_id) in adapter.objects
    for key in tuple(adapter.last_modified):
        if key.startswith(adapter._prefix(old.backup_id)):
            adapter.last_modified[key] = datetime(
                2020,
                1,
                1,
                tzinfo=timezone.utc,
            )

    second = remote_svc.prune_remote_backups(
        ctx,
        environment="production",
        adapter=adapter,
    )

    assert adapter.manifest_key(old.backup_id) not in adapter.objects
    assert second.deleted_backup_ids == (old.backup_id,)


def test_remote_retention_cleans_old_markerless_upload_but_not_fresh_one(
    tmp_path,
):
    ctx = _context(
        tmp_path,
        policy="best_effort",
        retention={"daily": 0, "weekly": 0, "monthly": 0},
        orphan_grace_hours=24,
    )
    backup_dir, manifest = _local_backup(ctx)
    adapter = MemoryS3()
    adapter.fail_after_payload_count = 1

    published = remote_svc.publish_remote_backup(
        ctx,
        backup_dir,
        manifest,
        adapter=adapter,
    )

    assert published.status == "degraded"
    payload_key = adapter._prefix(manifest.backup_id) + "db.dump"
    assert payload_key in adapter.objects
    assert adapter.manifest_key(manifest.backup_id) not in adapter.objects

    fresh = remote_svc.prune_remote_backups(
        ctx,
        environment="production",
        adapter=adapter,
    )
    assert fresh.deleted_backup_ids == ()
    assert payload_key in adapter.objects

    for key in tuple(adapter.last_modified):
        if key.startswith(adapter._prefix(manifest.backup_id)):
            adapter.last_modified[key] = datetime(
                2020,
                1,
                1,
                tzinfo=timezone.utc,
            )
    expired = remote_svc.prune_remote_backups(
        ctx,
        environment="production",
        adapter=adapter,
    )
    assert expired.deleted_backup_ids == (manifest.backup_id,)
    assert payload_key not in adapter.objects
    fence_key = adapter.abandonment_key(manifest.backup_id)
    assert fence_key in adapter.objects

    # Cleanup may remove every payload object, but it must never make the id
    # publishable again for a delayed worker holding the same local backup.
    adapter.fail_after_payload_count = None
    delayed = remote_svc.publish_remote_backup(
        ctx,
        backup_dir,
        manifest,
        adapter=adapter,
    )
    assert delayed.status == "degraded"
    assert "cannot be republished" in (delayed.error or "")
    assert fence_key in adapter.objects
    assert adapter.manifest_key(manifest.backup_id) not in adapter.objects
    assert payload_key not in adapter.objects


def test_abandoned_remote_id_cannot_be_republished(tmp_path):
    ctx = _context(tmp_path, policy="best_effort")
    backup_dir, manifest = _local_backup(ctx)
    adapter = MemoryS3()
    adapter.fail_after_payload_count = 1
    first = remote_svc.publish_remote_backup(
        ctx,
        backup_dir,
        manifest,
        adapter=adapter,
    )
    assert first.status == "degraded"
    assert adapter.abandonment_key(manifest.backup_id) in adapter.objects
    adapter.fail_after_payload_count = None

    second = remote_svc.publish_remote_backup(
        ctx,
        backup_dir,
        manifest,
        adapter=adapter,
    )

    assert second.status == "degraded"
    assert "cannot be republished" in (second.error or "")
    assert adapter.manifest_key(manifest.backup_id) not in adapter.objects
    assert adapter.abandonment_head_calls.count(manifest.backup_id) >= 2


def test_orphan_cleanup_fence_blocks_interleaved_same_id_publisher(
    tmp_path,
):
    ctx = _context(
        tmp_path,
        policy="best_effort",
        orphan_grace_hours=1,
    )
    backup_dir, manifest = _local_backup(ctx)
    adapter = MemoryS3()
    adapter.fail_after_payload_count = 1
    first = remote_svc.publish_remote_backup(
        ctx,
        backup_dir,
        manifest,
        adapter=adapter,
    )
    assert first.status == "degraded"
    adapter.fail_after_payload_count = None
    for key in tuple(adapter.last_modified):
        if key.startswith(adapter._prefix(manifest.backup_id)):
            adapter.last_modified[key] = datetime(
                2020,
                1,
                1,
                tzinfo=timezone.utc,
            )

    interleaved = []
    adapter.before_delete = lambda: interleaved.append(
        remote_svc.publish_remote_backup(
            ctx,
            backup_dir,
            manifest,
            adapter=adapter,
        )
    )
    cleaned = remote_svc.prune_remote_backups(
        ctx,
        environment="production",
        adapter=adapter,
    )

    assert cleaned.deleted_backup_ids == (manifest.backup_id,)
    assert len(interleaved) == 1
    assert interleaved[0].status == "degraded"
    assert "cannot be republished" in (interleaved[0].error or "")
    assert adapter.abandonment_key(manifest.backup_id) in adapter.objects
    assert adapter.manifest_key(manifest.backup_id) not in adapter.objects
    assert adapter._prefix(manifest.backup_id) + "db.dump" not in adapter.objects


def test_late_completion_marker_cannot_resurrect_fenced_backup(
    tmp_path,
):
    ctx = _context(
        tmp_path,
        policy="best_effort",
        orphan_grace_hours=1,
    )
    backup_dir, manifest = _local_backup(ctx)
    adapter = MemoryS3()
    adapter.fail_after_payload_count = 1
    failed = remote_svc.publish_remote_backup(
        ctx,
        backup_dir,
        manifest,
        adapter=adapter,
    )
    assert failed.status == "degraded"

    # Represent a publisher that had already passed its final fence probe and
    # landed a completion marker after abandonment was recorded.
    for name in ("db.dump", "filestore.tar"):
        key = adapter._prefix(manifest.backup_id) + name
        adapter.objects[key] = (backup_dir / name).read_bytes()
    late_complete = manifest.model_copy(
        update={
            "remote_status": "complete",
            "remote_error": None,
        }
    )
    adapter.objects[adapter.manifest_key(manifest.backup_id)] = (
        late_complete.model_dump_json(indent=2).encode()
    )
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    for key in tuple(adapter.objects):
        if key.startswith(adapter._prefix(manifest.backup_id)):
            adapter.last_modified[key] = old

    assert remote_svc.list_remote_backups(
        ctx,
        "production",
        adapter=adapter,
    ) == []
    with pytest.raises(S3IntegrityError, match="cannot be republished"):
        remote_svc.verify_remote_backup(
            ctx,
            "production",
            manifest.backup_id,
            adapter=adapter,
        )
    with pytest.raises(S3IntegrityError, match="cannot be republished"):
        remote_svc.download_remote_backup(
            ctx,
            "production",
            manifest.backup_id,
            adapter=adapter,
            destination_root=tmp_path / "fenced-download",
        )

    cleaned = remote_svc.prune_remote_backups(
        ctx,
        environment="production",
        adapter=adapter,
    )
    assert cleaned.deleted_backup_ids == (manifest.backup_id,)
    assert sorted(
        key
        for key in adapter.objects
        if key.startswith(adapter._prefix(manifest.backup_id))
    ) == [adapter.abandonment_key(manifest.backup_id)]


def test_orphan_cleanup_rejects_foreign_abandonment_marker(tmp_path):
    ctx = _context(
        tmp_path,
        policy="best_effort",
        orphan_grace_hours=1,
    )
    backup_dir, manifest = _local_backup(ctx)
    adapter = MemoryS3()
    adapter.fail_after_payload_count = 1
    remote_svc.publish_remote_backup(
        ctx,
        backup_dir,
        manifest,
        adapter=adapter,
    )
    tombstone_key = adapter.abandonment_key(manifest.backup_id)
    tombstone = json.loads(adapter.objects[tombstone_key])
    tombstone["project"] = "foreign-project"
    adapter.objects[tombstone_key] = json.dumps(tombstone).encode()
    for key in tuple(adapter.last_modified):
        if key.startswith(adapter._prefix(manifest.backup_id)):
            adapter.last_modified[key] = datetime(
                2020,
                1,
                1,
                tzinfo=timezone.utc,
            )

    result = remote_svc.prune_remote_backups(
        ctx,
        environment="production",
        adapter=adapter,
    )

    assert result.deleted_backup_ids == ()
    assert "marker identity is invalid" in (result.error or "")
    assert adapter._prefix(manifest.backup_id) + "db.dump" in adapter.objects


def test_remote_retention_never_deletes_aged_active_upload_without_tombstone(
    tmp_path,
):
    ctx = _context(
        tmp_path,
        policy="best_effort",
        retention={"daily": 0, "weekly": 0, "monthly": 0},
        orphan_grace_hours=1,
    )
    backup_dir, manifest = _local_backup(ctx)
    adapter = MemoryS3()
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    for name in ("db.dump", "filestore.tar"):
        key = adapter._prefix(manifest.backup_id) + name
        adapter.objects[key] = (backup_dir / name).read_bytes()
        adapter.last_modified[key] = old

    reconciliation = remote_svc.prune_remote_backups(
        ctx,
        environment="production",
        adapter=adapter,
    )

    assert reconciliation.deleted_backup_ids == ()
    assert "manual review required" in (
        reconciliation.error or ""
    )
    assert adapter.deleted == []

    resumed = remote_svc.publish_remote_backup(
        ctx,
        backup_dir,
        manifest,
        adapter=adapter,
    )
    assert resumed.status == "complete"
    assert adapter.manifest_key(manifest.backup_id) in adapter.objects
    assert all(
        adapter._prefix(manifest.backup_id) + name
        in adapter.objects
        for name in ("db.dump", "filestore.tar")
    )


def test_failed_download_validation_removes_only_new_target(tmp_path):
    ctx = _context(tmp_path)
    backup_dir, manifest = _local_backup(ctx)
    adapter = MemoryS3()
    remote_svc.publish_remote_backup(
        ctx,
        backup_dir,
        manifest,
        adapter=adapter,
    )
    shutil.rmtree(backup_dir)
    adapter.corrupt_download = True
    destination = tmp_path / "downloaded"

    with pytest.raises(RuntimeError):
        remote_svc.download_remote_backup(
            ctx,
            "production",
            manifest.backup_id,
            destination_root=destination,
            adapter=adapter,
        )

    assert not (destination / manifest.backup_id).exists()
    assert not list(destination.glob(".remote-download-*"))


def test_downloading_older_legacy_timestamp_does_not_move_latest_backward(
    tmp_path,
):
    ctx = _context(tmp_path)
    newer_dir, newer = _local_backup(
        ctx,
        backup_id="production_2026-07-30_120000_newer",
    )
    newer = newer.model_copy(
        update={"timestamp": "2026-07-30T12:00:00.100000Z"}
    )
    (newer_dir / "manifest.json").write_text(
        newer.model_dump_json(indent=2)
    )
    store = MetadataStore(ctx.project.state_dir)
    store.save_backup_manifest(newer.backup_id, newer)

    older_dir, older = _local_backup(
        ctx,
        backup_id="production_2026-07-30_120000_older",
    )
    older = older.model_copy(
        update={"timestamp": "2026-07-30T12:00:00Z"}
    )
    (older_dir / "manifest.json").write_text(
        older.model_dump_json(indent=2)
    )
    adapter = MemoryS3()
    remote_svc.publish_remote_backup(
        ctx,
        older_dir,
        older,
        adapter=adapter,
    )
    shutil.rmtree(older_dir)
    store.save_backup_manifest(newer.backup_id, newer)

    downloaded = remote_svc.download_remote_backup(
        ctx,
        "production",
        older.backup_id,
        adapter=adapter,
    )

    assert downloaded.name == older.backup_id
    latest = store.latest_backup("production")
    assert latest is not None
    assert latest["backup_id"] == newer.backup_id


def _install_local_backup_fakes(monkeypatch):
    from odooctl.services import backup as backup_svc

    class FakePostgres:
        def __init__(self, config):
            pass

        def dump(self, database, path):
            Path(path).write_bytes(b"database dump")

    class FakeFilestore:
        def archive(self, source, path):
            Path(path).write_bytes(b"filestore archive")

    monkeypatch.setattr(backup_svc, "PostgresAdapter", FakePostgres)
    monkeypatch.setattr(backup_svc, "FilestoreAdapter", FakeFilestore)
    monkeypatch.setattr(backup_svc, "git_commit", lambda cwd=None: "abc123")


def test_run_backup_required_remote_failure_keeps_local_backup(
    tmp_path,
    monkeypatch,
):
    from odooctl.services import backup as backup_svc

    ctx = _context(tmp_path, policy="required")
    adapter = MemoryS3()
    adapter.fail_upload = RuntimeError("provider unavailable")
    _install_local_backup_fakes(monkeypatch)
    monkeypatch.setattr(
        remote_svc,
        "make_remote_adapter",
        lambda service_ctx: adapter,
    )

    with pytest.raises(remote_svc.RemoteBackupPolicyError):
        backup_svc.run_backup(ctx, "production")

    published = list(ctx.project.backups_dir.glob("production_*"))
    assert len(published) == 1
    manifest = BackupManifest.model_validate_json((published[0] / "manifest.json").read_text())
    assert manifest.remote_status == "failed"
    assert not list(ctx.project.backups_dir.glob(".partial-*"))


def test_interruption_before_remote_network_leaves_truthful_pending_state(
    tmp_path,
    monkeypatch,
):
    from odooctl.services import backup as backup_svc

    ctx = _context(tmp_path, policy="required")
    _install_local_backup_fakes(monkeypatch)
    monkeypatch.setattr(
        remote_svc,
        "make_remote_adapter",
        lambda service_ctx: MemoryS3(),
    )

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(
        remote_svc,
        "publish_remote_backup",
        interrupt,
    )

    with pytest.raises(KeyboardInterrupt):
        backup_svc.run_backup(ctx, "production")

    published = list(ctx.project.backups_dir.glob("production_*"))
    assert len(published) == 1
    local = BackupManifest.model_validate_json((published[0] / "manifest.json").read_text())
    indexed = MetadataStore(ctx.project.state_dir).latest_backup("production")
    assert local.remote_status == "pending"
    assert local.remote_uri is not None
    assert indexed is not None
    assert indexed["remote_status"] == "pending"
    assert indexed["remote_uri"] == local.remote_uri


def test_run_backup_best_effort_remote_failure_reports_degraded(
    tmp_path,
    monkeypatch,
):
    from odooctl.services import backup as backup_svc

    ctx = _context(tmp_path, policy="best_effort")
    adapter = MemoryS3()
    adapter.fail_upload = RuntimeError("provider unavailable")
    _install_local_backup_fakes(monkeypatch)
    monkeypatch.setattr(
        remote_svc,
        "make_remote_adapter",
        lambda service_ctx: adapter,
    )

    result = backup_svc.run_backup(ctx, "production")

    assert result.remote_status == "degraded"
    assert result.remote_error == "provider unavailable"
    assert (ctx.project.backups_dir / result.backup_id).is_dir()


def test_run_backup_best_effort_records_retention_inventory_failure(
    tmp_path,
    monkeypatch,
):
    from odooctl.services import backup as backup_svc

    ctx = _context(tmp_path, policy="best_effort")
    adapter = MemoryS3()
    adapter.fail_list_on_call = 2
    _install_local_backup_fakes(monkeypatch)
    monkeypatch.setattr(
        remote_svc,
        "make_remote_adapter",
        lambda service_ctx: adapter,
    )

    result = backup_svc.run_backup(ctx, "production")

    assert result.remote_status == "complete"
    assert "provider inventory unavailable" in (
        result.remote_error or ""
    )
    manifest = BackupManifest.model_validate_json(
        (
            ctx.project.backups_dir
            / result.backup_id
            / "manifest.json"
        ).read_text()
    )
    assert manifest.remote_status == "complete"
    assert "provider inventory unavailable" in (
        manifest.remote_error or ""
    )


def test_run_backup_required_fails_on_retention_inventory_failure(
    tmp_path,
    monkeypatch,
):
    from odooctl.services import backup as backup_svc

    ctx = _context(tmp_path, policy="required")
    adapter = MemoryS3()
    adapter.fail_list_on_call = 2
    _install_local_backup_fakes(monkeypatch)
    monkeypatch.setattr(
        remote_svc,
        "make_remote_adapter",
        lambda service_ctx: adapter,
    )

    with pytest.raises(
        remote_svc.RemoteBackupPolicyError,
        match="Required remote retention failed",
    ):
        backup_svc.run_backup(ctx, "production")

    published = list(
        ctx.project.backups_dir.glob("production_*")
    )
    assert len(published) == 1
    manifest = BackupManifest.model_validate_json(
        (published[0] / "manifest.json").read_text()
    )
    assert manifest.remote_status == "complete"
    assert "provider inventory unavailable" in (
        manifest.remote_error or ""
    )


def test_backup_cli_warns_when_best_effort_remote_copy_is_degraded(
    tmp_path,
    monkeypatch,
):
    from odooctl.commands import backup as backup_cmd
    from odooctl.main import app
    from odooctl.services.models import BackupResult

    ctx = _context(tmp_path, policy="best_effort")
    (tmp_path / "odooctl.yml").write_text(
        yaml.safe_dump(
            ctx.project.config.model_dump(mode="json"),
            sort_keys=False,
        )
    )
    monkeypatch.setattr(
        backup_cmd,
        "run_backup",
        lambda service_ctx, environment: BackupResult(
            backup_id="production_test",
            remote_uri="s3://demo-backups/demo/production_test",
            remote_status="degraded",
            remote_error="provider unavailable",
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "backup",
            "production",
        ],
    )

    assert result.exit_code == 0
    assert "production_test" in result.stdout
    assert "remote backup is degraded" in result.stderr
    assert "provider unavailable" in result.stderr


def test_backup_cli_and_operation_records_redact_standard_aws_credentials(
    tmp_path,
    monkeypatch,
):
    from odooctl.main import app

    ctx = _context(tmp_path, policy="best_effort")
    remote = ctx.project.config.backups.remote
    assert remote is not None
    ctx.project.config.backups.remote = remote.model_copy(
        update={
            "access_key_env": None,
            "secret_key_env": None,
        }
    )
    (tmp_path / "odooctl.yml").write_text(
        yaml.safe_dump(
            ctx.project.config.model_dump(mode="json"),
            sort_keys=False,
        )
    )
    token_file_secret = "default-chain-container-file-token"
    token_file = tmp_path / "container-authorization-token"
    token_file.write_text(token_file_secret + "\n")
    credentials = {
        "AWS_ACCESS_KEY_ID": "default-chain-access-id",
        "AWS_SECRET_ACCESS_KEY": "default-chain-secret-value",
        "AWS_SESSION_TOKEN": "default-chain-session-token",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI": (
            "https://user:password@credentials.example.test/"
            "?signature=default-chain-signed-query"
        ),
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE": str(token_file),
    }
    for name, value in credentials.items():
        monkeypatch.setenv(name, value)
    secret_values = (*credentials.values(), token_file_secret)
    adapter = MemoryS3()
    adapter.fail_upload = RuntimeError(
        "provider echoed " + " ".join(secret_values)
    )
    _install_local_backup_fakes(monkeypatch)
    monkeypatch.setattr(
        remote_svc,
        "make_remote_adapter",
        lambda service_ctx: adapter,
    )

    result = CliRunner().invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "backup",
            "production",
        ],
    )

    assert result.exit_code == 0, result.output
    rendered = result.stdout + result.stderr
    assert "***" in rendered
    assert all(
        value not in rendered
        for value in secret_values
    )
    persisted_text = "\n".join(
        path.read_text(errors="ignore")
        for path in (tmp_path / ".odooctl").rglob("*")
        if path.is_file()
    )
    local_manifest_text = next(
        (tmp_path / "backups").glob("production_*/manifest.json")
    ).read_text()
    assert all(
        value not in persisted_text
        and value not in local_manifest_text
        for value in secret_values
    )


def test_run_backup_prunes_remote_only_after_verified_replacement(
    tmp_path,
    monkeypatch,
):
    from odooctl.services import backup as backup_svc

    ctx = _context(
        tmp_path,
        policy="required",
        retention={"daily": 0, "weekly": 0, "monthly": 0},
    )
    old_dir, old_manifest = _local_backup(
        ctx,
        backup_id="production_2026-07-29_120000",
    )
    adapter = MemoryS3()
    for name in ("db.dump", "filestore.tar", "manifest.json"):
        if name == "manifest.json":
            payload = (
                old_manifest.model_copy(update={"remote_status": "complete"})
                .model_dump_json(indent=2)
                .encode()
            )
        else:
            payload = (old_dir / name).read_bytes()
        adapter.objects[adapter._prefix(old_manifest.backup_id) + name] = payload
    stale_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
    os.utime(
        old_dir / "manifest.json",
        (
            stale_time.timestamp(),
            stale_time.timestamp(),
        ),
    )
    for key in adapter.objects:
        adapter.last_modified[key] = stale_time
    _install_local_backup_fakes(monkeypatch)
    monkeypatch.setattr(
        remote_svc,
        "make_remote_adapter",
        lambda service_ctx: adapter,
    )

    result = backup_svc.run_backup(ctx, "production")

    assert result.remote_status == "complete"
    assert not old_dir.exists()
    assert old_manifest.backup_id in adapter.deleted
    assert adapter.manifest_key(result.backup_id) in adapter.objects


def test_new_backup_is_safety_floor_against_future_dated_owned_history(
    tmp_path,
    monkeypatch,
):
    from odooctl.services import backup as backup_svc

    ctx = _context(
        tmp_path,
        policy="required",
        retention={
            "daily": 0,
            "weekly": 0,
            "monthly": 0,
            "grace_hours": 1,
        },
    )
    future_dir, future_local = _local_backup(
        ctx,
        backup_id="production_2099-01-01_000000_future",
    )
    future_local = future_local.model_copy(
        update={"timestamp": "2099-01-01T00:00:00Z"}
    )
    (future_dir / "manifest.json").write_text(
        future_local.model_dump_json(indent=2)
    )
    MetadataStore(ctx.project.state_dir).save_backup_manifest(
        future_local.backup_id,
        future_local,
    )
    stale = datetime(2020, 1, 1, tzinfo=timezone.utc)
    os.utime(
        future_dir / "manifest.json",
        (stale.timestamp(), stale.timestamp()),
    )
    adapter = MemoryS3()
    future_remote = _seed_remote_marker(
        adapter,
        backup_id=future_local.backup_id,
        timestamp="2099-01-01T00:00:00Z",
    )
    for key in adapter.last_modified:
        adapter.last_modified[key] = stale
    _install_local_backup_fakes(monkeypatch)
    monkeypatch.setattr(
        remote_svc,
        "make_remote_adapter",
        lambda service_ctx: adapter,
    )

    result = backup_svc.run_backup(ctx, "production")

    new_dir = ctx.project.backups_dir / result.backup_id
    assert new_dir.is_dir()
    assert future_dir.is_dir()
    assert adapter.manifest_key(result.backup_id) in adapter.objects
    assert adapter.manifest_key(future_remote.backup_id) in adapter.objects
    verified = remote_svc.verify_remote_backup(
        ctx,
        "production",
        result.backup_id,
        adapter=adapter,
    )
    assert verified.backup_id == result.backup_id
    latest = MetadataStore(ctx.project.state_dir).latest_backup(
        "production"
    )
    assert latest is not None
    assert latest["backup_id"] == result.backup_id


def test_unverified_new_remote_copy_never_prunes_verified_history(
    tmp_path,
    monkeypatch,
):
    from odooctl.services import backup as backup_svc

    ctx = _context(
        tmp_path,
        policy="required",
        verify_after_upload=False,
        retention={"daily": 0, "weekly": 0, "monthly": 0},
    )
    adapter = MemoryS3()
    old = _seed_remote_marker(
        adapter,
        backup_id="production_2026-07-29_120000_old",
        timestamp="2026-07-29T12:00:00Z",
    )
    _install_local_backup_fakes(monkeypatch)
    monkeypatch.setattr(
        remote_svc,
        "make_remote_adapter",
        lambda service_ctx: adapter,
    )

    result = backup_svc.run_backup(ctx, "production")

    new_manifest = BackupManifest.model_validate_json(
        (ctx.project.backups_dir / result.backup_id / "manifest.json").read_text()
    )
    assert new_manifest.remote_status == "complete"
    assert new_manifest.remote_verified_at is None
    assert adapter.manifest_key(old.backup_id) in adapter.objects
    assert adapter.deleted == []
