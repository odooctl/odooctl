from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from odooctl.adapters.pitr_postgres import (
    PhysicalBaseBackup,
    PostgresPitrInspection,
)
from odooctl.adapters.wal_s3 import PitrPinnedWal
from odooctl.config import OdooCtlConfig
from odooctl.context import ProjectContext
from odooctl.metadata.models import (
    PitrBaseBackupManifest,
    PitrRecoveryPlan,
    PitrRestoreMetadata,
    WalReceipt,
)
from odooctl.metadata.store import MetadataStore
from odooctl.odoo.db_swap import database_cutover_aside_name
from odooctl.services.context import ServiceContext
from odooctl.services.pitr import (
    PitrError,
    archive_command,
    archive_wal,
    create_base_backup,
    cutover_restore,
    execute_restore,
    inspect_coordination_lease,
    plan_restore,
    recover_expired_coordination_lease,
    reconcile_retention,
    wal_name_for_lsn,
    wal_name_for_sequence,
    wal_segment_range,
    wal_sequence,
)


UTC = timezone.utc
SYSTEM_ID = "7623400000000000001"
CLUSTER_ID = "primary-eu-1"
SEGMENT_SIZE = 16 * 1024 * 1024
RECOVERY_IMAGE = "postgres@sha256:" + ("1" * 64)
BASE_MANIFEST_BYTES = json.dumps(
    {
        "PostgreSQL-Backup-Manifest-Version": 2,
        "WAL-Ranges": [
            {
                "Timeline": 1,
                "Start-LSN": "0/01000000",
                "End-LSN": "0/03000000",
            }
        ],
    }
).encode()


def _dt(hour: int, minute: int = 0, *, day: int = 30) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=UTC)


def _z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _context(
    tmp_path: Path,
    *,
    recovery_image: str | None = RECOVERY_IMAGE,
    base_backups: int = 2,
    grace_hours: int = 24,
    system_identifier: str | None = SYSTEM_ID,
) -> ServiceContext:
    pitr: dict = {
        "enabled": True,
        "environment": "production",
        "cluster_id": CLUSTER_ID,
        "filestore_policy": "database_only",
        "destination": {
            "bucket": "demo-pitr",
            "prefix": "demo/postgres",
        },
        "retention": {
            "base_backups": base_backups,
            "grace_hours": grace_hours,
        },
    }
    if recovery_image is not None:
        pitr["recovery_image"] = recovery_image
    if system_identifier is not None:
        pitr["system_identifier"] = system_identifier
    config = OdooCtlConfig.model_validate(
        {
            "project": {
                "name": "demo project",
                "odoo_version": "19.0",
            },
            "postgres": {
                "host": "postgres",
                "user": "odoo",
                "password_env": "ODOO_DB_PASSWORD",
            },
            "odoo": {"image": "odoo:19.0"},
            "pitr": pitr,
            "environments": {
                "production": {
                    "branch": "main",
                    "domain": "odoo.example.com",
                    "db_name": "odoo_prod",
                    "filestore_path": "/srv/odoo/filestore/odoo_prod",
                }
            },
        }
    )
    project = ProjectContext(
        root=tmp_path,
        config_path=tmp_path / "odooctl.yml",
        config=config,
    )
    return ServiceContext(project=project)


def _inspection(
    *,
    timeline: int = 1,
    system_identifier: str = SYSTEM_ID,
) -> PostgresPitrInspection:
    return PostgresPitrInspection(
        server_version_num=170004,
        server_major=17,
        system_identifier=system_identifier,
        timeline=timeline,
        wal_segment_size_bytes=SEGMENT_SIZE,
        wal_level="replica",
        archive_mode="on",
        archive_command="odooctl pitr wal push %p %f",
        archive_library="",
        tablespaces=(),
    )


def _base_manifest(
    base_backup_id: str = "production_base_20260730T110000_deadbeef",
    *,
    completed_at: datetime | None = None,
    start_wal: str = "000000010000000000000001",
    end_wal: str = "000000010000000000000003",
    timeline: int = 1,
    system_identifier: str = SYSTEM_ID,
    postgres_image: str = RECOVERY_IMAGE,
) -> PitrBaseBackupManifest:
    completed = completed_at or _dt(11)
    manifest_digest = hashlib.sha256(BASE_MANIFEST_BYTES).hexdigest()
    return PitrBaseBackupManifest(
        base_backup_id=base_backup_id,
        project="demo project",
        environment="production",
        cluster_id=CLUSTER_ID,
        system_identifier=system_identifier,
        postgres_major=17,
        postgres_image=postgres_image,
        timeline=timeline,
        wal_segment_size=SEGMENT_SIZE,
        started_at=_z(completed - timedelta(minutes=5)),
        completed_at=_z(completed),
        start_lsn="0/01000000",
        end_lsn="0/03000000",
        start_wal=start_wal,
        end_wal=end_wal,
        artifact_paths=["pgdata/backup_manifest"],
        checksums={"pgdata/backup_manifest": manifest_digest},
        sizes={"pgdata/backup_manifest": len(BASE_MANIFEST_BYTES)},
        remote_uri=(
            "s3://demo-pitr/demo/postgres/projects/demo/clusters/"
            f"{CLUSTER_ID}/{system_identifier}/base/{base_backup_id}"
        ),
        status="complete",
        verified_at=_z(completed + timedelta(minutes=1)),
    )


def _wal_filename(sequence: int, *, timeline: int = 1) -> str:
    return wal_name_for_sequence(sequence, timeline, SEGMENT_SIZE)


def _wal_item(
    sequence: int,
    *,
    modified: datetime,
    timeline: int = 1,
    size: int = 4,
    digest: str | None = None,
) -> SimpleNamespace:
    filename = _wal_filename(sequence, timeline=timeline)
    return SimpleNamespace(
        key=(f"demo/postgres/clusters/{CLUSTER_ID}/{SYSTEM_ID}/wal/{timeline:08X}/{filename}"),
        size=size,
        sha256=digest or hashlib.sha256(filename.encode()).hexdigest(),
        last_modified=modified,
    )


class FakePostgres:
    def __init__(
        self,
        *,
        inspection: PostgresPitrInspection | None = None,
        wal_ranges: list[dict] | None = None,
        events: list[str] | None = None,
    ):
        self.inspection = inspection or _inspection()
        self.wal_ranges = wal_ranges or [
            {
                "Timeline": self.inspection.timeline,
                "Start-LSN": "0/01000000",
                "End-LSN": "0/03000000",
            }
        ]
        self.events = events if events is not None else []
        self.create_calls = 0

    def inspect(self) -> PostgresPitrInspection:
        self.events.append("postgres.inspect")
        return self.inspection

    def create_base_backup(
        self,
        destination: str | Path,
        *,
        inspection: PostgresPitrInspection | None = None,
        label: str = "odooctl-pitr",
    ) -> PhysicalBaseBackup:
        self.events.append("postgres.create_base")
        self.create_calls += 1
        pgdata = Path(destination)
        pgdata.mkdir()
        (pgdata / "PG_VERSION").write_text("17\n")
        manifest_bytes = json.dumps(
            {
                "PostgreSQL-Backup-Manifest-Version": 2,
                "WAL-Ranges": self.wal_ranges,
            }
        ).encode()
        (pgdata / "backup_manifest").write_bytes(manifest_bytes)
        (pgdata / "base-data").write_bytes(b"physical backup bytes")
        return PhysicalBaseBackup(
            pgdata=pgdata,
            tablespace_root=None,
            tablespaces=(),
            inspection=inspection or self.inspection,
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            verified=True,
        )


class FakeWalAdapter:
    def __init__(
        self,
        *,
        bases: list[PitrBaseBackupManifest] | None = None,
        wal_items: list[SimpleNamespace] | None = None,
        events: list[str] | None = None,
    ):
        self.bases = {manifest.base_backup_id: manifest for manifest in (bases or [])}
        self.wal_items = list(wal_items or [])
        self.events = events if events is not None else []
        self.verify_wal_error: BaseException | None = None
        self.verify_wal_errors: dict[str, BaseException] = {}
        self.verify_base_errors: dict[str, BaseException] = {}
        self.base_modified_overrides: dict[str, datetime] = {}
        self.verify_size_overrides: dict[str, int] = {}
        self.verify_wal_expectations: list[tuple[str, str | None]] = []
        self.archive_result: SimpleNamespace | None = None
        self.uploaded_manifest: dict | None = None
        self.uploaded_base_id: str | None = None
        self.deleted_bases: list[str] = []
        self.deleted_wal: list[str] = []
        self.coordination_events: list[str] = []
        self.lease_generation = 0
        self.pins: dict[tuple[str, str], SimpleNamespace] = {}
        self.inspected_lease = None
        self.recovered_lease = None
        self.recovery_confirmations = None
        self.base_manifest_bytes = BASE_MANIFEST_BYTES

    @staticmethod
    def _filename(item: SimpleNamespace) -> str:
        return item.key.rsplit("/", 1)[-1]

    def list_base_backups(self) -> list[str]:
        self.events.append("remote.list_bases")
        return list(self.bases)

    def read_base_manifest(self, base_backup_id: str) -> dict:
        self.events.append(f"remote.read_base:{base_backup_id}")
        return self.bases[base_backup_id].model_dump(mode="json")

    def verify_base_backup(self, base_backup_id: str) -> SimpleNamespace:
        self.events.append(f"remote.verify_base:{base_backup_id}")
        error = self.verify_base_errors.get(base_backup_id)
        if error is not None:
            raise error
        manifest = self.bases[base_backup_id]
        completed = datetime.fromisoformat(
            (manifest.completed_at or manifest.started_at).replace(
                "Z",
                "+00:00",
            )
        )
        return SimpleNamespace(
            base_backup_id=base_backup_id,
            manifest=SimpleNamespace(
                last_modified=self.base_modified_overrides.get(
                    base_backup_id,
                    completed,
                )
            ),
        )

    def list_wal(self) -> list[SimpleNamespace]:
        self.events.append("remote.list_wal")
        return list(self.wal_items)

    def verify_wal(
        self,
        filename: str,
        *,
        expected_sha256: str | None = None,
    ) -> SimpleNamespace:
        self.events.append(f"remote.verify_wal:{filename}")
        self.verify_wal_expectations.append((filename, expected_sha256))
        filename_error = self.verify_wal_errors.get(filename)
        if filename_error is not None:
            raise filename_error
        if self.verify_wal_error is not None:
            raise self.verify_wal_error
        item = next(
            (item for item in self.wal_items if self._filename(item) == filename),
            None,
        )
        if item is None:
            item = SimpleNamespace(
                key=f"wal/{filename}",
                size=4,
                sha256=expected_sha256 or hashlib.sha256(filename.encode()).hexdigest(),
                last_modified=_dt(12),
            )
        if expected_sha256 is not None and item.sha256 != expected_sha256:
            raise RuntimeError("WAL checksum mismatch")
        return SimpleNamespace(
            key=item.key,
            size=self.verify_size_overrides.get(filename, item.size),
            sha256=item.sha256,
            last_modified=item.last_modified,
        )

    def archive_wal(self, source: Path, filename: str) -> SimpleNamespace:
        self.events.append(f"remote.archive_wal:{filename}")
        if self.archive_result is not None:
            return self.archive_result
        return SimpleNamespace(
            filename=filename,
            key=f"demo/postgres/wal/{filename}",
            size=source.stat().st_size,
            sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            idempotent=False,
        )

    def base_prefix(self, base_backup_id: str) -> str:
        return (
            f"demo/postgres/projects/demo/clusters/{CLUSTER_ID}/{SYSTEM_ID}/base/{base_backup_id}"
        )

    def upload_base_backup(
        self,
        base_backup_id: str,
        backup_dir: str | Path,
    ) -> SimpleNamespace:
        self.events.append(f"remote.upload_base:{base_backup_id}")
        root = Path(backup_dir)
        marker = root / "manifest.json"
        assert marker.is_file()
        self.uploaded_manifest = json.loads(marker.read_text())
        self.uploaded_base_id = base_backup_id
        return SimpleNamespace(uri=f"s3://demo-pitr/{self.base_prefix(base_backup_id)}")

    def download_base_backup(
        self,
        base_backup_id: str,
        destination: str | Path,
    ) -> Path:
        self.events.append(f"remote.download_base:{base_backup_id}")
        root = Path(destination)
        (root / "pgdata").mkdir(parents=True)
        (root / "pgdata" / "backup_manifest").write_bytes(self.base_manifest_bytes)
        return root

    def download_wal(
        self,
        filename: str,
        destination: str | Path,
    ) -> SimpleNamespace:
        self.events.append(f"remote.download_wal:{filename}")
        item = next(item for item in self.wal_items if self._filename(item) == filename)
        Path(destination).write_bytes(b"wal!")
        return SimpleNamespace(
            key=item.key,
            size=item.size,
            sha256=item.sha256,
            last_modified=item.last_modified,
        )

    def delete_base_backup(
        self,
        base_backup_id: str,
        *,
        before_delete=None,
    ) -> tuple[str, ...]:
        self.events.append(f"remote.delete_base:{base_backup_id}")
        if before_delete is not None:
            before_delete(f"bases/{base_backup_id}/manifest.json")
            before_delete(f"bases/{base_backup_id}/payload")
        self.deleted_bases.append(base_backup_id)
        return (base_backup_id,)

    def delete_wal(self, filename: str) -> str:
        self.events.append(f"remote.delete_wal:{filename}")
        self.deleted_wal.append(filename)
        return filename

    def acquire_coordination_lease(
        self,
        *,
        purpose: str,
        owner: str,
        ttl_seconds: int,
    ) -> SimpleNamespace:
        self.lease_generation += 1
        self.coordination_events.append(f"lease.acquire:{purpose}:{owner}")
        return SimpleNamespace(generation=self.lease_generation)

    def renew_coordination_lease(
        self,
        lease: SimpleNamespace,
    ) -> SimpleNamespace:
        assert lease.generation == self.lease_generation
        self.lease_generation += 1
        self.coordination_events.append("lease.renew")
        return SimpleNamespace(generation=self.lease_generation)

    def release_coordination_lease(
        self,
        lease: SimpleNamespace,
    ) -> None:
        assert lease.generation == self.lease_generation
        self.coordination_events.append("lease.release")

    def inspect_coordination_lease(self):
        return self.inspected_lease

    def recover_expired_coordination_lease(self, **confirmations):
        self.recovery_confirmations = confirmations
        return self.recovered_lease

    @staticmethod
    def _pin(
        kind: str,
        pin_id: str,
        environment: str,
        base_backup_id: str,
        wal_segments,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            kind=kind,
            pin_id=pin_id,
            environment=environment,
            base_backup_id=base_backup_id,
            wal_segments=tuple(wal_segments),
            key=f"pins/{kind}/{pin_id}.json",
            etag=f"etag-{kind}-{pin_id}",
            sha256="f" * 64,
            last_modified=_dt(12),
        )

    def create_pitr_pin(
        self,
        *,
        kind: str,
        pin_id: str,
        environment: str,
        base_backup_id: str,
        wal_segments,
        lease: SimpleNamespace,
    ) -> SimpleNamespace:
        assert lease.generation == self.lease_generation
        candidate = self._pin(
            kind,
            pin_id,
            environment,
            base_backup_id,
            wal_segments,
        )
        existing = self.pins.get((kind, pin_id))
        if existing is not None:
            assert existing.__dict__ == candidate.__dict__
            return existing
        self.pins[(kind, pin_id)] = candidate
        self.coordination_events.append(f"pin.create:{kind}:{pin_id}")
        return candidate

    def get_pitr_pin(
        self,
        kind: str,
        pin_id: str,
        *,
        lease: SimpleNamespace,
    ) -> SimpleNamespace:
        assert lease.generation == self.lease_generation
        return self.pins[(kind, pin_id)]

    def list_pitr_pins(
        self,
        *,
        lease: SimpleNamespace,
        environment: str | None = None,
    ) -> tuple[SimpleNamespace, ...]:
        assert lease.generation == self.lease_generation
        return tuple(
            pin
            for pin in self.pins.values()
            if environment is None or pin.environment == environment
        )

    def release_pitr_pin(
        self,
        pin: SimpleNamespace,
        *,
        lease: SimpleNamespace,
    ) -> None:
        assert lease.generation == self.lease_generation
        assert self.pins[(pin.kind, pin.pin_id)] is pin
        del self.pins[(pin.kind, pin.pin_id)]
        self.coordination_events.append(
            f"pin.release:{pin.kind}:{pin.pin_id}"
        )


class FakeRuntime:
    def __init__(
        self,
        events: list[str],
        *,
        error: BaseException | None = None,
    ):
        self.events = events
        self.error = error
        self.spec = None

    def recover_and_dump(self, spec, output: str | Path) -> SimpleNamespace:
        self.events.append("runtime.recover_and_dump")
        self.spec = spec
        if self.error is not None:
            raise self.error
        dump_path = Path(output)
        dump_path.write_bytes(b"custom-format-dump")
        return SimpleNamespace(
            dump_path=dump_path,
            database=spec.database,
            target_time=spec.target_time,
            target_timeline=str(spec.target_timeline),
            postgres_image=spec.postgres_image,
            status=SimpleNamespace(
                in_recovery=True,
                replay_paused=True,
                timeline=int(spec.target_timeline),
                last_replay_timestamp=spec.target_time,
                replay_lsn="0/04000000",
            ),
        )


class FakeDb:
    def __init__(
        self,
        events: list[str],
        *,
        existing: bool = False,
        create_error: BaseException | None = None,
        restore_error: BaseException | None = None,
        ping_error: BaseException | None = None,
    ):
        self.events = events
        self.existing = existing
        self.create_error = create_error
        self.restore_error = restore_error
        self.ping_error = ping_error

    def database_exists(self, database: str) -> bool:
        self.events.append(f"db.exists:{database}")
        return self.existing

    def create(self, database: str) -> None:
        self.events.append(f"db.create:{database}")
        if self.create_error is not None:
            raise self.create_error
        if self.existing:
            raise PitrError(f"PITR target database already exists: {database}")
        self.existing = True

    def restore_into(self, database: str, dump_path: Path) -> None:
        self.events.append(f"db.restore_into:{database}")
        assert dump_path.read_bytes() == b"custom-format-dump"
        if self.restore_error is not None:
            raise self.restore_error

    def ping(self, database: str) -> None:
        self.events.append(f"db.ping:{database}")
        if self.ping_error is not None:
            raise self.ping_error

    def psql(self, database: str, sql: str) -> None:
        self.events.append(f"db.psql:{database}")
        assert "ir_module_module" in sql

    def drop(self, database: str) -> None:
        self.events.append(f"db.drop:{database}")
        self.existing = False


def test_wal_math_rolls_from_segment_ff_into_next_log():
    last_in_log = "0000000100000000000000FF"
    first_next_log = "000000010000000100000000"

    assert wal_name_for_lsn("0/FF000000", 1, SEGMENT_SIZE) == last_in_log
    assert wal_name_for_lsn("1/00000000", 1, SEGMENT_SIZE) == first_next_log
    assert wal_sequence(first_next_log, SEGMENT_SIZE) == (
        wal_sequence(last_in_log, SEGMENT_SIZE) + 1
    )
    assert wal_segment_range(
        last_in_log,
        first_next_log,
        SEGMENT_SIZE,
    ) == (last_in_log, first_next_log)


def test_coordination_lease_inspection_and_recovery_forward_exact_attestation(
    tmp_path,
):
    ctx = _context(tmp_path)
    adapter = FakeWalAdapter()
    lease = SimpleNamespace(lease_id="lease-123", expired=True)
    adapter.inspected_lease = lease
    adapter.recovered_lease = lease

    assert inspect_coordination_lease(
        ctx,
        "production",
        adapter=adapter,
    ) is lease
    recovered = recover_expired_coordination_lease(
        ctx,
        "production",
        confirm_lease_id="lease-123",
        confirm_owner="host-a",
        confirm_purpose="pitr-retention",
        confirm_owner_stopped="OWNER_STOPPED:lease-123",
        adapter=adapter,
    )

    assert recovered is lease
    assert adapter.recovery_confirmations == {
        "confirm_lease_id": "lease-123",
        "confirm_owner": "host-a",
        "confirm_purpose": "pitr-retention",
        "confirm_owner_stopped": "OWNER_STOPPED:lease-123",
    }


def test_archive_command_escapes_literal_postgres_percent_tokens(tmp_path):
    original = _context(tmp_path)
    project = ProjectContext(
        root=tmp_path / "project%f",
        config_path=tmp_path / "project%f" / "odoo%p.yml",
        config=original.project.config,
    )

    command = archive_command(
        ServiceContext(project=project),
        "production",
        odooctl_bin="/opt/odoo%%ctl",
    )

    assert "/opt/odoo%%%%ctl" in command
    assert "project%%f" in command
    assert "odoo%%p.yml" in command
    assert command.count('"%p"') == 1
    assert command.count('"%f"') == 1


@pytest.mark.parametrize("wal_segment_size", [0, 3 * 1024 * 1024])
def test_wal_name_for_sequence_rejects_invalid_segment_sizes(
    wal_segment_size: int,
):
    with pytest.raises(ValueError, match="segment size"):
        wal_name_for_sequence(1, 1, wal_segment_size)


def test_wal_range_refuses_implicit_timeline_crossing():
    with pytest.raises(PitrError, match="cannot cross timelines"):
        wal_segment_range(
            "000000010000000000000001",
            "000000020000000000000002",
            SEGMENT_SIZE,
        )


def test_archive_wal_validates_source_and_persists_receipt(tmp_path):
    ctx = _context(tmp_path)
    events: list[str] = []
    postgres = FakePostgres(events=events)
    adapter = FakeWalAdapter(events=events)
    filename = "00000001000000000000000A"
    source = tmp_path / filename
    source.write_bytes(b"wal segment")

    receipt = archive_wal(
        ctx,
        "production",
        source,
        filename,
        postgres=postgres,
        adapter=adapter,
        now=_dt(12),
    )

    assert receipt.filename == filename
    assert receipt.timeline == 1
    assert receipt.sha256 == hashlib.sha256(b"wal segment").hexdigest()
    assert receipt.remote_uri.endswith(f"/{filename}")
    assert events == [
        "postgres.inspect",
        f"remote.archive_wal:{filename}",
    ]
    assert (
        MetadataStore(ctx.project.state_dir).get_wal_receipt(
            CLUSTER_ID,
            SYSTEM_ID,
            filename,
        )
        == receipt
    )


@pytest.mark.parametrize("source_kind", ["wrong_name", "symlink", "directory"])
def test_archive_wal_rejects_unsafe_source_before_remote_write(
    tmp_path,
    source_kind: str,
):
    ctx = _context(tmp_path)
    events: list[str] = []
    adapter = FakeWalAdapter(events=events)
    filename = "00000001000000000000000A"
    if source_kind == "wrong_name":
        source = tmp_path / "different-name"
        source.write_bytes(b"wal")
    elif source_kind == "symlink":
        target = tmp_path / "target"
        target.write_bytes(b"wal")
        source = tmp_path / filename
        source.symlink_to(target)
    else:
        source = tmp_path / filename
        source.mkdir()

    with pytest.raises(PitrError, match="regular file.*basename"):
        archive_wal(
            ctx,
            "production",
            source,
            filename,
            postgres=FakePostgres(events=events),
            adapter=adapter,
        )

    assert events == []


def test_archive_wal_idempotent_retry_reuses_durable_receipt(tmp_path):
    ctx = _context(tmp_path)
    filename = "00000001000000000000000A"
    source = tmp_path / filename
    source.write_bytes(b"wal segment")
    postgres = FakePostgres()
    adapter = FakeWalAdapter()

    first = archive_wal(
        ctx,
        "production",
        source,
        filename,
        postgres=postgres,
        adapter=adapter,
        now=_dt(12),
    )
    second = archive_wal(
        ctx,
        "production",
        source,
        filename,
        postgres=postgres,
        adapter=adapter,
        now=_dt(12, 1),
    )

    assert second == first


def test_base_backup_checks_end_wal_before_completion_manifest_upload(
    tmp_path,
    monkeypatch,
):
    from odooctl.services import pitr

    ctx = _context(tmp_path)
    events: list[str] = []
    postgres = FakePostgres(events=events)
    adapter = FakeWalAdapter(events=events)
    monkeypatch.setattr(pitr.secrets, "token_hex", lambda size: "a" * (size * 2))
    monkeypatch.setattr(pitr, "_now_utc", lambda: _dt(11, 10))

    manifest = create_base_backup(
        ctx,
        "production",
        postgres=postgres,
        adapter=adapter,
        now=_dt(11),
    )

    expected_end = "000000010000000000000003"
    verify_index = events.index(f"remote.verify_wal:{expected_end}")
    upload_index = events.index(f"remote.upload_base:{manifest.base_backup_id}")
    assert verify_index < upload_index
    assert adapter.uploaded_manifest == manifest.model_dump(mode="json")
    assert "manifest.json" not in manifest.artifact_paths
    assert "pgdata/backup_manifest" in manifest.artifact_paths
    assert (
        MetadataStore(ctx.project.state_dir).get_pitr_base_manifest(manifest.base_backup_id)
        == manifest
    )
    staging_root = ctx.project.state_dir / "pitr" / "staging"
    assert not list(staging_root.glob(".partial-*"))


def test_base_backup_end_wal_failure_never_uploads_or_indexes_manifest(
    tmp_path,
    monkeypatch,
):
    from odooctl.services import pitr

    ctx = _context(tmp_path)
    events: list[str] = []
    postgres = FakePostgres(events=events)
    adapter = FakeWalAdapter(events=events)
    adapter.verify_wal_error = RuntimeError("end WAL unavailable")
    monkeypatch.setattr(pitr.secrets, "token_hex", lambda size: "b" * (size * 2))

    with pytest.raises(RuntimeError, match="end WAL unavailable"):
        create_base_backup(
            ctx,
            "production",
            postgres=postgres,
            adapter=adapter,
            now=_dt(11),
        )

    assert not any(event.startswith("remote.upload_base:") for event in events)
    assert MetadataStore(ctx.project.state_dir).list_pitr_base_manifests() == []
    assert not list((ctx.project.state_dir / "pitr" / "staging").glob(".partial-*"))


def test_base_backup_rejects_ambiguous_postgres_wal_range_before_upload(
    tmp_path,
):
    ctx = _context(tmp_path)
    events: list[str] = []
    postgres = FakePostgres(
        wal_ranges=[
            {
                "Timeline": 1,
                "Start-LSN": "0/01000000",
                "End-LSN": "0/02000000",
            },
            {
                "Timeline": 1,
                "Start-LSN": "0/02000000",
                "End-LSN": "0/03000000",
            },
        ],
        events=events,
    )
    adapter = FakeWalAdapter(events=events)

    with pytest.raises(PitrError, match="no unambiguous WAL range"):
        create_base_backup(
            ctx,
            "production",
            postgres=postgres,
            adapter=adapter,
            now=_dt(11),
        )

    assert not any(event.startswith("remote.verify_wal:") for event in events)
    assert not any(event.startswith("remote.upload_base:") for event in events)


def test_base_backup_requires_pinned_recovery_image_before_source_access(
    tmp_path,
):
    with pytest.raises(ValueError, match="recovery_image is required"):
        _context(tmp_path, recovery_image=None)


def test_plan_restore_rejects_naive_target_before_remote_access(tmp_path):
    ctx = _context(tmp_path)
    adapter = FakeWalAdapter()

    with pytest.raises(ValueError, match="target_time must include a timezone"):
        plan_restore(
            ctx,
            "production",
            datetime(2026, 7, 30, 12),
            adapter=adapter,
            now=_dt(13),
        )

    assert adapter.events == []


def test_plan_restore_rejects_future_target_before_remote_access(tmp_path):
    ctx = _context(tmp_path)
    adapter = FakeWalAdapter()

    with pytest.raises(PitrError, match="cannot be in the future"):
        plan_restore(
            ctx,
            "production",
            _dt(13),
            adapter=adapter,
            now=_dt(12),
        )

    assert adapter.events == []


def test_plan_restore_selects_latest_eligible_base_and_byte_verifies_wal(
    tmp_path,
    monkeypatch,
):
    from odooctl.services import pitr

    ctx = _context(tmp_path)
    older = _base_manifest(
        "production_base_older",
        completed_at=_dt(9),
        end_wal=_wal_filename(3),
    )
    selected = _base_manifest(
        "production_base_selected",
        completed_at=_dt(11),
        start_wal=_wal_filename(4),
        end_wal=_wal_filename(5),
    )
    too_new = _base_manifest(
        "production_base_future",
        completed_at=_dt(13),
        start_wal=_wal_filename(6),
        end_wal=_wal_filename(7),
    )
    wal_items = [
        _wal_item(5, modified=_dt(11, 30)),
        _wal_item(6, modified=_dt(12, 5)),
    ]
    adapter = FakeWalAdapter(
        bases=[too_new, older, selected],
        wal_items=wal_items,
    )
    monkeypatch.setattr(pitr.secrets, "token_hex", lambda size: "c" * (size * 2))

    plan = plan_restore(
        ctx,
        "production",
        _dt(12),
        adapter=adapter,
        now=_dt(14),
    )

    assert plan.base_backup_id == selected.base_backup_id
    assert plan.first_wal == _wal_filename(5)
    assert plan.last_wal == _wal_filename(6)
    assert plan.wal_count == 2
    assert plan.wal_bytes == 8
    assert f"remote.verify_base:{selected.base_backup_id}" in adapter.events
    assert [event for event in adapter.events if event.startswith("remote.verify_wal:")] == [
        f"remote.verify_wal:{_wal_filename(5)}",
        f"remote.verify_wal:{_wal_filename(6)}",
    ]
    assert MetadataStore(ctx.project.state_dir).get_pitr_recovery_plan(plan.plan_id) == plan


def test_plan_restore_honors_an_explicit_base_selection(
    tmp_path,
    monkeypatch,
):
    from odooctl.services import pitr

    ctx = _context(tmp_path)
    requested = _base_manifest(
        "production_base_requested",
        completed_at=_dt(9),
        end_wal=_wal_filename(3),
    )
    newer = _base_manifest(
        "production_base_newer",
        completed_at=_dt(11),
        start_wal=_wal_filename(4),
        end_wal=_wal_filename(5),
    )
    adapter = FakeWalAdapter(
        bases=[requested, newer],
        wal_items=[
            _wal_item(3, modified=_dt(9, 30)),
            _wal_item(4, modified=_dt(12, 5)),
        ],
    )
    monkeypatch.setattr(pitr.secrets, "token_hex", lambda size: "d" * (size * 2))

    plan = plan_restore(
        ctx,
        "production",
        _dt(12),
        base_backup_id=requested.base_backup_id,
        adapter=adapter,
        now=_dt(14),
    )

    assert plan.base_backup_id == requested.base_backup_id
    assert plan.first_wal == _wal_filename(3)


def test_plan_restore_requires_explicit_timeline_for_mixed_inventory(
    tmp_path,
):
    ctx = _context(tmp_path)
    base = _base_manifest(end_wal=_wal_filename(3))
    adapter = FakeWalAdapter(
        bases=[base],
        wal_items=[
            _wal_item(3, modified=_dt(12, 5), timeline=1),
            _wal_item(3, modified=_dt(12, 5), timeline=2),
        ],
    )

    with pytest.raises(PitrError, match="timeline must be explicit"):
        plan_restore(
            ctx,
            "production",
            _dt(12),
            adapter=adapter,
            now=_dt(14),
        )

    assert MetadataStore(ctx.project.state_dir).list_pitr_recovery_plans() == []


def test_plan_restore_uses_explicit_compatible_timeline(
    tmp_path,
    monkeypatch,
):
    from odooctl.services import pitr

    ctx = _context(tmp_path)
    base = _base_manifest(end_wal=_wal_filename(3))
    adapter = FakeWalAdapter(
        bases=[base],
        wal_items=[
            _wal_item(3, modified=_dt(12, 5), timeline=1),
            _wal_item(3, modified=_dt(12, 5), timeline=2),
        ],
    )
    monkeypatch.setattr(pitr.secrets, "token_hex", lambda size: "e" * (size * 2))

    plan = plan_restore(
        ctx,
        "production",
        _dt(12),
        timeline=1,
        adapter=adapter,
        now=_dt(14),
    )

    assert plan.target_timeline == 1
    assert plan.first_wal == _wal_filename(3)
    assert plan.wal_count == 1


def test_plan_restore_fails_closed_on_wal_gap_without_persisting_plan(
    tmp_path,
):
    ctx = _context(tmp_path)
    base = _base_manifest(end_wal=_wal_filename(3))
    adapter = FakeWalAdapter(
        bases=[base],
        wal_items=[
            _wal_item(3, modified=_dt(11, 30)),
            _wal_item(5, modified=_dt(12, 5)),
        ],
    )

    with pytest.raises(PitrError, match=_wal_filename(4)):
        plan_restore(
            ctx,
            "production",
            _dt(12),
            adapter=adapter,
            now=_dt(14),
        )

    assert MetadataStore(ctx.project.state_dir).list_pitr_recovery_plans() == []
    assert not any(event.startswith("remote.verify_wal:") for event in adapter.events)


def test_plan_restore_rejects_changed_remote_wal_size(tmp_path):
    ctx = _context(tmp_path)
    base = _base_manifest(end_wal=_wal_filename(3))
    adapter = FakeWalAdapter(
        bases=[base],
        wal_items=[_wal_item(3, modified=_dt(12, 5))],
    )
    adapter.verify_size_overrides[_wal_filename(3)] = 5

    with pytest.raises(PitrError, match="Remote WAL size changed"):
        plan_restore(
            ctx,
            "production",
            _dt(12),
            adapter=adapter,
            now=_dt(14),
        )

    assert MetadataStore(ctx.project.state_dir).list_pitr_recovery_plans() == []


def test_plan_restore_reconciles_archive_receipt_with_remote_object_time(
    tmp_path,
):
    ctx = _context(tmp_path)
    filename = _wal_filename(3)
    source = tmp_path / filename
    source.write_bytes(b"wal!")
    digest = hashlib.sha256(b"wal!").hexdigest()
    remote_item = _wal_item(
        3,
        modified=_dt(12, 5),
        digest=digest,
    )
    archive_adapter = FakeWalAdapter()
    archive_adapter.archive_result = SimpleNamespace(
        filename=filename,
        key=remote_item.key,
        size=4,
        sha256=digest,
        idempotent=False,
    )
    archive_wal(
        ctx,
        "production",
        source,
        filename,
        postgres=FakePostgres(),
        adapter=archive_adapter,
        now=_dt(11),
    )
    base = _base_manifest(end_wal=filename)
    planning_adapter = FakeWalAdapter(
        bases=[base],
        wal_items=[remote_item],
    )

    plan = plan_restore(
        ctx,
        "production",
        _dt(12),
        adapter=planning_adapter,
        now=_dt(14),
    )

    assert plan.first_wal == filename


def _wal_receipt(
    sequence: int,
    *,
    archived_at: datetime | None = None,
    timeline: int = 1,
    size: int = 4,
    digest: str | None = None,
) -> WalReceipt:
    filename = _wal_filename(sequence, timeline=timeline)
    checksum = digest or hashlib.sha256(filename.encode()).hexdigest()
    return WalReceipt(
        project="demo project",
        environment="production",
        cluster_id=CLUSTER_ID,
        system_identifier=SYSTEM_ID,
        filename=filename,
        timeline=timeline,
        sha256=checksum,
        size=size,
        archived_at=(archived_at or _dt(12)).isoformat().replace("+00:00", "Z"),
        remote_uri=(
            "s3://demo-pitr/demo/postgres/clusters/"
            f"{CLUSTER_ID}/{SYSTEM_ID}/wal/{timeline:08X}/{filename}"
        ),
    )


def _recovery_plan(
    base: PitrBaseBackupManifest,
    *,
    plan_id: str = "production_pitr_plan_test",
    base_backup_id: str | None = None,
    status: str = "planned",
    first_sequence: int = 3,
    last_sequence: int = 4,
) -> PitrRecoveryPlan:
    wal_count = last_sequence - first_sequence + 1
    return PitrRecoveryPlan(
        plan_id=plan_id,
        project="demo project",
        environment="production",
        cluster_id=CLUSTER_ID,
        system_identifier=SYSTEM_ID,
        base_backup_id=base_backup_id or base.base_backup_id,
        database="odoo_prod",
        new_database="odoo_prod_pitr_test",
        target_time=_dt(12).isoformat().replace("+00:00", "Z"),
        target_timeline=1,
        first_wal=_wal_filename(first_sequence),
        last_wal=_wal_filename(last_sequence),
        wal_count=wal_count,
        wal_bytes=wal_count * 4,
        recovery_image=RECOVERY_IMAGE,
        status=status,
    )


def _seed_execution(
    ctx: ServiceContext,
    *,
    status: str = "planned",
) -> tuple[
    PitrBaseBackupManifest,
    PitrRecoveryPlan,
    FakeWalAdapter,
]:
    base = _base_manifest(end_wal=_wal_filename(3))
    plan = _recovery_plan(base, status=status)
    receipts = [
        _wal_receipt(3, archived_at=_dt(11, 30)),
        _wal_receipt(4, archived_at=_dt(12, 5)),
    ]
    store = MetadataStore(ctx.project.state_dir)
    store.save_pitr_base_manifest(base)
    store.save_pitr_recovery_plan(plan)
    for receipt in receipts:
        store.save_wal_receipt(receipt)
    adapter = FakeWalAdapter(
        bases=[base],
        wal_items=[
            _wal_item(3, modified=_dt(11, 30)),
            _wal_item(4, modified=_dt(12, 5)),
        ],
    )
    adapter.pins[("plan", plan.plan_id)] = adapter._pin(
        "plan",
        plan.plan_id,
        "production",
        base.base_backup_id,
        tuple(
            PitrPinnedWal(
                filename=receipt.filename,
                sha256=receipt.sha256,
                size=receipt.size,
            )
            for receipt in receipts
        ),
    )
    return base, plan, adapter


def test_execute_restore_recovers_into_new_database_and_never_swaps(
    tmp_path,
    monkeypatch,
):
    from odooctl.services import pitr

    ctx = _context(tmp_path)
    base, plan, adapter = _seed_execution(ctx)
    events = adapter.events
    runtime = FakeRuntime(events)
    db = FakeDb(events)
    monkeypatch.setattr(pitr.secrets, "token_hex", lambda size: "f" * (size * 2))

    metadata = execute_restore(
        ctx,
        "production",
        plan.plan_id,
        adapter=adapter,
        runtime=runtime,
        db_adapter=db,
    )

    assert metadata.status == "verified"
    assert metadata.verified is True
    assert metadata.cutover is False
    assert metadata.database == "odoo_prod"
    assert metadata.new_database == plan.new_database
    assert runtime.spec.postgres_image == RECOVERY_IMAGE
    assert runtime.spec.base_backup.inspection.system_identifier == SYSTEM_ID
    assert events == [
        f"remote.verify_base:{base.base_backup_id}",
        f"remote.download_base:{base.base_backup_id}",
        f"remote.download_wal:{_wal_filename(3)}",
        f"remote.download_wal:{_wal_filename(4)}",
        "runtime.recover_and_dump",
        f"db.exists:{plan.new_database}",
        f"db.create:{plan.new_database}",
        f"db.restore_into:{plan.new_database}",
        f"db.ping:{plan.new_database}",
        f"db.psql:{plan.new_database}",
    ]
    store = MetadataStore(ctx.project.state_dir)
    persisted = store.get_pitr_restore(metadata.restore_id)
    assert persisted.restore_id == metadata.restore_id
    assert persisted.status == "verified"
    assert persisted.verified is True
    assert persisted.recovered_at == _z(_dt(12))
    assert persisted.recovered_lsn == "0/04000000"
    assert store.get_pitr_recovery_plan(plan.plan_id).status == "verified"
    assert not list((ctx.project.state_dir / "pitr" / "restore-staging").glob(".partial-*"))


def test_execute_restore_runtime_failure_records_failure_without_database_or_swap(
    tmp_path,
    monkeypatch,
):
    from odooctl.services import pitr

    ctx = _context(tmp_path)
    _, plan, adapter = _seed_execution(ctx)
    events = adapter.events
    runtime = FakeRuntime(events, error=RuntimeError("recovery failed"))
    db = FakeDb(events)
    monkeypatch.setattr(pitr.secrets, "token_hex", lambda size: "1" * (size * 2))

    with pytest.raises(RuntimeError, match="recovery failed"):
        execute_restore(
            ctx,
            "production",
            plan.plan_id,
            adapter=adapter,
            runtime=runtime,
            db_adapter=db,
        )

    assert not any(event.startswith("db.") for event in events)
    store = MetadataStore(ctx.project.state_dir)
    restore = store.list_pitr_restores(environment="production")[0]
    assert restore.status == "failed"
    assert restore.verified is False
    assert restore.last_error == "recovery failed"
    assert store.get_pitr_recovery_plan(plan.plan_id).status == "failed"
    assert not list((ctx.project.state_dir / "pitr" / "restore-staging").glob(".partial-*"))


def test_execute_restore_verification_failure_drops_new_database_without_swap(
    tmp_path,
    monkeypatch,
):
    from odooctl.services import pitr

    ctx = _context(tmp_path)
    _, plan, adapter = _seed_execution(ctx)
    events = adapter.events
    runtime = FakeRuntime(events)
    db = FakeDb(events, ping_error=RuntimeError("database rejected"))
    monkeypatch.setattr(pitr.secrets, "token_hex", lambda size: "2" * (size * 2))

    with pytest.raises(RuntimeError, match="database rejected"):
        execute_restore(
            ctx,
            "production",
            plan.plan_id,
            adapter=adapter,
            runtime=runtime,
            db_adapter=db,
        )

    assert f"db.drop:{plan.new_database}" in events
    assert "db.swap" not in events
    store = MetadataStore(ctx.project.state_dir)
    assert store.list_pitr_restores()[0].status == "failed"
    assert store.get_pitr_recovery_plan(plan.plan_id).status == "failed"


def test_execute_restore_cleans_up_database_after_partial_restore_failure(
    tmp_path,
    monkeypatch,
):
    from odooctl.services import pitr

    ctx = _context(tmp_path)
    _, plan, adapter = _seed_execution(ctx)
    events = adapter.events
    runtime = FakeRuntime(events)
    db = FakeDb(events, restore_error=RuntimeError("pg_restore failed"))
    monkeypatch.setattr(pitr.secrets, "token_hex", lambda size: "3" * (size * 2))

    with pytest.raises(RuntimeError, match="pg_restore failed"):
        execute_restore(
            ctx,
            "production",
            plan.plan_id,
            adapter=adapter,
            runtime=runtime,
            db_adapter=db,
        )

    assert f"db.drop:{plan.new_database}" in events
    assert "db.swap" not in events


def test_execute_restore_refuses_preexisting_target_database_without_swap(
    tmp_path,
    monkeypatch,
):
    from odooctl.services import pitr

    ctx = _context(tmp_path)
    _, plan, adapter = _seed_execution(ctx)
    events = adapter.events
    runtime = FakeRuntime(events)
    db = FakeDb(events, existing=True)
    monkeypatch.setattr(pitr.secrets, "token_hex", lambda size: "4" * (size * 2))

    with pytest.raises(PitrError, match="already exists"):
        execute_restore(
            ctx,
            "production",
            plan.plan_id,
            adapter=adapter,
            runtime=runtime,
            db_adapter=db,
        )

    assert f"db.exists:{plan.new_database}" in events
    assert f"db.create:{plan.new_database}" not in events
    assert not any(event.startswith("db.restore_into:") for event in events)
    assert not any(event.startswith("db.drop:") for event in events)
    assert "db.swap" not in events


def test_execute_restore_fails_closed_if_target_appears_during_atomic_create(
    tmp_path,
    monkeypatch,
):
    from odooctl.services import pitr

    ctx = _context(tmp_path)
    _, plan, adapter = _seed_execution(ctx)
    events = adapter.events
    db = FakeDb(
        events,
        create_error=PitrError(f"PITR target database already exists: {plan.new_database}"),
    )
    monkeypatch.setattr(pitr.secrets, "token_hex", lambda size: "6" * (size * 2))

    with pytest.raises(PitrError, match="already exists"):
        execute_restore(
            ctx,
            "production",
            plan.plan_id,
            adapter=adapter,
            runtime=FakeRuntime(events),
            db_adapter=db,
        )

    assert events[-2:] == [
        f"db.exists:{plan.new_database}",
        f"db.create:{plan.new_database}",
    ]
    assert not any(event.startswith("db.restore_into:") for event in events)
    assert not any(event.startswith("db.drop:") for event in events)
    assert "db.swap" not in events


def test_execute_restore_staging_collision_does_not_transition_durable_state(
    tmp_path,
    monkeypatch,
):
    from odooctl.services import pitr

    ctx = _context(tmp_path)
    _, plan, adapter = _seed_execution(ctx)
    token = "5" * 24
    restore_id = f"production_pitr_restore_{token}"
    collision = ctx.project.state_dir / "pitr" / "restore-staging" / f".partial-{restore_id}"
    collision.mkdir(parents=True)
    monkeypatch.setattr(pitr.secrets, "token_hex", lambda size: token)

    with pytest.raises(PitrError, match="staging path exists"):
        execute_restore(
            ctx,
            "production",
            plan.plan_id,
            adapter=adapter,
            runtime=FakeRuntime(adapter.events),
            db_adapter=FakeDb(adapter.events),
        )

    store = MetadataStore(ctx.project.state_dir)
    assert store.get_pitr_recovery_plan(plan.plan_id).status == "planned"
    assert store.list_pitr_restores() == []
    assert collision.is_dir()


def _verified_restore(
    *,
    restore_id: str = "production_pitr_restore_verified",
    status: str = "verified",
    verified: bool = True,
) -> PitrRestoreMetadata:
    return PitrRestoreMetadata(
        restore_id=restore_id,
        plan_id="production_pitr_plan_test",
        base_backup_id="production_base_test",
        project="demo project",
        environment="production",
        cluster_id=CLUSTER_ID,
        system_identifier=SYSTEM_ID,
        database="odoo_prod",
        new_database="odoo_prod_pitr_test",
        target_time=_dt(12).isoformat().replace("+00:00", "Z"),
        target_timeline=1,
        recovered_at=_dt(12).isoformat().replace("+00:00", "Z"),
        recovered_lsn="0/04000000",
        verified=verified,
        status=status,
    )


@pytest.mark.parametrize(
    ("confirm_environment", "confirm_database", "accept_database_only"),
    [
        ("staging", "odoo_prod", True),
        ("production", "wrong_database", True),
        ("production", "odoo_prod", False),
    ],
)
def test_cutover_requires_all_typed_confirmations_before_database_access(
    tmp_path,
    monkeypatch,
    confirm_environment: str,
    confirm_database: str,
    accept_database_only: bool,
):
    ctx = _context(tmp_path)
    metadata = _verified_restore()
    MetadataStore(ctx.project.state_dir).save_pitr_restore(metadata)
    events: list[str] = []
    db = FakeDb(events)

    with pytest.raises(PitrError, match="exact environment/database"):
        cutover_restore(
            ctx,
            "production",
            metadata.restore_id,
            confirm_environment=confirm_environment,
            confirm_database=confirm_database,
            accept_database_only=accept_database_only,
            db_adapter=db,
        )

    assert events == []


def test_cutover_refuses_unverified_restore_before_database_access(
    tmp_path,
    monkeypatch,
):
    ctx = _context(tmp_path)
    metadata = _verified_restore(
        restore_id="production_pitr_restore_failed",
        status="failed",
        verified=False,
    )
    MetadataStore(ctx.project.state_dir).save_pitr_restore(metadata)
    events: list[str] = []
    db = FakeDb(events)

    with pytest.raises(PitrError, match="Only a verified"):
        cutover_restore(
            ctx,
            "production",
            metadata.restore_id,
            confirm_environment="production",
            confirm_database="odoo_prod",
            accept_database_only=True,
            db_adapter=db,
        )

    assert events == []


def test_cutover_records_identity_before_promotion_and_finalizes_after_durable_cutover(
    tmp_path,
    monkeypatch,
):
    from odooctl.services import pitr
    from odooctl.odoo.db_swap import DatabaseCutoverPlan

    ctx = _context(tmp_path)
    metadata = _verified_restore()
    store = MetadataStore(ctx.project.state_dir)
    store.save_pitr_restore(metadata)
    events: list[str] = []
    db = FakeDb(events)
    cutover_plan = DatabaseCutoverPlan(
        cutover_id=metadata.restore_id,
        incoming_db=metadata.new_database,
        target_db=metadata.database,
        aside_db=database_cutover_aside_name(
            target_db=metadata.database,
            incoming_db=metadata.new_database,
            cutover_id=metadata.restore_id,
        ),
        incoming_oid=101,
        target_oid=202,
    )

    def record_plan(db_adapter, **kwargs):
        assert db_adapter is db
        events.append("db.plan")
        return cutover_plan

    def record_promote(db_adapter, plan):
        assert db_adapter is db
        assert plan == cutover_plan
        durable = store.get_pitr_restore(metadata.restore_id)
        assert durable.cutover_aside_database == cutover_plan.aside_db
        assert durable.cutover_incoming_oid == cutover_plan.incoming_oid
        assert durable.cutover_started_at is not None
        assert durable.cutover is False
        events.append("db.promote")
        return SimpleNamespace(promoted=True)

    def record_finalize(db_adapter, plan, *, cutover_durably_recorded):
        assert db_adapter is db
        assert plan == cutover_plan
        assert cutover_durably_recorded is True
        durable = store.get_pitr_restore(metadata.restore_id)
        assert durable.cutover is True
        assert durable.status == "cutover"
        assert durable.cutover_completed_at is not None
        events.append("db.finalize")
        return SimpleNamespace(promoted=True)

    monkeypatch.setattr(pitr, "plan_database_cutover", record_plan)
    monkeypatch.setattr(pitr, "promote_database_cutover", record_promote)
    monkeypatch.setattr(pitr, "finalize_database_cutover", record_finalize)
    monkeypatch.setattr(
        pitr,
        "_release_restore_pin",
        lambda *args, **kwargs: events.append("pin.release"),
    )

    updated = cutover_restore(
        ctx,
        "production",
        metadata.restore_id,
        confirm_environment="production",
        confirm_database="odoo_prod",
        accept_database_only=True,
        db_adapter=db,
    )

    assert events == [
        f"db.ping:{metadata.new_database}",
        f"db.psql:{metadata.new_database}",
        "db.plan",
        "db.promote",
        "db.finalize",
        "pin.release",
    ]
    assert updated.status == "cutover"
    assert updated.verified is True
    assert updated.cutover is True
    assert updated.cutover_finalized is True
    assert store.get_pitr_restore(metadata.restore_id) == updated


def test_cutover_cleanup_failure_is_durable_and_retry_skips_promotion(
    tmp_path,
    monkeypatch,
):
    from odooctl.odoo.db_swap import (
        DatabaseCutoverCleanupError,
        DatabaseCutoverPlan,
    )
    from odooctl.services import pitr

    ctx = _context(tmp_path)
    metadata = _verified_restore()
    store = MetadataStore(ctx.project.state_dir)
    store.save_pitr_restore(metadata)
    db = FakeDb([])
    plan = DatabaseCutoverPlan(
        cutover_id=metadata.restore_id,
        incoming_db=metadata.new_database,
        target_db=metadata.database,
        aside_db=database_cutover_aside_name(
            target_db=metadata.database,
            incoming_db=metadata.new_database,
            cutover_id=metadata.restore_id,
        ),
        incoming_oid=101,
        target_oid=202,
    )
    monkeypatch.setattr(pitr, "plan_database_cutover", lambda *args, **kwargs: plan)
    monkeypatch.setattr(
        pitr,
        "promote_database_cutover",
        lambda *args, **kwargs: SimpleNamespace(promoted=True),
    )
    monkeypatch.setattr(
        pitr,
        "finalize_database_cutover",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            DatabaseCutoverCleanupError(
                "old database cleanup must be retried",
                SimpleNamespace(promoted=True),
            )
        ),
    )

    with pytest.raises(DatabaseCutoverCleanupError, match="must be retried"):
        cutover_restore(
            ctx,
            "production",
            metadata.restore_id,
            confirm_environment="production",
            confirm_database="odoo_prod",
            accept_database_only=True,
            db_adapter=db,
        )

    interrupted = store.get_pitr_restore(metadata.restore_id)
    assert interrupted.cutover is True
    assert interrupted.cutover_finalized is False
    assert interrupted.status == "cutover"
    assert "must be retried" in (interrupted.last_error or "")

    monkeypatch.setattr(
        pitr,
        "_verify_recovered_database",
        lambda *args, **kwargs: pytest.fail("retry must not access the old incoming name"),
    )
    monkeypatch.setattr(
        pitr,
        "plan_database_cutover",
        lambda *args, **kwargs: pytest.fail("retry must reuse durable cutover intent"),
    )
    monkeypatch.setattr(
        pitr,
        "promote_database_cutover",
        lambda *args, **kwargs: pytest.fail("retry must not promote twice"),
    )
    monkeypatch.setattr(
        pitr,
        "finalize_database_cutover",
        lambda *args, **kwargs: SimpleNamespace(promoted=True),
    )
    monkeypatch.setattr(pitr, "_release_restore_pin", lambda *args, **kwargs: None)

    completed = cutover_restore(
        ctx,
        "production",
        metadata.restore_id,
        confirm_environment="production",
        confirm_database="odoo_prod",
        accept_database_only=True,
        db_adapter=db,
    )

    assert completed.cutover is True
    assert completed.cutover_finalized is True
    assert completed.last_error is None


def test_finalized_cutover_releases_exact_remote_restore_pin(tmp_path):
    from odooctl.services import pitr

    ctx = _context(tmp_path)
    base, plan, adapter = _seed_execution(ctx)
    receipts = tuple(
        MetadataStore(ctx.project.state_dir).get_wal_receipt(
            CLUSTER_ID,
            SYSTEM_ID,
            filename,
        )
        for filename in wal_segment_range(
            plan.first_wal,
            plan.last_wal,
            base.wal_segment_size,
        )
    )
    metadata = _verified_restore(
        restore_id="production_pitr_restore_release",
    ).model_copy(
        update={
            "plan_id": plan.plan_id,
            "base_backup_id": base.base_backup_id,
        }
    )
    adapter.pins[("restore", metadata.restore_id)] = adapter._pin(
        "restore",
        metadata.restore_id,
        "production",
        base.base_backup_id,
        tuple(
            PitrPinnedWal(
                filename=receipt.filename,
                sha256=receipt.sha256,
                size=receipt.size,
            )
            for receipt in receipts
        ),
    )

    warning = pitr._release_restore_pin(
        ctx,
        "production",
        metadata,
        adapter=adapter,
    )

    assert warning is None
    assert ("restore", metadata.restore_id) not in adapter.pins
    assert adapter.coordination_events[-3:] == [
        "lease.renew",
        f"pin.release:restore:{metadata.restore_id}",
        "lease.release",
    ]


def _retention_bases() -> tuple[
    PitrBaseBackupManifest,
    PitrBaseBackupManifest,
    PitrBaseBackupManifest,
]:
    oldest = _base_manifest(
        "production_base_oldest",
        completed_at=_dt(9, day=27),
        start_wal=_wal_filename(1),
        end_wal=_wal_filename(3),
    )
    middle = _base_manifest(
        "production_base_middle",
        completed_at=_dt(9, day=28),
        start_wal=_wal_filename(3),
        end_wal=_wal_filename(5),
    )
    newest = _base_manifest(
        "production_base_newest",
        completed_at=_dt(9, day=29),
        start_wal=_wal_filename(5),
        end_wal=_wal_filename(7),
    )
    return oldest, middle, newest


def _retention_wal_items(
    *,
    first: int = 1,
    last: int = 9,
    missing: set[int] | None = None,
    timeline: int = 1,
) -> list[SimpleNamespace]:
    excluded = missing or set()
    return [
        _wal_item(
            sequence,
            modified=_dt(23, day=29),
            timeline=timeline,
        )
        for sequence in range(first, last + 1)
        if sequence not in excluded
    ]


def test_retention_deletes_only_bases_and_wal_before_retained_boundary(
    tmp_path,
):
    ctx = _context(tmp_path, base_backups=1, grace_hours=0)
    oldest, middle, newest = _retention_bases()
    adapter = FakeWalAdapter(
        bases=[middle, newest, oldest],
        wal_items=_retention_wal_items(),
    )

    result = reconcile_retention(
        ctx,
        "production",
        adapter=adapter,
        now=_dt(12),
    )

    assert result.retained_base_backup_ids == (newest.base_backup_id,)
    assert result.deleted_base_backup_ids == (
        oldest.base_backup_id,
        middle.base_backup_id,
    )
    expected_wal = tuple(_wal_filename(sequence) for sequence in range(1, 7))
    assert result.deleted_wal_filenames == expected_wal
    assert adapter.deleted_bases == [
        oldest.base_backup_id,
        middle.base_backup_id,
    ]
    assert adapter.deleted_wal == list(expected_wal)
    for base_id in adapter.deleted_bases:
        assert adapter.events.index(f"remote.verify_base:{base_id}") < adapter.events.index(
            f"remote.delete_base:{base_id}"
        )
    receipt_by_name = {item.key.rsplit("/", 1)[-1]: item for item in adapter.wal_items}
    assert adapter.verify_wal_expectations == [
        (
            _wal_filename(sequence),
            receipt_by_name[_wal_filename(sequence)].sha256,
        )
        for sequence in range(1, 10)
    ]
    for filename in adapter.deleted_wal:
        assert adapter.events.index(f"remote.verify_wal:{filename}") < adapter.events.index(
            f"remote.delete_wal:{filename}"
        )


@pytest.mark.parametrize("pin_kind", ["plan", "restore"])
def test_retention_pins_base_for_active_plan_or_restore(
    tmp_path,
    pin_kind: str,
):
    ctx = _context(tmp_path, base_backups=1, grace_hours=0)
    oldest, _, newest = _retention_bases()
    store = MetadataStore(ctx.project.state_dir)
    if pin_kind == "plan":
        store.save_pitr_recovery_plan(
            _recovery_plan(
                oldest,
                plan_id="production_pitr_plan_pin",
                first_sequence=3,
                last_sequence=3,
            )
        )
    else:
        store.save_pitr_restore(
            _verified_restore(restore_id="production_pitr_restore_pin").model_copy(
                update={"base_backup_id": oldest.base_backup_id}
            )
        )
    adapter = FakeWalAdapter(
        bases=[oldest, newest],
        wal_items=_retention_wal_items(),
    )

    result = reconcile_retention(
        ctx,
        "production",
        adapter=adapter,
        now=_dt(12),
    )

    assert set(result.retained_base_backup_ids) == {
        oldest.base_backup_id,
        newest.base_backup_id,
    }
    assert result.deleted_base_backup_ids == ()
    assert result.deleted_wal_filenames == (
        _wal_filename(1),
        _wal_filename(2),
    )


def test_retention_grace_window_keeps_recent_extra_base(tmp_path):
    ctx = _context(tmp_path, base_backups=1, grace_hours=24)
    older = _base_manifest(
        "production_base_recent_older",
        completed_at=_dt(10),
        start_wal=_wal_filename(1),
        end_wal=_wal_filename(3),
    )
    newest = _base_manifest(
        "production_base_recent_newer",
        completed_at=_dt(11),
        start_wal=_wal_filename(3),
        end_wal=_wal_filename(5),
    )
    adapter = FakeWalAdapter(
        bases=[older, newest],
        wal_items=_retention_wal_items(first=3),
    )

    result = reconcile_retention(
        ctx,
        "production",
        adapter=adapter,
        now=_dt(12),
    )

    assert set(result.retained_base_backup_ids) == {
        older.base_backup_id,
        newest.base_backup_id,
    }
    assert result.deleted_base_backup_ids == ()
    assert result.deleted_wal_filenames == ()


def test_retention_fails_closed_when_a_pinned_base_is_missing(tmp_path):
    ctx = _context(tmp_path, base_backups=1, grace_hours=0)
    _, _, newest = _retention_bases()
    missing_id = "production_base_missing"
    MetadataStore(ctx.project.state_dir).save_pitr_recovery_plan(
        _recovery_plan(
            newest,
            plan_id="production_pitr_plan_missing_pin",
            base_backup_id=missing_id,
            first_sequence=7,
            last_sequence=7,
        )
    )
    adapter = FakeWalAdapter(
        bases=[newest],
        wal_items=_retention_wal_items(first=7),
    )

    with pytest.raises(PitrError, match=missing_id):
        reconcile_retention(
            ctx,
            "production",
            adapter=adapter,
            now=_dt(12),
        )

    assert adapter.deleted_bases == []
    assert adapter.deleted_wal == []


def test_retention_refuses_mixed_retained_timelines_before_deletion(
    tmp_path,
):
    ctx = _context(tmp_path, base_backups=2, grace_hours=0)
    timeline_one = _base_manifest(
        "production_base_timeline_one",
        completed_at=_dt(9, day=28),
        start_wal=_wal_filename(3),
        end_wal=_wal_filename(5),
    )
    timeline_two = _base_manifest(
        "production_base_timeline_two",
        completed_at=_dt(9, day=29),
        timeline=2,
        start_wal=_wal_filename(5, timeline=2),
        end_wal=_wal_filename(7, timeline=2),
    )
    adapter = FakeWalAdapter(
        bases=[timeline_one, timeline_two],
        wal_items=[
            *_retention_wal_items(timeline=1),
            *_retention_wal_items(timeline=2),
        ],
    )

    with pytest.raises(PitrError, match="mixed WAL sizes or timelines"):
        reconcile_retention(
            ctx,
            "production",
            adapter=adapter,
            now=_dt(12),
        )

    assert adapter.deleted_bases == []
    assert adapter.deleted_wal == []


def test_retention_validates_every_base_candidate_before_first_delete(
    tmp_path,
):
    ctx = _context(tmp_path, base_backups=1, grace_hours=0)
    oldest, middle, newest = _retention_bases()
    adapter = FakeWalAdapter(
        bases=[oldest, middle, newest],
        wal_items=_retention_wal_items(),
    )
    adapter.verify_base_errors[middle.base_backup_id] = RuntimeError("candidate base corrupt")

    with pytest.raises(RuntimeError, match="candidate base corrupt"):
        reconcile_retention(
            ctx,
            "production",
            adapter=adapter,
            now=_dt(12),
        )

    assert adapter.deleted_bases == []
    assert adapter.deleted_wal == []


def test_retention_verifies_retained_base_before_deleting_last_good_base(
    tmp_path,
):
    ctx = _context(tmp_path, base_backups=1, grace_hours=0)
    oldest, _, newest = _retention_bases()
    adapter = FakeWalAdapter(
        bases=[oldest, newest],
        wal_items=_retention_wal_items(),
    )
    adapter.verify_base_errors[newest.base_backup_id] = RuntimeError("retained base corrupt")

    with pytest.raises(RuntimeError, match="retained base corrupt"):
        reconcile_retention(
            ctx,
            "production",
            adapter=adapter,
            now=_dt(12),
        )

    assert adapter.deleted_bases == []
    assert adapter.deleted_wal == []


def test_retention_validates_all_wal_bytes_before_any_delete(tmp_path):
    ctx = _context(tmp_path, base_backups=1, grace_hours=0)
    oldest, _, newest = _retention_bases()
    adapter = FakeWalAdapter(
        bases=[oldest, newest],
        wal_items=_retention_wal_items(),
    )
    adapter.verify_wal_errors[_wal_filename(2)] = RuntimeError("WAL candidate corrupt")

    with pytest.raises(RuntimeError, match="WAL candidate corrupt"):
        reconcile_retention(
            ctx,
            "production",
            adapter=adapter,
            now=_dt(12),
        )

    assert adapter.deleted_bases == []
    assert adapter.deleted_wal == []


def test_retention_rejects_gap_in_retained_recovery_graph_before_delete(
    tmp_path,
):
    ctx = _context(tmp_path, base_backups=1, grace_hours=0)
    oldest, _, newest = _retention_bases()
    adapter = FakeWalAdapter(
        bases=[oldest, newest],
        wal_items=_retention_wal_items(missing={8}),
    )

    with pytest.raises(PitrError, match="contiguous|gap"):
        reconcile_retention(
            ctx,
            "production",
            adapter=adapter,
            now=_dt(12),
        )

    assert adapter.deleted_bases == []
    assert adapter.deleted_wal == []


def test_retention_byte_verifies_wal_needed_by_retained_base_before_delete(
    tmp_path,
):
    ctx = _context(tmp_path, base_backups=1, grace_hours=0)
    oldest, _, newest = _retention_bases()
    adapter = FakeWalAdapter(
        bases=[oldest, newest],
        wal_items=_retention_wal_items(),
    )
    adapter.verify_wal_errors[_wal_filename(8)] = RuntimeError("retained WAL corrupt")

    with pytest.raises(RuntimeError, match="retained WAL corrupt"):
        reconcile_retention(
            ctx,
            "production",
            adapter=adapter,
            now=_dt(12),
        )

    assert adapter.deleted_bases == []
    assert adapter.deleted_wal == []


def test_retention_rejects_untrusted_remote_wal_time_before_delete(tmp_path):
    ctx = _context(tmp_path, base_backups=1, grace_hours=0)
    oldest, _, newest = _retention_bases()
    wal_items = _retention_wal_items()
    wal_items[0].last_modified = datetime(2026, 7, 29, 23)
    adapter = FakeWalAdapter(
        bases=[oldest, newest],
        wal_items=wal_items,
    )

    with pytest.raises(PitrError, match="authoritative modification time"):
        reconcile_retention(
            ctx,
            "production",
            adapter=adapter,
            now=_dt(12),
        )

    assert adapter.deleted_bases == []
    assert adapter.deleted_wal == []
