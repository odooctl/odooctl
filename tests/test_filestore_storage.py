from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from odooctl.adapters.object_filestore import (
    ObjectFilestoreAdapter,
    ObjectFilestoreConflict,
)
from odooctl.adapters.s3 import S3IntegrityError
from odooctl.config import (
    FilestoreObjectStoreConfig,
    OdooCtlConfig,
)
from odooctl.context import ProjectContext
from odooctl.main import app
from odooctl.metadata.models import FilestoreMigrationManifest
from odooctl.metadata.store import MetadataStore
from odooctl.operations.models import OperationKind
from odooctl.services.context import ServiceContext
from odooctl.services.filestore_storage import (
    FilestoreStorageError,
    cutover_migration,
    delete_source_after_cutover,
    download_migration,
    plan_migration,
    scan_filestore,
    sync_migration,
    verify_migration,
)


class FakeNotFound(RuntimeError):
    response = {
        "Error": {"Code": "NoSuchKey"},
        "ResponseMetadata": {"HTTPStatusCode": 404},
    }


class FakeConditionalConflict(RuntimeError):
    response = {
        "Error": {"Code": "PreconditionFailed"},
        "ResponseMetadata": {"HTTPStatusCode": 412},
    }


class FakeS3:
    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.generation = 0

    def _store(self, key: str, body: bytes, metadata: dict) -> None:
        self.generation += 1
        self.objects[key] = {
            "body": body,
            "metadata": dict(metadata),
            "etag": f"etag-{self.generation}",
        }

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        extra = dict(ExtraArgs or {})
        self._store(
            key,
            Path(filename).read_bytes(),
            extra.get("Metadata", {}),
        )

    def put_object(
        self,
        *,
        Bucket,
        Key,
        Body,
        Metadata,
        IfNoneMatch=None,
        IfMatch=None,
        **kwargs,
    ):
        current = self.objects.get(Key)
        if IfNoneMatch == "*" and current is not None:
            raise FakeConditionalConflict(Key)
        if IfMatch is not None:
            if current is None or IfMatch != f'"{current["etag"]}"':
                raise FakeConditionalConflict(Key)
        payload = Body.read() if hasattr(Body, "read") else bytes(Body)
        self._store(Key, payload, Metadata)
        return {"ETag": f'"{self.objects[Key]["etag"]}"'}

    def head_object(self, *, Bucket, Key):
        try:
            item = self.objects[Key]
        except KeyError:
            raise FakeNotFound(Key) from None
        return {
            "ContentLength": len(item["body"]),
            "Metadata": dict(item["metadata"]),
            "ETag": f'"{item["etag"]}"',
        }

    def get_object(self, *, Bucket, Key):
        try:
            item = self.objects[Key]
        except KeyError:
            raise FakeNotFound(Key) from None
        return {"Body": io.BytesIO(item["body"])}

    def download_file(self, bucket, key, filename):
        Path(filename).write_bytes(self.objects[key]["body"])

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        return {
            "Contents": [
                {
                    "Key": key,
                    "Size": len(self.objects[key]["body"]),
                    "ETag": f'"{self.objects[key]["etag"]}"',
                }
                for key in keys
            ],
            "IsTruncated": False,
        }

    def delete_object(self, *, Bucket, Key):
        self.objects.pop(Key, None)


class FakeBoto3:
    def __init__(self):
        self.s3 = FakeS3()

    def client(self, service, **kwargs):
        assert service == "s3"
        return self.s3


def _config(
    tmp_path: Path,
    *,
    backend: dict | None = None,
) -> OdooCtlConfig:
    environment = {
        "branch": "main",
        "domain": "odoo.example.test",
        "db_name": "odoo_prod",
        "filestore_path": str(tmp_path / "source"),
    }
    if backend is not None:
        environment["filestore_backend"] = backend
    return OdooCtlConfig.model_validate(
        {
            "project": {"name": "demo", "odoo_version": "19.0"},
            "runtime": {"compose_file": "docker-compose.yml"},
            "postgres": {"password_env": "ODOO_DB_PASSWORD"},
            "odoo": {"image": "odoo:19.0"},
            "environments": {"production": environment},
        }
    )


def _context(
    tmp_path: Path,
    *,
    backend: dict,
) -> ServiceContext:
    config = _config(tmp_path, backend=backend)
    return ServiceContext(
        project=ProjectContext(
            root=tmp_path,
            config_path=tmp_path / "odooctl.yml",
            config=config,
        )
    )


def _write_source(root: Path) -> None:
    (root / "ab").mkdir(parents=True)
    (root / "ab" / "attachment").write_bytes(b"attachment")
    (root / "index").write_bytes(b"index")


def test_legacy_filestore_backend_contract_is_inferred(tmp_path):
    local = _config(tmp_path).env("production")
    assert local.effective_filestore_backend == "local"

    payload = _config(tmp_path).model_dump(mode="python")
    payload["environments"]["production"]["filestore_volume"] = "odoo-data"
    volume = OdooCtlConfig.model_validate(payload).env("production")
    assert volume.effective_filestore_backend == "docker_volume"


@pytest.mark.parametrize(
    ("backend", "message"),
    [
        ({"type": "object_mirror"}, "requires object_store"),
        ({"type": "posix_object_mount"}, "requires mount_path"),
        (
            {
                "type": "docker_volume",
            },
            "requires filestore_volume",
        ),
        (
            {
                "type": "odoo_module",
                "object_store": {"bucket": "filestore"},
            },
            "requires module_name",
        ),
        (
            {
                "type": "posix_object_mount",
                "mount_path": "/mnt/filestore",
                "object_store": {"bucket": "filestore"},
            },
            "does not accept object_store",
        ),
        (
            {
                "type": "object_mirror",
                "object_store": {"bucket": "filestore"},
                "mount_path": "/mnt/filestore",
            },
            "does not accept mount_path",
        ),
    ],
)
def test_filestore_backend_configuration_fails_closed(
    tmp_path,
    backend,
    message,
):
    with pytest.raises(ValueError, match=message):
        _config(tmp_path, backend=backend)


def test_filestore_object_store_secret_references_are_registered(
    tmp_path,
):
    config = _config(
        tmp_path,
        backend={
            "type": "object_mirror",
            "object_store": {
                "bucket": "filestore",
                "endpoint_env": "FILESTORE_ENDPOINT",
                "access_key_env": "FILESTORE_ACCESS",
                "secret_key_env": "FILESTORE_SECRET",
                "session_token_env": "FILESTORE_SESSION",
                "region_env": "FILESTORE_REGION",
                "encryption_algorithm": "aws:kms",
                "encryption_key_env": "FILESTORE_KMS",
            },
        },
    )

    refs = config.referenced_env_vars()
    for name in (
        "FILESTORE_ENDPOINT",
        "FILESTORE_ACCESS",
        "FILESTORE_SECRET",
        "FILESTORE_SESSION",
        "FILESTORE_REGION",
        "FILESTORE_KMS",
    ):
        assert name in refs


def test_scan_filestore_is_deterministic_and_rejects_symlinks(tmp_path):
    source = tmp_path / "source"
    _write_source(source)

    entries, inventory, total = scan_filestore(source)

    assert [entry.path for entry in entries] == ["ab/attachment", "index"]
    assert total == len(b"attachmentindex")
    assert len(inventory) == 64

    (source / "link").symlink_to(source / "index")
    with pytest.raises(FilestoreStorageError, match="symbolic-link"):
        scan_filestore(source)


def test_migration_plan_rejects_symlink_source_root(tmp_path):
    real_source = tmp_path / "real-source"
    _write_source(real_source)
    linked_source = tmp_path / "linked-source"
    linked_source.symlink_to(real_source, target_is_directory=True)
    ctx = _context(
        tmp_path,
        backend={
            "type": "posix_object_mount",
            "source_path": str(linked_source),
            "mount_path": str(tmp_path / "target"),
        },
    )

    with pytest.raises(FilestoreStorageError, match="real directory"):
        plan_migration(ctx, "production")


def test_filestore_manifest_binds_inventory_digest_to_entries(tmp_path):
    source = tmp_path / "source"
    _write_source(source)
    entries, _inventory, total = scan_filestore(source)

    with pytest.raises(
        ValueError,
        match="inventory_sha256 does not match",
    ):
        FilestoreMigrationManifest(
            migration_id="production_filestore_invalid",
            project="demo",
            environment="production",
            source_backend="local",
            target_backend="posix_object_mount",
            source_location=str(source),
            target_location=str(tmp_path / "target"),
            entries=entries,
            inventory_sha256="0" * 64,
            total_size=total,
        )


def test_posix_mount_migration_retains_source_until_separate_delete(
    tmp_path,
    monkeypatch,
):
    from odooctl.services import filestore_storage

    source = tmp_path / "source"
    target = tmp_path / "mounted-filestore"
    _write_source(source)
    ctx = _context(
        tmp_path,
        backend={
            "type": "posix_object_mount",
            "source_path": str(source),
            "mount_path": str(target),
        },
    )

    planned = plan_migration(ctx, "production")
    synced = sync_migration(ctx, "production", planned.migration_id)
    verified = verify_migration(ctx, "production", planned.migration_id)

    assert synced.status == "synced"
    assert verified.inventory_sha256 == planned.inventory_sha256
    assert (target / "ab" / "attachment").read_bytes() == b"attachment"
    with pytest.raises(FilestoreStorageError, match="exact environment"):
        cutover_migration(
            ctx,
            "production",
            planned.migration_id,
            confirm_environment="staging",
            confirm_source_retained=True,
        )
    cutover = cutover_migration(
        ctx,
        "production",
        planned.migration_id,
        confirm_environment="production",
        confirm_source_retained=True,
    )
    assert cutover.status == "cutover"
    assert source.exists()

    downloaded = download_migration(
        ctx,
        "production",
        planned.migration_id,
        tmp_path / "downloaded",
    )
    assert (downloaded / "index").read_bytes() == b"index"
    with pytest.raises(FilestoreStorageError, match="--delete-source"):
        delete_source_after_cutover(
            ctx,
            "production",
            planned.migration_id,
            confirm_environment="production",
            confirm_migration_id=planned.migration_id,
            delete_source=False,
        )
    real_rmtree = filestore_storage.shutil.rmtree

    def interrupted_delete(path, *args, **kwargs):
        if Path(path) == source:
            raise RuntimeError("simulated interruption after checkpoint")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(
        filestore_storage.shutil,
        "rmtree",
        interrupted_delete,
    )
    with pytest.raises(RuntimeError, match="simulated interruption"):
        delete_source_after_cutover(
            ctx,
            "production",
            planned.migration_id,
            confirm_environment="production",
            confirm_migration_id=planned.migration_id,
            delete_source=True,
        )
    checkpoint = MetadataStore(
        ctx.project.state_dir
    ).get_filestore_migration(planned.migration_id)
    assert checkpoint.source_delete_started_at is not None
    assert checkpoint.source_deleted is False
    monkeypatch.setattr(
        filestore_storage.shutil,
        "rmtree",
        real_rmtree,
    )
    deleted = delete_source_after_cutover(
        ctx,
        "production",
        planned.migration_id,
        confirm_environment="production",
        confirm_migration_id=planned.migration_id,
        delete_source=True,
    )
    assert deleted.source_deleted is True
    assert deleted.source_delete_started_at is not None
    assert not source.exists()
    assert target.exists()


@pytest.mark.parametrize("changed_field", ["source_path", "mount_path"])
def test_migration_rejects_source_or_target_config_drift(
    tmp_path,
    changed_field,
):
    source = tmp_path / "source"
    duplicate = tmp_path / "duplicate-source"
    target = tmp_path / "target"
    _write_source(source)
    _write_source(duplicate)
    ctx = _context(
        tmp_path,
        backend={
            "type": "posix_object_mount",
            "source_path": str(source),
            "mount_path": str(target),
        },
    )
    planned = plan_migration(ctx, "production")
    backend = ctx.project.config.env(
        "production"
    ).filestore_backend
    setattr(
        backend,
        changed_field,
        (
            str(duplicate)
            if changed_field == "source_path"
            else str(tmp_path / "different-target")
        ),
    )

    with pytest.raises(
        FilestoreStorageError,
        match="configuration changed",
    ):
        sync_migration(ctx, "production", planned.migration_id)


def test_object_adapter_upload_verify_download_and_active_cas(tmp_path):
    source = tmp_path / "source"
    _write_source(source)
    entries, inventory, total = scan_filestore(source)
    boto = FakeBoto3()
    adapter = ObjectFilestoreAdapter(
        FilestoreObjectStoreConfig(bucket="filestore", prefix="acme"),
        project="demo",
        environment="production",
        state_dir=tmp_path / ".odooctl",
        boto3_module=boto,
    )
    first = FilestoreMigrationManifest(
        migration_id="production_filestore_one",
        project="demo",
        environment="production",
        source_backend="local",
        target_backend="object_mirror",
        source_location=str(source),
        target_location=adapter.uri_for_migration(
            "production_filestore_one"
        ),
        entries=entries,
        inventory_sha256=inventory,
        total_size=total,
    )

    with pytest.raises(ObjectFilestoreConflict, match="scope"):
        adapter.upload_inventory(
            source,
            first.model_copy(update={"project": "other-project"}),
        )
    uploaded = adapter.upload_inventory(source, first)
    assert uploaded.object_count == 2
    assert adapter.verify_inventory(first).total_size == total
    conflicting_source = tmp_path / "conflicting-source"
    _write_source(conflicting_source)
    (conflicting_source / "new").write_bytes(b"new")
    conflicting_entries, conflicting_inventory, conflicting_total = (
        scan_filestore(conflicting_source)
    )
    conflicting = first.model_copy(
        update={
            "entries": conflicting_entries,
            "inventory_sha256": conflicting_inventory,
            "total_size": conflicting_total,
        }
    )
    with pytest.raises(ObjectFilestoreConflict, match="conflicts"):
        adapter.upload_inventory(conflicting_source, conflicting)
    first_key = adapter.object_key(entries[0].sha256)
    original_body = boto.s3.objects[first_key]["body"]
    boto.s3.objects[first_key]["body"] = b"x" * len(original_body)
    with pytest.raises(S3IntegrityError, match="checksum"):
        adapter.verify_inventory(first)
    boto.s3.objects[first_key]["body"] = original_body
    active = adapter.publish_active(
        first,
        expected_previous_migration_id=None,
    )
    assert active.migration_id == first.migration_id
    assert adapter.publish_active(
        first,
        expected_previous_migration_id=None,
    ) == active

    downloaded = adapter.download_inventory(first, tmp_path / "downloaded")
    assert (downloaded / "ab" / "attachment").read_bytes() == b"attachment"
    with pytest.raises(ObjectFilestoreConflict, match="active"):
        adapter.delete_migration_manifest(
            first.migration_id,
            confirm_not_active=True,
        )

    second = first.model_copy(
        update={
            "migration_id": "production_filestore_two",
            "target_location": adapter.uri_for_migration(
                "production_filestore_two"
            ),
            "previous_active_migration_id": first.migration_id,
        }
    )
    adapter.upload_inventory(source, second)
    with pytest.raises(ObjectFilestoreConflict, match="changed"):
        adapter.publish_active(
            second,
            expected_previous_migration_id=None,
        )
    next_active = adapter.publish_active(
        second,
        expected_previous_migration_id=first.migration_id,
    )
    assert next_active.migration_id == second.migration_id
    assert adapter.delete_migration_manifest(
        first.migration_id,
        confirm_not_active=True,
    ).endswith("/manifest.json")
    assert adapter.object_key(entries[0].sha256) in boto.s3.objects


def test_object_mirror_service_flow_keeps_source_until_explicit_cleanup(
    tmp_path,
    monkeypatch,
):
    from odooctl.services import filestore_storage

    source = tmp_path / "source"
    _write_source(source)
    ctx = _context(
        tmp_path,
        backend={
            "type": "object_mirror",
            "object_store": {
                "bucket": "filestore",
                "prefix": "acme",
            },
        },
    )
    adapter = ObjectFilestoreAdapter(
        ctx.project.config.env(
            "production"
        ).filestore_backend.object_store,
        project="demo",
        environment="production",
        state_dir=ctx.project.state_dir,
        boto3_module=FakeBoto3(),
    )
    monkeypatch.setattr(
        filestore_storage,
        "_object_adapter",
        lambda *args, **kwargs: adapter,
    )

    planned = plan_migration(ctx, "production")
    sync_migration(ctx, "production", planned.migration_id)
    verify_migration(ctx, "production", planned.migration_id)
    cutover = cutover_migration(
        ctx,
        "production",
        planned.migration_id,
        confirm_environment="production",
        confirm_source_retained=True,
    )

    assert cutover.status == "cutover"
    assert source.exists()
    assert adapter.read_active().migration_id == planned.migration_id
    with pytest.raises(
        FilestoreStorageError,
        match="retained copy",
    ):
        delete_source_after_cutover(
            ctx,
            "production",
            planned.migration_id,
            confirm_environment="production",
            confirm_migration_id=planned.migration_id,
            delete_source=True,
        )
    destination = download_migration(
        ctx,
        "production",
        planned.migration_id,
        tmp_path / "object-download",
    )
    assert hashlib.sha256(
        (destination / "index").read_bytes()
    ).hexdigest() == next(
        entry.sha256 for entry in planned.entries if entry.path == "index"
    )


def test_odoo_module_mode_records_operator_selected_integration(
    tmp_path,
    monkeypatch,
):
    from odooctl.services import filestore_storage

    source = tmp_path / "source"
    _write_source(source)
    ctx = _context(
        tmp_path,
        backend={
            "type": "odoo_module",
            "module_name": "community_object_storage",
            "object_store": {
                "bucket": "filestore",
                "prefix": "acme",
            },
        },
    )
    adapter = ObjectFilestoreAdapter(
        ctx.project.config.env(
            "production"
        ).filestore_backend.object_store,
        project="demo",
        environment="production",
        state_dir=ctx.project.state_dir,
        boto3_module=FakeBoto3(),
    )
    monkeypatch.setattr(
        filestore_storage,
        "_object_adapter",
        lambda *args, **kwargs: adapter,
    )

    planned = plan_migration(ctx, "production")
    synced = sync_migration(ctx, "production", planned.migration_id)
    verified = verify_migration(
        ctx,
        "production",
        planned.migration_id,
    )

    assert planned.target_backend == "odoo_module"
    assert planned.module_name == "community_object_storage"
    assert synced.status == "synced"
    assert verified.inventory_sha256 == planned.inventory_sha256


def test_docker_volume_source_materializes_for_object_mirror(
    tmp_path,
    monkeypatch,
):
    from odooctl.services import filestore_storage

    source = tmp_path / "source"
    _write_source(source)
    config = _config(
        tmp_path,
        backend={
            "type": "object_mirror",
            "object_store": {"bucket": "filestore"},
        },
    )
    payload = config.model_dump(mode="python")
    payload["environments"]["production"]["filestore_volume"] = "odoo-data"
    ctx = ServiceContext(
        project=ProjectContext(
            root=tmp_path,
            config_path=tmp_path / "odooctl.yml",
            config=OdooCtlConfig.model_validate(payload),
        )
    )
    object_adapter = ObjectFilestoreAdapter(
        ctx.project.config.env(
            "production"
        ).filestore_backend.object_store,
        project="demo",
        environment="production",
        state_dir=ctx.project.state_dir,
        boto3_module=FakeBoto3(),
    )

    class FakeVolume:
        def archive(self, filestore_path, output):
            with tarfile.open(output, "w") as archive:
                archive.add(
                    source,
                    arcname=Path(filestore_path).name,
                )

    monkeypatch.setattr(
        filestore_storage,
        "make_filestore_adapter",
        lambda *args, **kwargs: FakeVolume(),
    )
    monkeypatch.setattr(
        filestore_storage,
        "_object_adapter",
        lambda *args, **kwargs: object_adapter,
    )

    planned = plan_migration(ctx, "production")
    synced = sync_migration(
        ctx,
        "production",
        planned.migration_id,
    )

    assert planned.source_backend == "docker_volume"
    assert synced.status == "synced"
    verified = object_adapter.verify_inventory(synced)
    assert verified.object_count == len(planned.entries)
    assert verified.total_size == planned.total_size


def test_filestore_cli_plan_records_durable_operation(tmp_path):
    source = tmp_path / "source"
    _write_source(source)
    target = tmp_path / "mounted-filestore"
    config = tmp_path / "odooctl.yml"
    config.write_text(
        f"""\
project:
  name: demo
  odoo_version: "19.0"
runtime:
  compose_file: docker-compose.yml
postgres:
  password_env: ODOO_DB_PASSWORD
odoo:
  image: odoo:19.0
environments:
  production:
    branch: main
    domain: odoo.example.test
    db_name: odoo_prod
    filestore_path: {source}
    filestore_backend:
      type: posix_object_mount
      mount_path: {target}
"""
    )

    result = CliRunner().invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "filestore",
            "migrate",
            "plan",
            "production",
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 0, result.output
    operation_files = list(
        (tmp_path / ".odooctl" / "operations").glob(
            "*/operation.json"
        )
    )
    assert len(operation_files) == 1
    assert (
        '"kind": "filestore_migrate"'
        in operation_files[0].read_text()
    )
    assert OperationKind.FILESTORE_MIGRATE.value == "filestore_migrate"


def test_filestore_cli_cutover_forwards_typed_confirmation(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "target"
    config = tmp_path / "odooctl.yml"
    config.write_text(
        f"""\
project:
  name: demo
  odoo_version: "19.0"
runtime:
  compose_file: docker-compose.yml
postgres:
  password_env: ODOO_DB_PASSWORD
odoo:
  image: odoo:19.0
environments:
  production:
    branch: main
    domain: odoo.example.test
    db_name: odoo_prod
    filestore_path: {source}
    filestore_backend:
      type: posix_object_mount
      mount_path: {target}
"""
    )
    observed = {}

    def fake_cutover(ctx, environment, migration_id, **kwargs):
        observed.update(
            environment=environment,
            migration_id=migration_id,
            **kwargs,
        )
        return SimpleNamespace(
            migration_id=migration_id,
            source_location=str(source),
            source_deleted=False,
        )

    monkeypatch.setattr(
        "odooctl.services.filestore_storage.cutover_migration",
        fake_cutover,
    )
    result = CliRunner().invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "filestore",
            "migrate",
            "cutover",
            "production",
            "--migration",
            "production_filestore_123",
            "--confirm-environment",
            "production",
            "--confirm-source-retained",
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 0, result.output
    assert observed == {
        "environment": "production",
        "migration_id": "production_filestore_123",
        "confirm_environment": "production",
        "confirm_source_retained": True,
    }
