from __future__ import annotations

import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from odooctl.adapters.wal_s3 import (
    BASE_COMPLETION_MARKER,
    PitrPinnedWal,
    WalS3ClockSkewError,
    WalS3Adapter,
    WalS3ConflictError,
    WalS3IntegrityError,
    WalS3LeaseBusyError,
    WalS3LeaseError,
    WalS3LeaseExpiredError,
    WalS3PathError,
    WalS3PinError,
    WalS3ProviderError,
)


SEGMENT = "000000010000000A000000FE"
HISTORY = "00000002.history"
BACKUP_HISTORY = "000000010000000A000000FE.00000028.backup"
SYSTEM_IDENTIFIER = "7612345678901234567"


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class FakeClientError(RuntimeError):
    def __init__(
        self,
        code: str,
        status: int,
        message: str,
        *,
        server_time: datetime | None = None,
    ):
        super().__init__(message)
        headers = {}
        if server_time is not None:
            headers["date"] = format_datetime(
                server_time.astimezone(timezone.utc),
                usegmt=True,
            )
        self.response = {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {
                "HTTPStatusCode": status,
                "HTTPHeaders": headers,
            },
        }


class FakeS3Client:
    def __init__(self):
        self.objects: dict[str, dict[str, Any]] = {}
        self.put_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.delete_request_calls: list[dict[str, Any]] = []
        self.foreign_list_entries: list[dict[str, Any]] = []
        self.fail_put_key: str | None = None
        self.fail_put_when: str | None = None
        self.fail_put_message = "provider put failed"
        self.fail_delete_key: str | None = None
        self.fail_delete_when: str | None = None
        self.corrupt_get_keys: set[str] = set()
        self.server_time = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)
        self.omit_server_date = False

    def _response_metadata(self) -> dict[str, Any]:
        headers = (
            {}
            if self.omit_server_date
            else {"date": format_datetime(self.server_time, usegmt=True)}
        )
        return {
            "HTTPStatusCode": 200,
            "HTTPHeaders": headers,
        }

    def _missing(self, key: str) -> FakeClientError:
        return FakeClientError(
            "NoSuchKey",
            404,
            f"missing {key}",
            server_time=None if self.omit_server_date else self.server_time,
        )

    def put_object(self, **kwargs):
        body = kwargs["Body"].read()
        call = {key: value for key, value in kwargs.items() if key != "Body"}
        call["BodyBytes"] = body
        self.put_calls.append(call)
        key = kwargs["Key"]

        if self.fail_put_key == key and self.fail_put_when == "before":
            self.fail_put_key = None
            raise RuntimeError(self.fail_put_message)
        if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
            raise FakeClientError(
                "PreconditionFailed",
                412,
                f"exists {key}",
                server_time=self.server_time,
            )
        if kwargs.get("IfMatch") is not None and (
            key not in self.objects or self.objects[key]["etag"] != kwargs["IfMatch"].strip('"')
        ):
            raise FakeClientError(
                "PreconditionFailed",
                412,
                f"etag mismatch {key}",
                server_time=self.server_time,
            )

        self.objects[key] = {
            "body": body,
            "metadata": dict(kwargs.get("Metadata", {})),
            "etag": _sha(body),
            "last_modified": self.server_time,
        }
        if self.fail_put_key == key and self.fail_put_when == "after":
            self.fail_put_key = None
            raise TimeoutError(self.fail_put_message)
        return {
            "ETag": f'"{_sha(body)}"',
            "ResponseMetadata": self._response_metadata(),
        }

    def head_object(self, **kwargs):
        key = kwargs["Key"]
        if key not in self.objects:
            raise self._missing(key)
        stored = self.objects[key]
        return {
            "ContentLength": len(stored["body"]),
            "Metadata": dict(stored["metadata"]),
            "ETag": f'"{stored["etag"]}"',
            "LastModified": stored["last_modified"],
            "ResponseMetadata": self._response_metadata(),
        }

    def get_object(self, **kwargs):
        key = kwargs["Key"]
        if key not in self.objects:
            raise self._missing(key)
        self.get_calls.append(key)
        payload = self.objects[key]["body"]
        if key in self.corrupt_get_keys:
            payload = payload + b"-corrupt"
        return {
            "Body": io.BytesIO(payload),
            "ResponseMetadata": self._response_metadata(),
        }

    def list_objects_v2(self, **kwargs):
        prefix = kwargs["Prefix"]
        contents = [
            {
                "Key": key,
                "Size": len(stored["body"]),
                "ETag": f'"{stored["etag"]}"',
                "LastModified": stored["last_modified"],
            }
            for key, stored in sorted(self.objects.items())
            if key.startswith(prefix)
        ]
        contents.extend(self.foreign_list_entries)
        return {"Contents": contents, "IsTruncated": False}

    def delete_object(self, **kwargs):
        key = kwargs["Key"]
        self.delete_calls.append(key)
        self.delete_request_calls.append(dict(kwargs))
        if self.fail_delete_key == key:
            if self.fail_delete_when == "after":
                self.objects.pop(key, None)
                self.fail_delete_key = None
                self.fail_delete_when = None
                raise TimeoutError(f"lost delete response {key}")
            raise RuntimeError(f"refused delete {key}")
        if kwargs.get("IfMatch") is not None and (
            key not in self.objects or self.objects[key]["etag"] != kwargs["IfMatch"].strip('"')
        ):
            raise FakeClientError(
                "PreconditionFailed",
                412,
                f"etag mismatch {key}",
                server_time=self.server_time,
            )
        self.objects.pop(key, None)
        return {"ResponseMetadata": self._response_metadata()}

    def store_raw(
        self,
        key: str,
        payload: bytes,
        *,
        metadata_sha256: str | None = None,
    ) -> None:
        self.objects[key] = {
            "body": payload,
            "metadata": ({"sha256": metadata_sha256} if metadata_sha256 is not None else {}),
            "etag": _sha(payload),
            "last_modified": self.server_time,
        }


class FakeBoto3:
    def __init__(self, client: FakeS3Client):
        self.s3 = client
        self.client_calls: list[tuple[str, dict[str, Any]]] = []

    def client(self, name: str, **kwargs):
        self.client_calls.append((name, kwargs))
        return self.s3


def _config(**updates):
    values = {
        "bucket": "pitr-archive",
        "prefix": "odooctl-pitr",
        "region": "eu-central-1",
        "region_env": None,
        "endpoint_env": None,
        "endpoint_url": None,
        "access_key_env": None,
        "secret_key_env": None,
        "session_token_env": None,
        "encryption_algorithm": None,
        "encryption_key_env": None,
    }
    values.update(updates)
    return SimpleNamespace(**values)


def _adapter(
    client: FakeS3Client | None = None,
    *,
    project: str = "Demo / Sales",
    cluster_id: str = "primary",
    system_identifier: str = SYSTEM_IDENTIFIER,
    config=None,
) -> tuple[WalS3Adapter, FakeS3Client, FakeBoto3]:
    fake_client = client or FakeS3Client()
    module = FakeBoto3(fake_client)
    adapter = WalS3Adapter(
        config or _config(),
        project,
        cluster_id,
        system_identifier,
        boto3_module=module,
    )
    return adapter, fake_client, module


def _write_wal(tmp_path: Path, payload: bytes = b"wal payload") -> Path:
    path = tmp_path / SEGMENT
    path.write_bytes(payload)
    return path


def _base_manifest(
    base_backup_id: str,
    artifacts: dict[str, bytes],
    *,
    project: str = "Demo / Sales",
    environment: str = "production",
    cluster_id: str = "primary",
    system_identifier: str = SYSTEM_IDENTIFIER,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "base_backup_id": base_backup_id,
        "project": project,
        "environment": environment,
        "cluster_id": cluster_id,
        "system_identifier": system_identifier,
        "status": "complete",
        "artifacts": [
            {
                "path": path,
                "size": len(payload),
                "sha256": _sha(payload),
            }
            for path, payload in sorted(artifacts.items())
        ],
    }


def _write_base(
    tmp_path: Path,
    base_backup_id: str = "base-20260730-deadbeef",
) -> tuple[Path, dict[str, bytes], dict[str, Any]]:
    root = tmp_path / base_backup_id
    root.mkdir(parents=True)
    artifacts = {
        "base.tar": b"physical postgres base",
        "pg/backup_manifest": b"postgres backup manifest",
    }
    for relative, payload in artifacts.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    manifest = _base_manifest(base_backup_id, artifacts)
    (root / BASE_COMPLETION_MARKER).write_text(json.dumps(manifest))
    return root, artifacts, manifest


@pytest.mark.parametrize("filename", [SEGMENT, HISTORY, BACKUP_HISTORY])
def test_wal_archive_names_are_strict_and_namespace_is_cluster_scoped(filename):
    first, _client, _module = _adapter()
    second, _client2, _module2 = _adapter(project="Demo : Sales")

    assert first.validate_archive_name(filename) == filename
    assert first.wal_key(filename).startswith("odooctl-pitr/projects/demo-sales-")
    assert f"/clusters/primary/{SYSTEM_IDENTIFIER}/wal/{filename}" in first.wal_key(filename)
    # Lossy project slugs cannot collide because the raw project hash is part
    # of the namespace.
    assert first.project_namespace != second.project_namespace


@pytest.mark.parametrize(
    "filename",
    [
        "",
        "000000010000000a000000fe",
        f"{SEGMENT}.partial",
        f"../{SEGMENT}",
        f"nested/{SEGMENT}",
        "0000001.history",
        f"{SEGMENT}.0000002.backup",
        f"{SEGMENT}\x00",
    ],
)
def test_wal_archive_names_reject_partial_malformed_and_traversal(filename):
    adapter, _client, _module = _adapter()

    with pytest.raises(WalS3PathError):
        adapter.validate_archive_name(filename)


def test_archive_wal_uses_conditional_create_and_byte_verifies(tmp_path):
    adapter, client, _module = _adapter()
    source = _write_wal(tmp_path)

    receipt = adapter.archive_wal(source)

    assert receipt.filename == SEGMENT
    assert receipt.sha256 == _sha(b"wal payload")
    assert receipt.idempotent is False
    assert client.put_calls[0]["IfNoneMatch"] == "*"
    assert client.put_calls[0]["Metadata"] == {"sha256": receipt.sha256}
    assert client.put_calls[0]["Key"] == adapter.wal_key(SEGMENT)
    assert client.get_calls == [adapter.wal_key(SEGMENT)]


def test_archive_wal_identical_precondition_race_is_idempotent(tmp_path):
    adapter, client, _module = _adapter()
    source = _write_wal(tmp_path)
    first = adapter.archive_wal(source)

    retry = adapter.archive_wal(source)

    assert retry.idempotent is True
    assert retry.sha256 == first.sha256
    assert client.objects[first.key]["body"] == b"wal payload"
    assert all(call["IfNoneMatch"] == "*" for call in client.put_calls)


def test_archive_wal_conflicting_existing_bytes_are_never_overwritten(tmp_path):
    adapter, client, _module = _adapter()
    source = _write_wal(tmp_path, b"original")
    receipt = adapter.archive_wal(source)
    source.write_bytes(b"different")

    with pytest.raises(WalS3ConflictError, match="immutable"):
        adapter.archive_wal(source)

    assert client.objects[receipt.key]["body"] == b"original"


def test_archive_wal_lost_put_response_reconciles_identical_object(tmp_path):
    adapter, client, _module = _adapter()
    source = _write_wal(tmp_path)
    key = adapter.wal_key(SEGMENT)
    client.fail_put_key = key
    client.fail_put_when = "after"

    receipt = adapter.archive_wal(source)

    assert receipt.idempotent is True
    assert client.objects[key]["body"] == b"wal payload"


def test_provider_error_redacts_configured_and_standard_aws_secrets(
    tmp_path,
    monkeypatch,
):
    token_path = tmp_path / "aws-token"
    token_path.write_text("web-identity-super-secret\n")
    secrets = {
        "WAL_ACCESS": "configured-access-secret",
        "WAL_SECRET": "configured-secret-secret",
        "AWS_SESSION_TOKEN": "standard-session-secret",
        "AWS_WEB_IDENTITY_TOKEN_FILE": str(token_path),
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    adapter, client, _module = _adapter(
        config=_config(
            access_key_env="WAL_ACCESS",
            secret_key_env="WAL_SECRET",
        )
    )
    source = _write_wal(tmp_path)
    client.fail_put_key = adapter.wal_key(SEGMENT)
    client.fail_put_when = "before"
    client.fail_put_message = (
        "configured-access-secret configured-secret-secret "
        "standard-session-secret web-identity-super-secret "
        f"{token_path}"
    )

    with pytest.raises(WalS3ProviderError) as exc_info:
        adapter.archive_wal(source)

    message = str(exc_info.value)
    for secret in (*secrets.values(), "web-identity-super-secret"):
        assert secret not in message
    assert "***" in message


def test_download_wal_is_verified_and_refuses_destination_collisions(tmp_path):
    adapter, _client, _module = _adapter()
    adapter.archive_wal(_write_wal(tmp_path))
    destination = tmp_path / "restore" / SEGMENT

    info = adapter.download_wal(SEGMENT, destination)

    assert destination.read_bytes() == b"wal payload"
    assert info.sha256 == _sha(b"wal payload")
    with pytest.raises(WalS3PathError, match="already exists"):
        adapter.download_wal(SEGMENT, destination)
    assert destination.read_bytes() == b"wal payload"


def test_download_wal_rejects_symlink_and_cleans_corrupt_partial(tmp_path):
    adapter, client, _module = _adapter()
    receipt = adapter.archive_wal(_write_wal(tmp_path))
    link = tmp_path / "wal-link"
    target = tmp_path / "outside"
    target.write_bytes(b"outside")
    link.symlink_to(target)

    with pytest.raises(WalS3PathError):
        adapter.download_wal(SEGMENT, link)

    corrupt_target = tmp_path / "corrupt-download"
    client.corrupt_get_keys.add(receipt.key)
    with pytest.raises(WalS3IntegrityError, match="byte verification"):
        adapter.download_wal(SEGMENT, corrupt_target)
    assert not corrupt_target.exists()
    assert not list(tmp_path.glob(".corrupt-download.*.partial"))


def test_base_backup_upload_publishes_manifest_last_and_reads_authoritative_document(
    tmp_path,
):
    adapter, client, _module = _adapter()
    base_dir, artifacts, manifest = _write_base(tmp_path)

    receipt = adapter.upload_base_backup(base_dir.name, base_dir)

    assert client.put_calls[-1]["Key"] == adapter.base_manifest_key(base_dir.name)
    assert all(call["IfNoneMatch"] == "*" for call in client.put_calls)
    assert [Path(call["Key"]).name for call in client.put_calls[:-1]] == [
        "base.tar",
        "backup_manifest",
    ]
    assert receipt.base_backup_id == base_dir.name
    assert receipt.manifest.key == adapter.base_manifest_key(base_dir.name)
    assert {Path(item.key).name for item in receipt.objects} == {
        "base.tar",
        "backup_manifest",
    }
    assert adapter.list_base_backups() == [base_dir.name]
    assert adapter.read_base_manifest(base_dir.name) == manifest
    for relative, payload in artifacts.items():
        key = adapter.base_object_key(base_dir.name, relative)
        assert client.objects[key]["body"] == payload


def test_base_upload_validates_manifest_before_creating_orphan_payload(tmp_path):
    adapter, client, _module = _adapter()
    base_dir, _artifacts, manifest = _write_base(tmp_path)
    manifest["status"] = "pending"
    (base_dir / BASE_COMPLETION_MARKER).write_text(json.dumps(manifest))

    with pytest.raises(WalS3IntegrityError, match="status"):
        adapter.upload_base_backup(base_dir.name, base_dir)

    assert client.put_calls == []
    assert client.objects == {}


def test_base_upload_rejects_local_checksum_mismatch_before_remote_writes(
    tmp_path,
):
    adapter, client, _module = _adapter()
    base_dir, _artifacts, _manifest = _write_base(tmp_path)
    (base_dir / "base.tar").write_bytes(b"changed after manifest")

    with pytest.raises(WalS3IntegrityError, match="checksum"):
        adapter.upload_base_backup(base_dir.name, base_dir)

    assert client.put_calls == []
    assert client.objects == {}


def test_base_manifest_rejects_traversal_and_wrong_scope_before_publication(
    tmp_path,
):
    adapter, client, _module = _adapter()
    base_dir, _artifacts, manifest = _write_base(tmp_path)
    manifest["artifacts"][0]["path"] = "../escape"
    (base_dir / BASE_COMPLETION_MARKER).write_text(json.dumps(manifest))

    with pytest.raises(WalS3PathError):
        adapter.upload_base_backup(base_dir.name, base_dir)
    assert client.put_calls == []

    manifest = _base_manifest(base_dir.name, {"base.tar": b"physical"})
    manifest["project"] = "foreign"
    (base_dir / BASE_COMPLETION_MARKER).write_text(json.dumps(manifest))
    with pytest.raises(WalS3IntegrityError, match="project"):
        adapter.upload_base_backup(base_dir.name, base_dir)
    assert client.put_calls == []


def test_publish_base_manifest_refuses_unexpected_inventory_before_marker(
    tmp_path,
):
    adapter, client, _module = _adapter()
    base_dir, _artifacts, _manifest = _write_base(tmp_path)
    adapter.upload_base_payload(base_dir.name, base_dir)
    extra = adapter.base_object_key(base_dir.name, "undeclared")
    client.store_raw(extra, b"extra", metadata_sha256=_sha(b"extra"))

    with pytest.raises(WalS3IntegrityError, match="unexpected"):
        adapter.publish_base_manifest(
            base_dir.name,
            base_dir / BASE_COMPLETION_MARKER,
        )

    assert adapter.base_manifest_key(base_dir.name) not in client.objects


def test_base_retry_is_idempotent_but_changed_payload_is_a_conflict(tmp_path):
    adapter, client, _module = _adapter()
    base_dir, _artifacts, _manifest = _write_base(tmp_path)
    first = adapter.upload_base_backup(base_dir.name, base_dir)

    retry = adapter.upload_base_backup(base_dir.name, base_dir)

    assert retry.idempotent is True
    assert retry.manifest.sha256 == first.manifest.sha256

    (base_dir / "base.tar").write_bytes(b"changed physical base")
    changed = _base_manifest(
        base_dir.name,
        {
            "base.tar": b"changed physical base",
            "pg/backup_manifest": b"postgres backup manifest",
        },
    )
    (base_dir / BASE_COMPLETION_MARKER).write_text(json.dumps(changed))
    with pytest.raises(WalS3ConflictError):
        adapter.upload_base_backup(base_dir.name, base_dir)
    assert (
        client.objects[adapter.base_object_key(base_dir.name, "base.tar")]["body"]
        == b"physical postgres base"
    )


def test_lost_base_manifest_response_reconciles_completed_backup(tmp_path):
    adapter, client, _module = _adapter()
    base_dir, _artifacts, _manifest = _write_base(tmp_path)
    marker = adapter.base_manifest_key(base_dir.name)
    client.fail_put_key = marker
    client.fail_put_when = "after"

    receipt = adapter.upload_base_backup(base_dir.name, base_dir)

    assert receipt.idempotent is True
    assert adapter.list_base_backups() == [base_dir.name]
    assert adapter.verify_base_backup(base_dir.name).manifest.key == marker


def test_base_verify_rejects_corrupt_bytes_and_unexpected_scoped_objects(tmp_path):
    adapter, client, _module = _adapter()
    base_dir, _artifacts, _manifest = _write_base(tmp_path)
    adapter.upload_base_backup(base_dir.name, base_dir)
    base_key = adapter.base_object_key(base_dir.name, "base.tar")
    client.corrupt_get_keys.add(base_key)

    with pytest.raises(WalS3IntegrityError, match="content checksum"):
        adapter.verify_base_backup(base_dir.name)

    client.corrupt_get_keys.clear()
    extra = adapter.base_object_key(base_dir.name, "undeclared")
    client.store_raw(extra, b"extra", metadata_sha256=_sha(b"extra"))
    with pytest.raises(WalS3IntegrityError, match="unexpected"):
        adapter.verify_base_backup(base_dir.name)


def test_list_operations_ignore_foreign_provider_entries(tmp_path):
    adapter, client, _module = _adapter()
    adapter.archive_wal(_write_wal(tmp_path))
    base_dir, _artifacts, _manifest = _write_base(tmp_path)
    adapter.upload_base_backup(base_dir.name, base_dir)
    client.foreign_list_entries = [
        {
            "Key": "other/projects/foreign/clusters/x/1/wal/" + SEGMENT,
            "Size": 10,
        },
        {
            "Key": "other/projects/foreign/bases/base-x/manifest.json",
            "Size": 10,
        },
    ]

    assert [item.key for item in adapter.list_wal()] == [adapter.wal_key(SEGMENT)]
    assert adapter.list_base_backups() == [base_dir.name]


def test_download_base_backup_stages_verifies_and_publishes_atomically(tmp_path):
    adapter, _client, _module = _adapter()
    base_dir, artifacts, manifest = _write_base(tmp_path / "source")
    adapter.upload_base_backup(base_dir.name, base_dir)
    destination_root = tmp_path / "downloaded"

    target = adapter.download_base_backup(base_dir.name, destination_root)

    assert target == destination_root / base_dir.name
    for relative, payload in artifacts.items():
        assert (target / relative).read_bytes() == payload
    assert json.loads((target / BASE_COMPLETION_MARKER).read_text()) == manifest
    assert not list(destination_root.glob(f".{base_dir.name}.*.partial"))

    with pytest.raises(WalS3PathError, match="already exists"):
        adapter.download_base_backup(base_dir.name, destination_root)
    assert (target / "base.tar").read_bytes() == artifacts["base.tar"]


def test_download_base_rejects_remote_manifest_traversal_without_writes(tmp_path):
    adapter, client, _module = _adapter()
    base_id = "base-hostile"
    payload = b"escape"
    manifest = _base_manifest(base_id, {"../escape": payload})
    manifest_bytes = json.dumps(manifest).encode()
    marker = adapter.base_manifest_key(base_id)
    client.store_raw(marker, manifest_bytes, metadata_sha256=_sha(manifest_bytes))
    destination_root = tmp_path / "download"

    with pytest.raises(WalS3PathError):
        adapter.download_base_backup(base_id, destination_root)

    assert not (destination_root / base_id).exists()
    assert not (tmp_path / "escape").exists()


def test_download_base_corruption_never_publishes_partial_directory(tmp_path):
    adapter, client, _module = _adapter()
    base_dir, _artifacts, _manifest = _write_base(tmp_path / "source")
    adapter.upload_base_backup(base_dir.name, base_dir)
    client.corrupt_get_keys.add(adapter.base_object_key(base_dir.name, "base.tar"))
    destination_root = tmp_path / "download"

    with pytest.raises(WalS3IntegrityError, match="byte verification"):
        adapter.download_base_backup(base_dir.name, destination_root)

    assert not (destination_root / base_dir.name).exists()
    assert not list(destination_root.glob(f".{base_dir.name}.*.partial"))


def test_delete_base_removes_completion_marker_first_and_delete_wal_is_exact(
    tmp_path,
):
    adapter, client, _module = _adapter()
    wal_receipt = adapter.archive_wal(_write_wal(tmp_path))
    base_dir, _artifacts, _manifest = _write_base(tmp_path / "source")
    adapter.upload_base_backup(base_dir.name, base_dir)
    marker = adapter.base_manifest_key(base_dir.name)

    deleted = adapter.delete_base_backup(base_dir.name)

    assert deleted[0] == marker
    assert client.delete_calls[0] == marker
    assert adapter.list_base_backups() == []

    assert adapter.delete_wal(SEGMENT) == wal_receipt.key
    assert client.delete_calls[-1] == wal_receipt.key
    assert wal_receipt.key not in client.objects


def test_failed_marker_delete_never_deletes_base_payload(tmp_path):
    adapter, client, _module = _adapter()
    base_dir, _artifacts, _manifest = _write_base(tmp_path)
    adapter.upload_base_backup(base_dir.name, base_dir)
    marker = adapter.base_manifest_key(base_dir.name)
    client.fail_delete_key = marker

    with pytest.raises(WalS3ProviderError, match="delete"):
        adapter.delete_base_backup(base_dir.name)

    assert client.delete_calls == [marker]
    assert adapter.base_object_key(base_dir.name, "base.tar") in client.objects


def test_delete_base_reasserts_coordination_before_every_remote_delete(
    tmp_path,
):
    adapter, client, _module = _adapter()
    base_dir, _artifacts, _manifest = _write_base(tmp_path)
    adapter.upload_base_backup(base_dir.name, base_dir)
    observations: list[tuple[str, tuple[str, ...]]] = []

    deleted = adapter.delete_base_backup(
        base_dir.name,
        before_delete=lambda key: observations.append((key, tuple(client.delete_calls))),
    )

    assert tuple(key for key, _previous in observations) == deleted
    assert [previous for _key, previous in observations] == [
        deleted[:index] for index in range(len(deleted))
    ]
    assert tuple(client.delete_calls) == deleted


def test_delete_base_stops_when_inter_object_coordination_reassertion_fails(
    tmp_path,
):
    adapter, client, _module = _adapter()
    base_dir, _artifacts, _manifest = _write_base(tmp_path)
    adapter.upload_base_backup(base_dir.name, base_dir)
    marker = adapter.base_manifest_key(base_dir.name)
    callbacks: list[str] = []

    def reassert(key: str) -> None:
        callbacks.append(key)
        if len(callbacks) == 2:
            raise WalS3LeaseExpiredError("lease expired between objects")

    with pytest.raises(WalS3LeaseExpiredError, match="between objects"):
        adapter.delete_base_backup(
            base_dir.name,
            before_delete=reassert,
        )

    assert client.delete_calls == [marker]
    assert marker not in client.objects
    assert adapter.base_object_key(base_dir.name, "base.tar") in client.objects


@pytest.mark.parametrize(
    "action",
    [
        lambda adapter: adapter.delete_wal("../" + SEGMENT),
        lambda adapter: adapter.delete_base_backup("../foreign"),
        lambda adapter: adapter._delete_owned_key(  # noqa: SLF001
            "foreign/projects/x/clusters/y/1/wal/" + SEGMENT
        ),
    ],
)
def test_delete_apis_refuse_foreign_or_out_of_scope_targets(action):
    adapter, client, _module = _adapter()

    with pytest.raises(WalS3PathError):
        action(adapter)

    assert client.delete_calls == []


def _pin_wal(offset: int) -> PitrPinnedWal:
    filename = f"{int(SEGMENT, 16) + offset:024X}"
    return PitrPinnedWal(
        filename=filename,
        sha256=_sha(filename.encode()),
        size=16 * 1024 * 1024,
    )


def test_coordination_lease_is_create_only_and_cas_released():
    adapter, client, _module = _adapter()
    now = client.server_time

    lease = adapter.acquire_coordination_lease(
        purpose="retention",
        owner="host-a",
        ttl_seconds=60,
        now=now,
    )

    create_call = next(
        call for call in client.put_calls if call["Key"] == adapter.coordination_lease_key
    )
    assert create_call["IfNoneMatch"] == "*"
    assert "IfMatch" not in create_call
    assert lease.generation == 1
    assert lease.owner == "host-a"
    assert lease.expires_at == now + timedelta(seconds=60)
    assert lease.expired is False
    assert adapter.inspect_coordination_lease(now=now) == lease
    assert adapter.assert_coordination_lease(lease, now=now) == lease

    adapter.release_coordination_lease(lease, now=now)

    assert adapter.coordination_lease_key not in client.objects
    assert client.delete_request_calls[-1]["IfMatch"] == f'"{lease.etag}"'
    assert adapter.inspect_coordination_lease(now=now) is None


def test_active_coordination_lease_blocks_another_host():
    first, client, _module = _adapter()
    second, _same_client, _module2 = _adapter(client)
    now = client.server_time
    lease = first.acquire_coordination_lease(
        purpose="plan:one",
        owner="host-a",
        now=now,
    )

    with pytest.raises(WalS3LeaseBusyError, match="host-a"):
        second.acquire_coordination_lease(
            purpose="retention",
            owner="host-b",
            now=now,
        )

    assert second.assert_coordination_lease(lease, now=now) == lease


def test_expired_coordination_lease_is_never_stolen_but_owner_can_release():
    first, client, _module = _adapter()
    second, _same_client, _module2 = _adapter(client)
    started = client.server_time
    lease = first.acquire_coordination_lease(
        purpose="retention",
        owner="host-a",
        ttl_seconds=30,
        now=started,
    )
    client.server_time = started + timedelta(seconds=31)

    with pytest.raises(WalS3LeaseExpiredError, match="cannot be stolen"):
        first.assert_coordination_lease(lease, now=client.server_time)
    with pytest.raises(WalS3LeaseExpiredError, match="stealing"):
        second.acquire_coordination_lease(
            purpose="plan:two",
            owner="host-b",
            now=client.server_time,
        )

    first.release_coordination_lease(lease, now=client.server_time)
    replacement = second.acquire_coordination_lease(
        purpose="plan:two",
        owner="host-b",
        now=client.server_time,
    )
    assert replacement.lease_id != lease.lease_id


def test_coordination_lease_fails_before_write_on_clock_skew_or_missing_date():
    adapter, client, _module = _adapter()

    with pytest.raises(WalS3ClockSkewError, match="clock differs"):
        adapter.acquire_coordination_lease(
            purpose="retention",
            owner="host-a",
            now=client.server_time + timedelta(seconds=61),
        )
    assert client.put_calls == []

    client.omit_server_date = True
    with pytest.raises(WalS3LeaseError, match="server time"):
        adapter.acquire_coordination_lease(
            purpose="retention",
            owner="host-a",
            now=client.server_time,
        )
    assert client.put_calls == []


@pytest.mark.parametrize(
    ("lease_id", "owner", "purpose", "owner_stopped"),
    [
        ("0" * 32, "host-a", "retention", None),
        (None, "host-b", "retention", None),
        (None, "host-a", "plan:other", None),
        (None, "host-a", "retention", "OWNER_STOPPED:wrong"),
    ],
)
def test_expired_lease_recovery_requires_exact_typed_confirmations(
    lease_id,
    owner,
    purpose,
    owner_stopped,
):
    adapter, client, _module = _adapter()
    started = client.server_time
    lease = adapter.acquire_coordination_lease(
        purpose="retention",
        owner="host-a",
        ttl_seconds=30,
        now=started,
    )

    with pytest.raises(WalS3LeaseBusyError, match="active"):
        adapter.recover_expired_coordination_lease(
            confirm_lease_id=lease.lease_id,
            confirm_owner=lease.owner,
            confirm_purpose=lease.purpose,
            confirm_owner_stopped=f"OWNER_STOPPED:{lease.lease_id}",
            now=started,
        )
    client.server_time = started + timedelta(seconds=31)
    inspected = adapter.inspect_coordination_lease(now=client.server_time)
    assert inspected is not None
    assert inspected.expired is True

    expected_message = "owner-stopped" if owner_stopped is not None else "confirmations"
    with pytest.raises(WalS3LeaseError, match=expected_message):
        adapter.recover_expired_coordination_lease(
            confirm_lease_id=lease_id or lease.lease_id,
            confirm_owner=owner,
            confirm_purpose=purpose,
            confirm_owner_stopped=(owner_stopped or f"OWNER_STOPPED:{lease.lease_id}"),
            now=client.server_time,
        )

    assert adapter.coordination_lease_key in client.objects
    assert client.delete_calls == []


def test_exact_expired_lease_recovery_uses_cas_and_allows_new_owner():
    first, client, _module = _adapter()
    second, _same_client, _module2 = _adapter(client)
    started = client.server_time
    stale = first.acquire_coordination_lease(
        purpose="retention",
        owner="crashed-host",
        ttl_seconds=30,
        now=started,
    )
    client.server_time = started + timedelta(seconds=31)

    recovered = second.recover_expired_coordination_lease(
        confirm_lease_id=stale.lease_id,
        confirm_owner=stale.owner,
        confirm_purpose=stale.purpose,
        confirm_owner_stopped=f"OWNER_STOPPED:{stale.lease_id}",
        now=client.server_time,
    )

    assert recovered.lease_id == stale.lease_id
    assert recovered.expired is True
    assert client.delete_request_calls[-1]["IfMatch"] == f'"{stale.etag}"'
    replacement = second.acquire_coordination_lease(
        purpose="plan:new",
        owner="host-b",
        now=client.server_time,
    )
    assert replacement.lease_id != stale.lease_id


def test_coordination_lease_renewal_is_cas_and_stale_handle_fails():
    adapter, client, _module = _adapter()
    started = client.server_time
    lease = adapter.acquire_coordination_lease(
        purpose="retention",
        owner="host-a",
        ttl_seconds=60,
        now=started,
    )
    client.server_time = started + timedelta(seconds=20)

    renewed = adapter.renew_coordination_lease(
        lease,
        now=client.server_time,
    )

    renew_call = client.put_calls[-1]
    assert renew_call["IfMatch"] == f'"{lease.etag}"'
    assert "IfNoneMatch" not in renew_call
    assert renewed.generation == 2
    assert renewed.etag != lease.etag
    assert renewed.acquired_at == client.server_time
    assert renewed.expires_at == client.server_time + timedelta(seconds=60)
    with pytest.raises(WalS3LeaseError, match="identity, generation"):
        adapter.assert_coordination_lease(lease, now=client.server_time)
    with pytest.raises(WalS3LeaseError, match="different identity"):
        adapter.release_coordination_lease(lease, now=client.server_time)

    adapter.release_coordination_lease(renewed, now=client.server_time)


def test_identical_lease_body_rewrite_invalidates_stale_issuance():
    adapter, client, _module = _adapter()
    lease = adapter.acquire_coordination_lease(
        purpose="retention",
        owner="host-a",
        now=client.server_time,
    )
    stored = client.objects[lease.key]
    client.server_time += timedelta(seconds=10)
    client.store_raw(
        lease.key,
        stored["body"],
        metadata_sha256=stored["metadata"]["sha256"],
    )

    with pytest.raises(WalS3LeaseError, match="issuance changed"):
        adapter.assert_coordination_lease(
            lease,
            now=client.server_time,
        )
    with pytest.raises(WalS3LeaseError, match="issuance"):
        adapter.release_coordination_lease(
            lease,
            now=client.server_time,
        )
    assert lease.key in client.objects


def test_lost_lease_renew_and_release_responses_are_reconciled():
    adapter, client, _module = _adapter()
    lease = adapter.acquire_coordination_lease(
        purpose="retention",
        owner="host-a",
        now=client.server_time,
    )
    client.server_time += timedelta(seconds=10)
    client.fail_put_key = adapter.coordination_lease_key
    client.fail_put_when = "after"

    renewed = adapter.renew_coordination_lease(
        lease,
        now=client.server_time,
    )

    assert renewed.generation == 2
    client.fail_delete_key = adapter.coordination_lease_key
    client.fail_delete_when = "after"
    adapter.release_coordination_lease(
        renewed,
        now=client.server_time,
    )
    assert adapter.coordination_lease_key not in client.objects


def test_remote_pin_is_immutable_canonical_and_contains_exact_wal_receipts():
    adapter, client, _module = _adapter()
    lease = adapter.acquire_coordination_lease(
        purpose="plan:create",
        owner="host-a",
        now=client.server_time,
    )
    wal_segments = (_pin_wal(0), _pin_wal(1), _pin_wal(2))

    pin = adapter.create_pitr_pin(
        kind="plan",
        pin_id="production_pitr_plan_abc",
        environment="production",
        base_backup_id="production_base_abc",
        wal_segments=wal_segments,
        lease=lease,
        now=client.server_time,
    )
    retry = adapter.create_pitr_pin(
        kind="plan",
        pin_id=pin.pin_id,
        environment=pin.environment,
        base_backup_id=pin.base_backup_id,
        wal_segments=wal_segments,
        lease=lease,
        now=client.server_time,
    )

    document = json.loads(client.objects[pin.key]["body"])
    assert document["project"] == "Demo / Sales"
    assert document["cluster_id"] == "primary"
    assert document["system_identifier"] == SYSTEM_IDENTIFIER
    assert document["base_backup_id"] == "production_base_abc"
    assert document["first_wal"] == wal_segments[0].filename
    assert document["last_wal"] == wal_segments[-1].filename
    assert document["wal_count"] == 3
    assert document["wal_segments"] == [
        {
            "filename": item.filename,
            "sha256": item.sha256,
            "size": item.size,
        }
        for item in wal_segments
    ]
    assert client.objects[pin.key]["body"].endswith(b"\n")
    pin_puts = [call for call in client.put_calls if call["Key"] == pin.key]
    assert all(call["IfNoneMatch"] == "*" for call in pin_puts)
    assert retry == pin
    assert (
        adapter.get_pitr_pin(
            "plan",
            pin.pin_id,
            lease=lease,
            now=client.server_time,
        )
        == pin
    )
    assert adapter.list_pitr_pins(
        lease=lease,
        environment="production",
        now=client.server_time,
    ) == (pin,)


def test_remote_pin_conflict_and_stale_lease_fail_without_overwrite():
    adapter, client, _module = _adapter()
    lease = adapter.acquire_coordination_lease(
        purpose="plan:create",
        owner="host-a",
        now=client.server_time,
    )
    original = adapter.create_pitr_pin(
        kind="plan",
        pin_id="production_pitr_plan_abc",
        environment="production",
        base_backup_id="production_base_abc",
        wal_segments=(_pin_wal(0),),
        lease=lease,
        now=client.server_time,
    )
    client.server_time += timedelta(seconds=1)
    renewed = adapter.renew_coordination_lease(
        lease,
        now=client.server_time,
    )

    with pytest.raises(WalS3LeaseError, match="identity, generation"):
        adapter.create_pitr_pin(
            kind="restore",
            pin_id="production_pitr_restore_stale",
            environment="production",
            base_backup_id="production_base_abc",
            wal_segments=(_pin_wal(0),),
            lease=lease,
            now=client.server_time,
        )
    with pytest.raises(WalS3PinError, match="conflicts"):
        adapter.create_pitr_pin(
            kind="plan",
            pin_id=original.pin_id,
            environment="production",
            base_backup_id="production_base_different",
            wal_segments=(_pin_wal(0),),
            lease=renewed,
            now=client.server_time,
        )

    assert (
        adapter.get_pitr_pin(
            "plan",
            original.pin_id,
            lease=renewed,
            now=client.server_time,
        )
        == original
    )


def test_pin_listing_fails_closed_on_unexpected_or_foreign_scope_object():
    adapter, client, _module = _adapter()
    lease = adapter.acquire_coordination_lease(
        purpose="retention",
        owner="host-a",
        now=client.server_time,
    )
    unexpected = f"{adapter.pins_prefix}/scratch.tmp"
    client.store_raw(
        unexpected,
        b"unexpected",
        metadata_sha256=_sha(b"unexpected"),
    )

    with pytest.raises(WalS3PinError, match="Unexpected object"):
        adapter.list_pitr_pins(
            lease=lease,
            now=client.server_time,
        )

    client.objects.pop(unexpected)
    pin = adapter.create_pitr_pin(
        kind="plan",
        pin_id="production_pitr_plan_foreign",
        environment="production",
        base_backup_id="production_base_abc",
        wal_segments=(_pin_wal(0),),
        lease=lease,
        now=client.server_time,
    )
    document = json.loads(client.objects[pin.key]["body"])
    document["project"] = "foreign project"
    payload = (json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n").encode()
    client.store_raw(pin.key, payload, metadata_sha256=_sha(payload))

    with pytest.raises(WalS3PinError, match="archive scope"):
        adapter.list_pitr_pins(
            lease=lease,
            now=client.server_time,
        )


def test_pin_release_is_conditional_and_lost_response_is_reconciled():
    adapter, client, _module = _adapter()
    lease = adapter.acquire_coordination_lease(
        purpose="restore:release",
        owner="host-a",
        now=client.server_time,
    )
    pin = adapter.create_pitr_pin(
        kind="restore",
        pin_id="production_pitr_restore_abc",
        environment="production",
        base_backup_id="production_base_abc",
        wal_segments=(_pin_wal(0), _pin_wal(1)),
        lease=lease,
        now=client.server_time,
    )
    client.fail_delete_key = pin.key
    client.fail_delete_when = "after"

    adapter.release_pitr_pin(
        pin,
        lease=lease,
        now=client.server_time,
    )

    assert pin.key not in client.objects
    assert client.delete_request_calls[-1]["IfMatch"] == f'"{pin.etag}"'
