from __future__ import annotations

import hashlib
import io
import json
import os
import traceback
from pathlib import Path

import pytest

from odooctl.adapters.s3 import (
    COMPLETION_MARKER,
    S3Adapter,
    S3ConfigurationError,
    S3DependencyError,
    S3IntegrityError,
    S3PathError,
    S3ProviderError,
)
from odooctl.commands.backup import prune_backups
from odooctl.config import RemoteBackupConfig


class FakeS3NotFoundError(RuntimeError):
    response = {
        "Error": {"Code": "NoSuchKey"},
        "ResponseMetadata": {"HTTPStatusCode": 404},
    }


def test_prune_backups_keeps_most_recent(tmp_path):
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    for index in range(4):
        path = backup_root / f"backup_{index}"
        path.mkdir()
        (path / "marker.txt").write_text(str(index))
        os.utime(path, (100 + index, 100 + index))

    removed = prune_backups(backup_root, keep=2)

    assert {p.name for p in removed} == {"backup_0", "backup_1"}
    assert sorted(p.name for p in backup_root.iterdir()) == ["backup_2", "backup_3"]


def test_prune_backups_keeps_most_recent_per_environment(tmp_path):
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    for index, name in enumerate(["production_1", "production_2", "staging_1", "staging_2"]):
        path = backup_root / name
        path.mkdir()
        os.utime(path, (100 + index, 100 + index))

    removed = prune_backups(backup_root, keep=1, environment="production")

    assert {p.name for p in removed} == {"production_1"}
    assert sorted(p.name for p in backup_root.iterdir()) == [
        "production_2",
        "staging_1",
        "staging_2",
    ]


def test_prune_backups_removes_only_backups_older_than_days(tmp_path):
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    old = backup_root / "production_old"
    new = backup_root / "production_new"
    old.mkdir()
    new.mkdir()
    os.utime(old, (100, 100))
    os.utime(new, (200 + 86400 * 2, 200 + 86400 * 2))

    removed = prune_backups(backup_root, keep=99, newer_than_days=1, now=200 + 86400 * 2)

    assert [p.name for p in removed] == ["production_old"]
    assert [p.name for p in backup_root.iterdir()] == ["production_new"]


class FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.uploads = []
        self.calls = []
        self.failures = {}
        self.page_size = None
        self.listed_keys = None

    def seed(self, key, body, *, checksum="auto"):
        body_bytes = body if isinstance(body, bytes) else body.encode()
        if checksum == "auto":
            checksum = hashlib.sha256(body_bytes).hexdigest()
        metadata = {} if checksum is None else {"sha256": checksum}
        self.objects[key] = {"body": body_bytes, "metadata": metadata}

    def _fail(self, operation):
        if operation in self.failures:
            raise self.failures[operation]

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        self._fail("upload_file")
        body = Path(filename).read_bytes()
        extra_args = dict(ExtraArgs or {})
        self.objects[key] = {
            "body": body,
            "metadata": dict(extra_args.get("Metadata", {})),
        }
        self.uploads.append(
            {
                "filename": filename,
                "bucket": bucket,
                "key": key,
                "extra_args": extra_args,
            }
        )
        self.calls.append(("upload", key))

    def head_object(self, *, Bucket, Key):
        self._fail("head_object")
        self.calls.append(("head", Key))
        try:
            stored = self.objects[Key]
        except KeyError:
            raise FakeS3NotFoundError(Key) from None
        return {
            "ContentLength": len(stored["body"]),
            "Metadata": dict(stored["metadata"]),
            "ETag": f'"etag-{Key}"',
        }

    def get_object(self, *, Bucket, Key):
        self._fail("get_object")
        self.calls.append(("get", Key))
        return {"Body": io.BytesIO(self.objects[Key]["body"])}

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None):
        self._fail("list_objects_v2")
        self.calls.append(("list", Prefix))
        if self.listed_keys is None:
            keys = sorted(key for key in self.objects if key.startswith(Prefix))
        else:
            keys = list(self.listed_keys)
        offset = int(ContinuationToken or 0)
        limit = self.page_size or len(keys) or 1
        page = keys[offset : offset + limit]
        next_offset = offset + len(page)
        response = {
            "Contents": [
                {
                    "Key": key,
                    "Size": len(self.objects.get(key, {"body": b""})["body"]),
                    "ETag": f'"etag-{key}"',
                }
                for key in page
            ],
            "IsTruncated": next_offset < len(keys),
        }
        if response["IsTruncated"]:
            response["NextContinuationToken"] = str(next_offset)
        return response

    def download_file(self, bucket, key, filename):
        self._fail("download_file")
        self.calls.append(("download", key))
        Path(filename).write_bytes(self.objects[key]["body"])

    def delete_object(self, *, Bucket, Key):
        self._fail("delete_object")
        self.calls.append(("delete", Key))
        self.objects.pop(Key, None)


class FakeBoto3:
    def __init__(self, *, client_error=None):
        self.client_kwargs = None
        self.s3 = FakeS3Client()
        self.client_error = client_error

    def client(self, service, **kwargs):
        assert service == "s3"
        self.client_kwargs = kwargs
        if self.client_error is not None:
            raise self.client_error
        return self.s3


def remote_manifest(
    backup_name="backup_2026",
    *,
    db_body=b"dump",
    filestore_body=b"filestore",
    remote_status=None,
    status="complete",
):
    manifest = {
        "backup_id": backup_name,
        "project": "sample-project",
        "environment": "production",
        "artifact_paths": ["db.dump", "filestore.tar"],
        "checksums": {
            "db_dump": hashlib.sha256(db_body).hexdigest(),
            "filestore": hashlib.sha256(filestore_body).hexdigest(),
        },
        "status": status,
    }
    if remote_status is not None:
        manifest["remote_status"] = remote_status
    return json.dumps(manifest)


def make_backup(
    tmp_path,
    name="backup_2026",
    *,
    remote_status="complete",
):
    backup_dir = tmp_path / name
    backup_dir.mkdir()
    (backup_dir / "db.dump").write_text("dump")
    (backup_dir / "filestore.tar").write_bytes(b"filestore")
    (backup_dir / COMPLETION_MARKER).write_text(remote_manifest(name, remote_status=remote_status))
    return backup_dir


def make_adapter(fake, tmp_path, *, config=None):
    return S3Adapter(
        config or RemoteBackupConfig(bucket="bucket-name", prefix="odoo/prod"),
        root=tmp_path / "unused-local-mirror",
        boto3_module=fake,
    )


def test_s3_adapter_uploads_manifest_last_with_sha256_metadata(
    tmp_path,
    monkeypatch,
):
    backup_dir = make_backup(tmp_path)
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    fake = FakeBoto3()
    adapter = make_adapter(fake, tmp_path)

    uploaded = adapter.upload_backup(backup_dir)

    assert uploaded == "s3://bucket-name/odoo/prod/backup_2026"
    assert fake.client_kwargs == {"region_name": "eu-central-1"}
    assert [item["key"] for item in fake.s3.uploads] == [
        "odoo/prod/backup_2026/db.dump",
        "odoo/prod/backup_2026/filestore.tar",
        "odoo/prod/backup_2026/manifest.json",
    ]
    for item in fake.s3.uploads:
        expected = hashlib.sha256(Path(item["filename"]).read_bytes()).hexdigest()
        assert item["extra_args"]["Metadata"] == {"sha256": expected}
    assert fake.s3.calls.index(
        ("delete", "odoo/prod/backup_2026/manifest.json")
    ) < fake.s3.calls.index(("upload", "odoo/prod/backup_2026/filestore.tar"))


def test_s3_abandonment_fence_uses_direct_head_and_blocks_publication(
    tmp_path,
):
    backup_dir = make_backup(tmp_path)
    fake = FakeBoto3()
    adapter = make_adapter(fake, tmp_path)
    fence_key = adapter.abandonment_key(backup_dir.name)
    fake.s3.seed(fence_key, b'{"status":"abandoned"}')

    with pytest.raises(S3IntegrityError, match="cannot be republished"):
        adapter.upload_backup(backup_dir)

    assert fake.s3.calls == [("head", fence_key)]
    assert fake.s3.uploads == []
    assert fence_key in fake.s3.objects


def test_s3_fence_hides_late_completion_marker_from_all_backup_reads(
    tmp_path,
):
    fake = FakeBoto3()
    adapter = make_adapter(fake, tmp_path)
    backup_name = "backup_2026"
    fence_key = adapter.abandonment_key(backup_name)
    marker_key = adapter.manifest_key(backup_name)
    db_key = "odoo/prod/backup_2026/db.dump"
    filestore_key = "odoo/prod/backup_2026/filestore.tar"
    fake.s3.seed(fence_key, b'{"status":"abandoned"}')
    fake.s3.seed(marker_key, remote_manifest())
    fake.s3.seed(db_key, b"dump")
    fake.s3.seed(filestore_key, b"filestore")
    # Simulate an inventory response that sees the late completion marker but
    # not the fence. Authority must come from direct HEAD, never this LIST.
    fake.s3.listed_keys = [db_key, filestore_key, marker_key]

    assert adapter.list_backups() == []
    assert ("head", fence_key) in fake.s3.calls
    with pytest.raises(S3IntegrityError, match="cannot be republished"):
        adapter.verify_backup(backup_name)
    destination = tmp_path / "fenced-download"
    with pytest.raises(S3IntegrityError, match="cannot be republished"):
        adapter.download_backup(backup_name, destination)
    assert not destination.exists()


def test_s3_payload_removes_stale_marker_then_accepts_staged_final_manifest(
    tmp_path,
):
    backup_dir = make_backup(tmp_path, remote_status="pending")
    fake = FakeBoto3()
    marker_key = "odoo/prod/backup_2026/manifest.json"
    fake.s3.seed(marker_key, '{"remote_status":"old"}')
    adapter = make_adapter(fake, tmp_path)

    payload = adapter.upload_backup_payload(backup_dir)

    assert marker_key not in fake.s3.objects
    assert marker_key not in {item.key for item in payload}
    first_upload = next(index for index, call in enumerate(fake.s3.calls) if call[0] == "upload")
    assert fake.s3.calls.index(("delete", marker_key)) < first_upload

    staged = tmp_path / "final-manifest.tmp"
    staged.write_text(remote_manifest(remote_status="complete"))
    uploaded = adapter.upload_manifest(staged, backup_name="backup_2026")

    assert uploaded.key == marker_key
    assert json.loads(fake.s3.objects[marker_key]["body"])["remote_status"] == "complete"
    assert json.loads((backup_dir / COMPLETION_MARKER).read_text())["remote_status"] == "pending"
    assert fake.s3.calls[-2:] == [("upload", marker_key), ("head", marker_key)]


@pytest.mark.parametrize(
    ("manifest_text", "error"),
    [
        (
            remote_manifest(remote_status="pending"),
            "remote status is not complete",
        ),
        (
            remote_manifest(remote_status="failed"),
            "remote status is not complete",
        ),
        (
            remote_manifest(remote_status="complete", status="failed"),
            "status is not complete",
        ),
        (
            remote_manifest(
                "another-backup",
                remote_status="complete",
            ),
            "identity does not match",
        ),
        (
            remote_manifest(
                db_body=b"different",
                remote_status="complete",
            ),
            "checksum mismatch",
        ),
        ("{not-json", "not valid JSON"),
    ],
)
def test_s3_direct_upload_rejects_invalid_manifest_without_remote_mutation(
    tmp_path,
    manifest_text,
    error,
):
    backup_dir = make_backup(tmp_path)
    (backup_dir / COMPLETION_MARKER).write_text(manifest_text)
    fake = FakeBoto3()
    marker_key = "odoo/prod/backup_2026/manifest.json"
    db_key = "odoo/prod/backup_2026/db.dump"
    filestore_key = "odoo/prod/backup_2026/filestore.tar"
    fake.s3.seed(db_key, b"dump")
    fake.s3.seed(filestore_key, b"filestore")
    fake.s3.seed(marker_key, remote_manifest())
    original_objects = {
        key: (stored["body"], dict(stored["metadata"])) for key, stored in fake.s3.objects.items()
    }
    adapter = make_adapter(fake, tmp_path)

    with pytest.raises(S3IntegrityError, match=error):
        adapter.upload_backup(backup_dir)

    assert fake.s3.calls == []
    assert fake.s3.uploads == []
    assert {
        key: (stored["body"], dict(stored["metadata"])) for key, stored in fake.s3.objects.items()
    } == original_objects
    assert {item.key for item in adapter.verify_backup("backup_2026")} == {
        db_key,
        filestore_key,
        marker_key,
    }


def test_s3_direct_directory_manifest_rejection_removes_existing_marker(
    tmp_path,
):
    backup_dir = make_backup(tmp_path, remote_status="pending")
    fake = FakeBoto3()
    marker_key = "odoo/prod/backup_2026/manifest.json"
    fake.s3.seed(marker_key, remote_manifest())
    adapter = make_adapter(fake, tmp_path)

    with pytest.raises(S3IntegrityError, match="remote status is not complete"):
        adapter.upload_manifest(backup_dir)

    assert marker_key not in fake.s3.objects


def test_s3_direct_upload_accepts_valid_pre_r3_manifest(tmp_path):
    backup_dir = make_backup(tmp_path)
    (backup_dir / COMPLETION_MARKER).write_text(remote_manifest())
    fake = FakeBoto3()
    adapter = make_adapter(fake, tmp_path)

    uploaded = adapter.upload_backup(backup_dir)

    assert uploaded == "s3://bucket-name/odoo/prod/backup_2026"
    marker = fake.s3.objects[adapter.manifest_key("backup_2026")]
    assert "remote_status" not in json.loads(marker["body"])


def test_s3_adapter_passes_credentials_endpoint_and_encryption(
    tmp_path,
    monkeypatch,
):
    backup_dir = make_backup(tmp_path)
    monkeypatch.setenv("S3_ENDPOINT", "https://objects.example.test")
    monkeypatch.setenv("S3_ACCESS", "access-value")
    monkeypatch.setenv("S3_SECRET", "secret-value")
    monkeypatch.setenv("S3_REGION", "eu-central-2")
    monkeypatch.setenv("S3_KMS", "kms-key-id")
    fake = FakeBoto3()
    adapter = make_adapter(
        fake,
        tmp_path,
        config=RemoteBackupConfig(
            bucket="bucket-name",
            endpoint_env="S3_ENDPOINT",
            access_key_env="S3_ACCESS",
            secret_key_env="S3_SECRET",
            region_env="S3_REGION",
            encryption_algorithm="aws:kms",
            encryption_key_env="S3_KMS",
        ),
    )

    adapter.upload_backup(backup_dir)

    assert fake.client_kwargs == {
        "aws_access_key_id": "access-value",
        "aws_secret_access_key": "secret-value",
        "region_name": "eu-central-2",
        "endpoint_url": "https://objects.example.test",
    }
    for item in fake.s3.uploads:
        assert item["extra_args"]["ServerSideEncryption"] == "aws:kms"
        assert item["extra_args"]["SSEKMSKeyId"] == "kms-key-id"
        assert len(item["extra_args"]["Metadata"]["sha256"]) == 64


def test_s3_adapter_has_no_local_fallback_when_dependency_is_missing(
    tmp_path,
    monkeypatch,
):
    backup_dir = make_backup(tmp_path)
    adapter = S3Adapter(
        RemoteBackupConfig(bucket="bucket-name"),
        root=tmp_path / "must-not-exist",
    )

    def missing_boto3():
        raise S3DependencyError("boto3 is unavailable")

    monkeypatch.setattr(adapter, "_load_boto3", missing_boto3)

    with pytest.raises(S3DependencyError, match="boto3 is unavailable"):
        adapter.upload_backup(backup_dir)

    assert not adapter.root.exists()


def test_s3_adapter_provider_errors_do_not_fall_back_locally(tmp_path):
    backup_dir = make_backup(tmp_path)
    fake = FakeBoto3()
    fake.s3.failures["upload_file"] = RuntimeError("provider unavailable")
    adapter = make_adapter(fake, tmp_path)

    with pytest.raises(S3ProviderError, match="provider unavailable"):
        adapter.upload_backup(backup_dir)

    assert not adapter.root.exists()
    assert adapter.manifest_key("backup_2026") not in fake.s3.objects


@pytest.mark.parametrize("failure_phase", ["client", "provider"])
def test_s3_provider_errors_redact_all_configured_environment_values(
    tmp_path,
    monkeypatch,
    failure_phase,
):
    values = {
        "S3_ENDPOINT": "https://user:pass@objects.example.test",
        "S3_ACCESS": "access-value",
        "S3_SECRET": "secret-value",
        "S3_REGION": "private-region",
        "S3_KMS": "kms-key-value",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    leaked = " ".join(values.values())
    error = RuntimeError(f"provider echoed {leaked}")
    fake = FakeBoto3(client_error=error if failure_phase == "client" else None)
    if failure_phase == "provider":
        fake.s3.failures["list_objects_v2"] = error
    adapter = make_adapter(
        fake,
        tmp_path,
        config=RemoteBackupConfig(
            bucket="bucket-name",
            endpoint_env="S3_ENDPOINT",
            access_key_env="S3_ACCESS",
            secret_key_env="S3_SECRET",
            region_env="S3_REGION",
            encryption_algorithm="aws:kms",
            encryption_key_env="S3_KMS",
        ),
    )

    with pytest.raises(S3ProviderError) as captured:
        adapter.list_backups()

    message = str(captured.value)
    formatted_traceback = "".join(traceback.format_exception(captured.value))
    assert "***" in message
    assert all(value not in message for value in values.values())
    assert all(value not in formatted_traceback for value in values.values())


@pytest.mark.parametrize("failure_phase", ["client", "provider"])
def test_s3_provider_errors_redact_standard_aws_chain_credentials(
    tmp_path,
    monkeypatch,
    failure_phase,
):
    token_file_secret = "container-auth-token-from-file"
    token_file = tmp_path / "container-authorization-token"
    token_file.write_text(token_file_secret + "\n")
    values = {
        "AWS_ACCESS_KEY_ID": "standard-access-identifier",
        "AWS_SECRET_ACCESS_KEY": "standard-secret-credential",
        "AWS_SESSION_TOKEN": "standard-session-token",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN": "container-auth-token",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI": (
            "https://user:password@credentials.example.test/"
            "?signature=signed-container-query"
        ),
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE": str(token_file),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    secret_values = (*values.values(), token_file_secret)
    error = RuntimeError(
        "provider echoed " + " ".join(secret_values)
    )
    fake = FakeBoto3(
        client_error=error if failure_phase == "client" else None
    )
    if failure_phase == "provider":
        fake.s3.failures["list_objects_v2"] = error
    adapter = make_adapter(
        fake,
        tmp_path,
        config=RemoteBackupConfig(bucket="bucket-name"),
    )

    with pytest.raises(S3ProviderError) as captured:
        adapter.list_backups()

    rendered = "".join(traceback.format_exception(captured.value))
    assert "***" in rendered
    assert all(value not in rendered for value in secret_values)


def test_s3_default_chain_rejects_non_regular_container_token_file(
    tmp_path,
    monkeypatch,
):
    token_directory = tmp_path / "container-token-directory"
    token_directory.mkdir()
    monkeypatch.setenv(
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
        str(token_directory),
    )
    adapter = make_adapter(
        FakeBoto3(),
        tmp_path,
        config=RemoteBackupConfig(bucket="bucket-name"),
    )

    with pytest.raises(
        S3ConfigurationError,
        match="could not be securely read",
    ):
        adapter.list_backups()


@pytest.mark.parametrize("failure_phase", ["client", "provider"])
def test_s3_redaction_keeps_container_token_loaded_before_file_rotation(
    tmp_path,
    monkeypatch,
    failure_phase,
):
    original_secret = "container-token-loaded-by-provider"
    token_file = tmp_path / "rotating-container-token"
    token_file.write_text(original_secret)
    monkeypatch.setenv(
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
        str(token_file),
    )
    leaked = f"provider echoed {original_secret} from {token_file}"
    fake = FakeBoto3()
    adapter = make_adapter(
        fake,
        tmp_path,
        config=RemoteBackupConfig(bucket="bucket-name"),
    )

    if failure_phase == "client":
        def remove_token_then_fail(service, **kwargs):
            token_file.unlink()
            raise RuntimeError(leaked)

        fake.client = remove_token_then_fail
    else:
        adapter._client()
        token_file.write_text("rotated-container-token")
        fake.s3.failures["list_objects_v2"] = RuntimeError(leaked)

    with pytest.raises(S3ProviderError) as captured:
        adapter.list_backups()

    rendered = "".join(traceback.format_exception(captured.value))
    assert "***" in rendered
    assert original_secret not in rendered
    assert str(token_file) not in rendered


def test_s3_head_and_content_verification_detect_checksum_mismatches(tmp_path):
    fake = FakeBoto3()
    key = "odoo/prod/backup_2026/db.dump"
    fake.s3.seed(key, b"bad-bytes!", checksum=hashlib.sha256(b"good-data!").hexdigest())
    adapter = make_adapter(fake, tmp_path)

    info = adapter.head_object(key)

    assert info.key == key
    assert info.size == len(b"bad-bytes!")
    with pytest.raises(S3IntegrityError, match="metadata mismatch"):
        adapter.verify_object(key, expected_sha256="0" * 64)
    with pytest.raises(S3IntegrityError, match="content checksum mismatch"):
        adapter.verify_object_content(key)

    fake.s3.objects[key]["metadata"] = {}
    with pytest.raises(S3IntegrityError, match="no valid SHA-256 metadata"):
        adapter.verify_object(key)


def test_s3_verify_backup_requires_manifest_artifacts(tmp_path):
    fake = FakeBoto3()
    marker_key = "odoo/prod/backup_2026/manifest.json"
    fake.s3.seed(marker_key, remote_manifest())
    adapter = make_adapter(fake, tmp_path)

    with pytest.raises(S3IntegrityError, match="missing required artifacts"):
        adapter.verify_backup("backup_2026")


def test_s3_manifest_checksums_are_authoritative_over_object_metadata(
    tmp_path,
):
    fake = FakeBoto3()
    fake.s3.seed(
        "odoo/prod/backup_2026/manifest.json",
        remote_manifest(),
    )
    # The object and its custom metadata were changed together. Its manifest
    # checksum remains the independent source of truth.
    fake.s3.seed("odoo/prod/backup_2026/db.dump", b"tampered")
    fake.s3.seed("odoo/prod/backup_2026/filestore.tar", b"filestore")
    adapter = make_adapter(fake, tmp_path)

    with pytest.raises(S3IntegrityError, match="checksum metadata mismatch"):
        adapter.verify_backup("backup_2026")


def test_s3_legacy_objects_without_metadata_use_manifest_checksums(tmp_path):
    fake = FakeBoto3()
    fake.s3.seed(
        "odoo/prod/backup_2026/manifest.json",
        remote_manifest(),
        checksum=None,
    )
    fake.s3.seed(
        "odoo/prod/backup_2026/db.dump",
        b"dump",
        checksum=None,
    )
    fake.s3.seed(
        "odoo/prod/backup_2026/filestore.tar",
        b"filestore",
        checksum=None,
    )
    adapter = make_adapter(fake, tmp_path)

    verified = adapter.verify_backup("backup_2026")
    restored = adapter.download_backup("backup_2026", tmp_path / "legacy")

    assert {item.key for item in verified} == set(fake.s3.objects)
    assert all(item.sha256 is not None for item in verified)
    assert (restored / "db.dump").read_bytes() == b"dump"
    assert (restored / "filestore.tar").read_bytes() == b"filestore"


def test_s3_lists_only_completed_backups_across_pages(tmp_path):
    fake = FakeBoto3()
    fake.s3.page_size = 1
    for key in (
        "odoo/prod/backup-a/db.dump",
        "odoo/prod/backup-a/manifest.json",
        "odoo/prod/backup-a-copy/manifest.json",
        "odoo/prod/incomplete/db.dump",
        "odoo/prod/nested-marker/nested/manifest.json",
    ):
        fake.s3.seed(key, key)
    adapter = make_adapter(fake, tmp_path)

    assert adapter.list_backups() == ["backup-a", "backup-a-copy"]
    assert [item.key for item in adapter.list_objects("backup-a")] == [
        "odoo/prod/backup-a/db.dump",
        "odoo/prod/backup-a/manifest.json",
    ]


def test_s3_download_manifest_and_backup_verify_actual_bytes(tmp_path):
    fake = FakeBoto3()
    marker_key = "odoo/prod/backup_2026/manifest.json"
    fake.s3.seed(marker_key, remote_manifest())
    fake.s3.seed("odoo/prod/backup_2026/db.dump", "dump")
    fake.s3.seed("odoo/prod/backup_2026/filestore.tar", "filestore")
    adapter = make_adapter(fake, tmp_path)

    manifest_path = tmp_path / "downloaded-manifest.json"
    manifest_info = adapter.download_manifest("backup_2026", manifest_path)
    target = adapter.download_backup("backup_2026", tmp_path / "restored")

    assert manifest_info.key == marker_key
    assert json.loads(manifest_path.read_text())["backup_id"] == "backup_2026"
    assert json.loads((target / "manifest.json").read_text())["backup_id"] == "backup_2026"
    assert (target / "db.dump").read_text() == "dump"
    assert (target / "filestore.tar").read_text() == "filestore"


def test_s3_download_fsyncs_tree_before_atomic_publish(
    tmp_path,
    monkeypatch,
):
    from odooctl.adapters import s3 as s3_module

    fake = FakeBoto3()
    fake.s3.seed(
        "odoo/prod/backup_2026/manifest.json",
        remote_manifest(),
    )
    fake.s3.seed("odoo/prod/backup_2026/db.dump", "dump")
    fake.s3.seed(
        "odoo/prod/backup_2026/filestore.tar",
        "filestore",
    )
    adapter = make_adapter(fake, tmp_path)
    events: list[str] = []
    original_fsync_tree = s3_module._fsync_tree
    original_replace = s3_module.os.replace

    def observed_fsync_tree(path):
        events.append("fsync-tree")
        return original_fsync_tree(path)

    def observed_replace(source, destination):
        if Path(source).is_dir():
            events.append("publish-directory")
        return original_replace(source, destination)

    monkeypatch.setattr(
        s3_module,
        "_fsync_tree",
        observed_fsync_tree,
    )
    monkeypatch.setattr(
        s3_module.os,
        "replace",
        observed_replace,
    )

    adapter.download_backup(
        "backup_2026",
        tmp_path / "durable-restore",
    )

    assert events == ["fsync-tree", "publish-directory"]


def test_s3_failed_download_leaves_no_published_or_partial_backup(tmp_path):
    fake = FakeBoto3()
    marker_key = "odoo/prod/backup_2026/manifest.json"
    payload_key = "odoo/prod/backup_2026/db.dump"
    fake.s3.seed(marker_key, remote_manifest())
    # Tampered bytes have matching custom metadata, but not the independent
    # checksum in the manifest.
    fake.s3.seed(payload_key, b"corrupt!")
    fake.s3.seed("odoo/prod/backup_2026/filestore.tar", b"filestore")
    adapter = make_adapter(fake, tmp_path)
    destination = tmp_path / "restore"

    with pytest.raises(S3IntegrityError, match="checksum"):
        adapter.download_backup("backup_2026", destination)

    assert not (destination / "backup_2026").exists()
    assert list(destination.iterdir()) == []


def test_s3_scoped_delete_does_not_touch_colliding_backup_name(tmp_path):
    fake = FakeBoto3()
    for key in (
        "odoo/prod/backup/db.dump",
        "odoo/prod/backup/manifest.json",
        "odoo/prod/backup-copy/db.dump",
        "odoo/prod/backup-copy/manifest.json",
    ):
        fake.s3.seed(key, key)
    adapter = make_adapter(fake, tmp_path)

    removed = adapter.delete_backup("backup")

    assert removed == [
        "odoo/prod/backup/manifest.json",
        "odoo/prod/backup/db.dump",
    ]
    assert sorted(fake.s3.objects) == [
        "odoo/prod/backup-copy/db.dump",
        "odoo/prod/backup-copy/manifest.json",
    ]
    delete_calls = [call for call in fake.s3.calls if call[0] == "delete"]
    assert delete_calls[0] == ("delete", "odoo/prod/backup/manifest.json")


def test_s3_delete_permanently_retains_abandonment_fence(tmp_path):
    fake = FakeBoto3()
    adapter = make_adapter(fake, tmp_path)
    marker_key = adapter.manifest_key("backup")
    payload_key = "odoo/prod/backup/db.dump"
    fence_key = adapter.abandonment_key("backup")
    fake.s3.seed(marker_key, remote_manifest("backup"))
    fake.s3.seed(payload_key, b"dump")
    fake.s3.seed(fence_key, b'{"status":"abandoned"}')

    removed = adapter.delete_backup("backup")

    assert removed == [marker_key, payload_key]
    assert sorted(fake.s3.objects) == [fence_key]
    assert ("delete", fence_key) not in fake.s3.calls


def test_s3_delete_removes_marker_first_when_list_is_stale(tmp_path):
    fake = FakeBoto3()
    marker_key = "odoo/prod/backup/manifest.json"
    payload_key = "odoo/prod/backup/db.dump"
    fake.s3.seed(marker_key, remote_manifest("backup"))
    fake.s3.seed(payload_key, b"dump")
    fake.s3.listed_keys = [payload_key]
    adapter = make_adapter(fake, tmp_path)

    removed = adapter.delete_backup("backup")

    assert removed == [marker_key, payload_key]
    assert marker_key not in fake.s3.objects
    delete_calls = [call for call in fake.s3.calls if call[0] == "delete"]
    assert delete_calls == [
        ("delete", marker_key),
        ("delete", payload_key),
    ]


def test_s3_rejects_paths_outside_prefix_and_malicious_provider_keys(
    tmp_path,
):
    fake = FakeBoto3()
    adapter = make_adapter(fake, tmp_path)

    with pytest.raises(S3PathError, match="outside the configured prefix"):
        adapter.download_object(
            "odoo/other/backup/db.dump",
            tmp_path / "db.dump",
        )
    with pytest.raises(S3PathError, match="safe path component"):
        adapter.delete_backup("../backup")

    fake.s3.listed_keys = ["odoo/prod/backup/../escape"]
    with pytest.raises(S3PathError, match="unsafe path segments"):
        adapter.list_objects("backup")


def test_s3_upload_validates_manifest_and_symlinks_before_remote_changes(
    tmp_path,
):
    fake = FakeBoto3()
    adapter = make_adapter(fake, tmp_path)
    missing_manifest = tmp_path / "missing-manifest"
    missing_manifest.mkdir()
    (missing_manifest / "db.dump").write_text("dump")

    with pytest.raises(S3IntegrityError, match="missing root manifest"):
        adapter.upload_backup(missing_manifest)
    with pytest.raises(S3ConfigurationError, match="backup_name is required"):
        adapter.upload_manifest(missing_manifest / "db.dump")

    backup_dir = make_backup(tmp_path, "symlinked")
    (backup_dir / "linked.dump").symlink_to(backup_dir / "nested" / "db.dump")
    with pytest.raises(S3PathError, match="symbolic link"):
        adapter.upload_backup(backup_dir)

    assert fake.s3.calls == []


def test_s3_adapter_passes_server_side_encryption_args(tmp_path, monkeypatch):
    backup_dir = make_backup(tmp_path)
    monkeypatch.setenv("ODOO_BACKUP_KMS_KEY_ID", "kms-key-id")
    fake = FakeBoto3()
    adapter = S3Adapter(
        RemoteBackupConfig(
            bucket="bucket-name",
            encryption_algorithm="aws:kms",
            encryption_key_env="ODOO_BACKUP_KMS_KEY_ID",
        ),
        root=tmp_path / "remote",
        boto3_module=fake,
    )

    adapter.upload_backup(backup_dir)

    for item in fake.s3.uploads:
        assert item["extra_args"]["ServerSideEncryption"] == "aws:kms"
        assert item["extra_args"]["SSEKMSKeyId"] == "kms-key-id"
