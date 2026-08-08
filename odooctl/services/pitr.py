"""PostgreSQL WAL archiving and point-in-time recovery services.

PITR is intentionally separate from portable Odoo backups.  A physical base
backup and its WAL graph protect PostgreSQL only; the Odoo filestore is never
silently treated as part of the recovery point.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shlex
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from odooctl.adapters.db import make_db_adapter as make_context_db_adapter
from odooctl.adapters.pitr_postgres import (
    PhysicalBaseBackup,
    PitrPostgresAdapter,
    PostgresPitrInspection,
)
from odooctl.adapters.pitr_runtime import (
    DockerPitrRuntime,
    PitrRecoverySpec,
)
from odooctl.adapters.wal_s3 import (
    PitrCoordinationLease,
    PitrPinnedWal,
    PitrRemotePin,
    WalS3Adapter,
)
from odooctl.metadata.models import (
    PitrBaseBackupManifest,
    PitrRecoveryPlan,
    PitrRestoreMetadata,
    WalReceipt,
)
from odooctl.metadata.store import MetadataStore
from odooctl.odoo.db_swap import (
    DatabaseCutoverError,
    DatabaseCutoverPlan,
    finalize_database_cutover,
    plan_database_cutover,
    promote_database_cutover,
)
from odooctl.utils.shell import redact

if TYPE_CHECKING:
    from odooctl.services.context import ServiceContext


MAX_PITR_WAL_SEGMENTS = 100_000


class PitrError(RuntimeError):
    """PITR configuration, continuity, or recovery failed closed."""


@dataclass(frozen=True)
class PitrCheckResult:
    environment: str
    cluster_id: str
    system_identifier: str
    postgres_major: int
    timeline: int
    wal_segment_size: int
    archive_mode: str
    archive_command_configured: bool
    recovery_image: str
    remote_wal_objects: int


@dataclass(frozen=True)
class PitrRetentionResult:
    retained_base_backup_ids: tuple[str, ...]
    deleted_base_backup_ids: tuple[str, ...]
    deleted_wal_filenames: tuple[str, ...]


class _CoordinationGuard:
    """Scope-wide remote mutex with fail-safe release semantics."""

    def __init__(
        self,
        client: WalS3Adapter,
        *,
        purpose: str,
        owner: str,
    ):
        self.client = client
        self.lease = client.acquire_coordination_lease(
            purpose=purpose,
            owner=owner,
            ttl_seconds=3600,
        )

    def __enter__(self) -> "_CoordinationGuard":
        return self

    def renew(self) -> None:
        self.lease = self.client.renew_coordination_lease(
            self.lease
        )

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            self.client.release_coordination_lease(self.lease)
        except Exception as release_error:
            if exc is not None:
                exc.add_note(
                    "PITR coordination lease release also failed: "
                    + redact(str(release_error))
                )
                return False
            raise
        return False


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    current = value or _now_utc()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("PITR timestamps must be timezone-aware")
    return (
        current.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_utc(value: str | datetime, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(
                value.replace("Z", "+00:00")
            )
        except ValueError:
            raise ValueError(
                f"{label} must be an RFC3339 timestamp"
            ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _same_wal_object(left: WalReceipt, right: WalReceipt) -> bool:
    """Compare the immutable identity of two receipts.

    ``archived_at`` is deliberately excluded: the local publisher timestamp
    and the provider's authoritative object timestamp describe different
    events. Neither is allowed to change the object's identity or bytes.
    """

    fields = (
        "schema_version",
        "project",
        "environment",
        "cluster_id",
        "system_identifier",
        "filename",
        "timeline",
        "sha256",
        "size",
        "remote_uri",
        "status",
    )
    return all(
        getattr(left, field) == getattr(right, field)
        for field in fields
    )


def _validated_update(model, **updates):
    payload = model.model_dump(mode="python")
    payload.update(updates)
    return type(model).model_validate(payload)


def _pin_segments(
    receipts: list[WalReceipt] | tuple[WalReceipt, ...],
) -> tuple[PitrPinnedWal, ...]:
    return tuple(
        PitrPinnedWal(
            filename=receipt.filename,
            sha256=receipt.sha256,
            size=receipt.size,
        )
        for receipt in receipts
    )


def _assert_remote_pin(
    pin: PitrRemotePin,
    *,
    kind: str,
    pin_id: str,
    environment: str,
    base_backup_id: str,
    receipts: list[WalReceipt] | tuple[WalReceipt, ...],
) -> None:
    if (
        pin.kind != kind
        or pin.pin_id != pin_id
        or pin.environment != environment
        or pin.base_backup_id != base_backup_id
        or pin.wal_segments != _pin_segments(receipts)
    ):
        raise PitrError(
            f"Remote PITR {kind} pin does not match durable recovery state"
        )


def _pitr_config(ctx: "ServiceContext", environment: str):
    config = ctx.project.config
    pitr = config.pitr
    if not pitr.enabled:
        raise PitrError("PITR is disabled; set pitr.enabled: true")
    config.env(environment)
    if environment != pitr.environment:
        raise PitrError(
            f"PITR is bound to environment {pitr.environment!r}, "
            f"not {environment!r}"
        )
    if pitr.destination is None or not pitr.cluster_id:
        raise PitrError("PITR destination and cluster identity are incomplete")
    if pitr.filestore_policy != "database_only":
        raise PitrError(
            "PITR requires the explicit database_only filestore policy"
        )
    return pitr


def _source_postgres_adapter(
    ctx: "ServiceContext",
    environment: str,
) -> PitrPostgresAdapter:
    pitr = _pitr_config(ctx, environment)
    postgres = ctx.project.config.postgres
    source_config = postgres.model_copy(
        update={
            "user": pitr.replication_user or postgres.user,
            "password_env": (
                pitr.replication_password_env
                or postgres.password_env
            ),
        }
    )
    return PitrPostgresAdapter(source_config)


def _inspect_source(
    ctx: "ServiceContext",
    environment: str,
    *,
    postgres: PitrPostgresAdapter | None = None,
) -> PostgresPitrInspection:
    pitr = _pitr_config(ctx, environment)
    inspection = (
        postgres or _source_postgres_adapter(ctx, environment)
    ).inspect()
    pinned_identifier = getattr(pitr, "system_identifier", None)
    if (
        pinned_identifier is not None
        and str(pinned_identifier) != inspection.system_identifier
    ):
        raise PitrError(
            "Live PostgreSQL system identifier does not match "
            "pitr.system_identifier"
        )
    if inspection.server_major < 13:
        raise PitrError(
            "PITR base verification requires PostgreSQL 13 or newer"
        )
    if not inspection.archiving_configured:
        raise PitrError(
            "PostgreSQL archive_mode and archive_command/archive_library "
            "must be enabled before PITR can succeed"
        )
    if inspection.tablespaces:
        names = ", ".join(
            tablespace.name for tablespace in inspection.tablespaces
        )
        raise PitrError(
            "PITR v1 refuses non-default PostgreSQL tablespaces: "
            f"{names}"
        )
    return inspection


def _stored_system_identifiers(
    ctx: "ServiceContext",
    environment: str,
) -> set[str]:
    return {
        manifest.system_identifier
        for manifest in MetadataStore(
            ctx.project.state_dir
        ).list_pitr_base_manifests(environment=environment)
    }


def _resolve_system_identifier(
    ctx: "ServiceContext",
    environment: str,
    *,
    inspection: PostgresPitrInspection | None = None,
) -> str:
    pitr = _pitr_config(ctx, environment)
    pinned = getattr(pitr, "system_identifier", None)
    if inspection is not None:
        if pinned is not None and str(pinned) != inspection.system_identifier:
            raise PitrError(
                "Live PostgreSQL system identifier does not match "
                "pitr.system_identifier"
            )
        return inspection.system_identifier
    if pinned is not None:
        return str(pinned)
    identifiers = _stored_system_identifiers(ctx, environment)
    if len(identifiers) == 1:
        return next(iter(identifiers))
    if len(identifiers) > 1:
        raise PitrError(
            "Multiple PITR system identifiers are indexed; set "
            "pitr.system_identifier explicitly"
        )
    try:
        return _inspect_source(ctx, environment).system_identifier
    except Exception as exc:
        raise PitrError(
            "Cannot resolve the PostgreSQL system identifier. Configure "
            "pitr.system_identifier or restore PITR metadata before recovery."
        ) from exc


def make_wal_adapter(
    ctx: "ServiceContext",
    environment: str,
    system_identifier: str,
) -> WalS3Adapter:
    pitr = _pitr_config(ctx, environment)
    assert pitr.destination is not None
    assert pitr.cluster_id is not None
    return WalS3Adapter(
        pitr.destination,
        project=ctx.project.config.project.name,
        cluster_id=pitr.cluster_id,
        system_identifier=system_identifier,
    )


def inspect_coordination_lease(
    ctx: "ServiceContext",
    environment: str,
    *,
    adapter: WalS3Adapter | None = None,
) -> PitrCoordinationLease | None:
    """Inspect the scope-wide PITR mutation lease without changing it."""

    _pitr_config(ctx, environment)
    identity = _resolve_system_identifier(ctx, environment)
    client = adapter or make_wal_adapter(ctx, environment, identity)
    return client.inspect_coordination_lease()


def recover_expired_coordination_lease(
    ctx: "ServiceContext",
    environment: str,
    *,
    confirm_lease_id: str,
    confirm_owner: str,
    confirm_purpose: str,
    confirm_owner_stopped: str,
    adapter: WalS3Adapter | None = None,
) -> PitrCoordinationLease:
    """Remove only one expired lease with exact operator attestations."""

    _pitr_config(ctx, environment)
    identity = _resolve_system_identifier(ctx, environment)
    client = adapter or make_wal_adapter(ctx, environment, identity)
    return client.recover_expired_coordination_lease(
        confirm_lease_id=confirm_lease_id,
        confirm_owner=confirm_owner,
        confirm_purpose=confirm_purpose,
        confirm_owner_stopped=confirm_owner_stopped,
    )


def archive_command(
    ctx: "ServiceContext",
    environment: str,
    *,
    odooctl_bin: str = "odooctl",
) -> str:
    """Render a secret-free PostgreSQL ``archive_command`` value."""

    _pitr_config(ctx, environment)

    def literal(value: str) -> str:
        # PostgreSQL expands %p, %f, and %% across the whole command before
        # invoking the shell. Escape percent characters in operator-controlled
        # literal paths while leaving the two intended placeholders below.
        return shlex.quote(value.replace("%", "%%"))

    executable = literal(odooctl_bin)
    project_root = literal(str(ctx.project.root))
    config_path = literal(str(ctx.project.config_path))
    env_value = literal(environment)
    return (
        f"{executable} --project-dir {project_root} "
        f"pitr wal push {env_value} --path \"%p\" --name \"%f\" "
        f"--config {config_path}"
    )


def check_pitr(
    ctx: "ServiceContext",
    environment: str,
    *,
    postgres: PitrPostgresAdapter | None = None,
    adapter: WalS3Adapter | None = None,
) -> PitrCheckResult:
    pitr = _pitr_config(ctx, environment)
    inspection = _inspect_source(
        ctx,
        environment,
        postgres=postgres,
    )
    if not pitr.recovery_image:
        raise PitrError(
            "pitr.recovery_image must pin the PostgreSQL image used for "
            "isolated recovery"
        )
    client = adapter or make_wal_adapter(
        ctx,
        environment,
        inspection.system_identifier,
    )
    remote_wal = client.list_wal()
    return PitrCheckResult(
        environment=environment,
        cluster_id=str(pitr.cluster_id),
        system_identifier=inspection.system_identifier,
        postgres_major=inspection.server_major,
        timeline=inspection.timeline,
        wal_segment_size=inspection.wal_segment_size_bytes,
        archive_mode=inspection.archive_mode,
        archive_command_configured=inspection.archiving_configured,
        recovery_image=pitr.recovery_image,
        remote_wal_objects=len(remote_wal),
    )


def archive_wal(
    ctx: "ServiceContext",
    environment: str,
    source: str | Path,
    filename: str,
    *,
    postgres: PitrPostgresAdapter | None = None,
    adapter: WalS3Adapter | None = None,
    now: datetime | None = None,
) -> WalReceipt:
    pitr = _pitr_config(ctx, environment)
    source_path = Path(source)
    if (
        not source_path.is_file()
        or source_path.is_symlink()
        or source_path.name != filename
    ):
        raise PitrError(
            "WAL archive source must be a regular file whose basename "
            "matches --name"
        )
    inspection = _inspect_source(
        ctx,
        environment,
        postgres=postgres,
    )
    client = adapter or make_wal_adapter(
        ctx,
        environment,
        inspection.system_identifier,
    )
    archived = client.archive_wal(source_path, filename)
    assert pitr.destination is not None
    receipt = WalReceipt(
        project=ctx.project.config.project.name,
        environment=environment,
        cluster_id=str(pitr.cluster_id),
        system_identifier=inspection.system_identifier,
        filename=archived.filename,
        timeline=int(archived.filename[:8], 16),
        sha256=archived.sha256,
        size=archived.size,
        archived_at=_timestamp(now),
        remote_uri=(
            f"s3://{pitr.destination.bucket}/{archived.key}"
        ),
    )
    store = MetadataStore(ctx.project.state_dir)
    existing = store.find_wal_receipt(
        receipt.cluster_id,
        receipt.system_identifier,
        receipt.filename,
    )
    if existing is not None:
        if not _same_wal_object(receipt, existing):
            raise PitrError(
                "Archived WAL conflicts with its immutable local receipt "
                f"for {receipt.filename!r}"
            )
        return existing
    store.save_wal_receipt(receipt)
    return receipt


def _segments_per_wal_log(wal_segment_size: int) -> int:
    if (
        isinstance(wal_segment_size, bool)
        or not isinstance(wal_segment_size, int)
        or wal_segment_size < 1024 * 1024
        or wal_segment_size > 1024 * 1024 * 1024
        or wal_segment_size & (wal_segment_size - 1)
        or 0x100000000 % wal_segment_size
    ):
        raise ValueError("invalid WAL segment size")
    return 0x100000000 // wal_segment_size


def wal_name_for_lsn(
    lsn: str,
    timeline: int,
    wal_segment_size: int,
) -> str:
    """Equivalent of PostgreSQL ``pg_walfile_name`` for one LSN."""

    if (
        isinstance(timeline, bool)
        or not isinstance(timeline, int)
        or not 1 <= timeline <= 0xFFFFFFFF
    ):
        raise ValueError("invalid timeline or WAL segment size")
    segments_per_log = _segments_per_wal_log(wal_segment_size)
    try:
        upper, lower = lsn.split("/", 1)
        upper_value = int(upper, 16)
        lower_value = int(lower, 16)
        if not (0 <= upper_value <= 0xFFFFFFFF):
            raise ValueError
        if not (0 <= lower_value <= 0xFFFFFFFF):
            raise ValueError
        position = (upper_value << 32) | lower_value
    except (ValueError, TypeError):
        raise ValueError(f"invalid PostgreSQL LSN: {lsn!r}") from None
    segment_number = position // wal_segment_size
    log = segment_number // segments_per_log
    segment = segment_number % segments_per_log
    return f"{timeline:08X}{log:08X}{segment:08X}"


def wal_sequence(filename: str, wal_segment_size: int) -> int:
    WalS3Adapter.validate_archive_name(filename)
    if len(filename) != 24:
        raise ValueError("WAL continuity applies only to segment filenames")
    segments_per_log = _segments_per_wal_log(wal_segment_size)
    if int(filename[:8], 16) < 1:
        raise ValueError("invalid WAL timeline")
    if int(filename[16:24], 16) >= segments_per_log:
        raise ValueError("invalid WAL segment filename for segment size")
    return (
        int(filename[8:16], 16) * segments_per_log
        + int(filename[16:24], 16)
    )


def wal_name_for_sequence(
    sequence: int,
    timeline: int,
    wal_segment_size: int,
) -> str:
    segments_per_log = _segments_per_wal_log(wal_segment_size)
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 0
        or sequence >= 0x100000000 * segments_per_log
        or isinstance(timeline, bool)
        or not isinstance(timeline, int)
        or not 1 <= timeline <= 0xFFFFFFFF
    ):
        raise ValueError("invalid WAL sequence or timeline")
    log, segment = divmod(sequence, segments_per_log)
    return f"{timeline:08X}{log:08X}{segment:08X}"


def wal_segment_range(
    first: str,
    last: str,
    wal_segment_size: int,
) -> tuple[str, ...]:
    if len(first) != 24 or len(last) != 24:
        raise ValueError("WAL range boundaries must be segment names")
    first_timeline = int(first[:8], 16)
    last_timeline = int(last[:8], 16)
    if first_timeline != last_timeline:
        raise PitrError(
            "A single PITR WAL range cannot cross timelines implicitly"
        )
    start = wal_sequence(first, wal_segment_size)
    end = wal_sequence(last, wal_segment_size)
    if end < start:
        raise PitrError("PITR WAL range ends before it starts")
    if end - start + 1 > MAX_PITR_WAL_SEGMENTS:
        raise PitrError(
            "PITR WAL range exceeds the safe segment-count limit"
        )
    return tuple(
        wal_name_for_sequence(
            sequence,
            first_timeline,
            wal_segment_size,
        )
        for sequence in range(start, end + 1)
    )


def _backup_wal_range(
    backup: PhysicalBaseBackup,
) -> tuple[str, str]:
    manifest_path = backup.pgdata / "backup_manifest"
    try:
        raw = json.loads(manifest_path.read_text())
        ranges = raw["WAL-Ranges"]
        if (
            not isinstance(ranges, list)
            or len(ranges) != 1
            or not isinstance(ranges[0], dict)
        ):
            raise TypeError
        selected = ranges[0]
        if int(selected.get("Timeline", -1)) != backup.inspection.timeline:
            raise ValueError
        start_lsn = str(selected["Start-LSN"])
        end_lsn = str(selected["End-LSN"])
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        raise PitrError(
            "PostgreSQL backup_manifest has no unambiguous WAL range"
        ) from None
    # Validate and normalize by deriving the segment names now.
    wal_name_for_lsn(
        start_lsn,
        backup.inspection.timeline,
        backup.inspection.wal_segment_size_bytes,
    )
    wal_name_for_lsn(
        end_lsn,
        backup.inspection.timeline,
        backup.inspection.wal_segment_size_bytes,
    )
    return start_lsn, end_lsn


def _artifact_inventory(
    root: Path,
) -> tuple[list[str], dict[str, str], dict[str, int]]:
    paths: list[str] = []
    checksums: dict[str, str] = {}
    sizes: dict[str, int] = {}
    for path in sorted(
        root.rglob("*"),
        key=lambda candidate: candidate.relative_to(root).as_posix(),
    ):
        if path.is_symlink():
            raise PitrError(
                f"Physical base backup contains a symbolic link: {path}"
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise PitrError(
                f"Physical base backup contains a non-regular file: {path}"
            )
        relative = path.relative_to(root).as_posix()
        if relative == "manifest.json":
            continue
        paths.append(relative)
        checksums[relative] = _sha256_file(path)
        sizes[relative] = path.stat().st_size
    if not paths:
        raise PitrError("Physical base backup contains no artifacts")
    return paths, checksums, sizes


def _base_id(environment: str, now: datetime | None = None) -> str:
    instant = (now or _now_utc()).astimezone(timezone.utc)
    return (
        f"{environment}_base_{instant:%Y%m%dT%H%M%S}_"
        f"{secrets.token_hex(12)}"
    )


def create_base_backup(
    ctx: "ServiceContext",
    environment: str,
    *,
    postgres: PitrPostgresAdapter | None = None,
    adapter: WalS3Adapter | None = None,
    now: datetime | None = None,
) -> PitrBaseBackupManifest:
    pitr = _pitr_config(ctx, environment)
    if not pitr.recovery_image:
        raise PitrError(
            "pitr.recovery_image must pin the PostgreSQL image before "
            "creating a physical base backup"
        )
    source = postgres or _source_postgres_adapter(ctx, environment)
    inspection = _inspect_source(
        ctx,
        environment,
        postgres=source,
    )
    client = adapter or make_wal_adapter(
        ctx,
        environment,
        inspection.system_identifier,
    )
    started = now or _now_utc()
    base_backup_id = _base_id(environment, started)
    pitr_root = ctx.project.state_dir / "pitr" / "staging"
    pitr_root.mkdir(parents=True, exist_ok=True)
    staging = pitr_root / f".partial-{base_backup_id}"
    if os.path.lexists(staging):
        raise PitrError(f"PITR staging path already exists: {staging}")
    staging.mkdir(mode=0o700)
    try:
        physical = source.create_base_backup(
            staging / "pgdata",
            inspection=inspection,
            label=base_backup_id,
        )
        start_lsn, end_lsn = _backup_wal_range(physical)
        start_wal = wal_name_for_lsn(
            start_lsn,
            inspection.timeline,
            inspection.wal_segment_size_bytes,
        )
        end_wal = wal_name_for_lsn(
            end_lsn,
            inspection.timeline,
            inspection.wal_segment_size_bytes,
        )
        # A base is not advertised until the independent archive proves its
        # ending segment exists and its actual bytes match its checksum.
        client.verify_wal(end_wal)
        artifacts, checksums, sizes = _artifact_inventory(staging)
        completed = _now_utc()
        assert pitr.destination is not None
        remote_uri = (
            f"s3://{pitr.destination.bucket}/"
            f"{client.base_prefix(base_backup_id)}"
        )
        manifest = PitrBaseBackupManifest(
            base_backup_id=base_backup_id,
            project=ctx.project.config.project.name,
            environment=environment,
            cluster_id=str(pitr.cluster_id),
            system_identifier=inspection.system_identifier,
            postgres_major=inspection.server_major,
            postgres_image=pitr.recovery_image,
            timeline=inspection.timeline,
            wal_segment_size=inspection.wal_segment_size_bytes,
            started_at=_timestamp(started),
            completed_at=_timestamp(completed),
            start_lsn=start_lsn,
            end_lsn=end_lsn,
            start_wal=start_wal,
            end_wal=end_wal,
            artifact_paths=artifacts,
            checksums=checksums,
            sizes=sizes,
            remote_uri=remote_uri,
            status="complete",
            verified_at=_timestamp(completed),
        )
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(manifest.model_dump_json(indent=2))
        with manifest_path.open("rb") as handle:
            os.fsync(handle.fileno())
        receipt = client.upload_base_backup(
            base_backup_id,
            staging,
        )
        if receipt.uri != remote_uri:
            raise PitrError(
                "Remote base-backup URI does not match the completion manifest"
            )
        MetadataStore(
            ctx.project.state_dir
        ).save_pitr_base_manifest(manifest)
        return manifest
    finally:
        if staging.is_dir() and not staging.is_symlink():
            shutil.rmtree(staging)


def _validate_manifest_ownership(
    ctx: "ServiceContext",
    environment: str,
    system_identifier: str,
    raw: object,
) -> PitrBaseBackupManifest:
    manifest = PitrBaseBackupManifest.model_validate(raw)
    pitr = _pitr_config(ctx, environment)
    expected = (
        ctx.project.config.project.name,
        environment,
        str(pitr.cluster_id),
        system_identifier,
    )
    actual = (
        manifest.project,
        manifest.environment,
        manifest.cluster_id,
        manifest.system_identifier,
    )
    if actual != expected:
        raise PitrError(
            "Remote PITR base backup does not belong to this "
            "project/environment/cluster"
        )
    if manifest.status != "complete":
        raise PitrError("Remote PITR base backup is not complete")
    return manifest


def list_base_backups(
    ctx: "ServiceContext",
    environment: str,
    *,
    adapter: WalS3Adapter | None = None,
    system_identifier: str | None = None,
) -> list[PitrBaseBackupManifest]:
    identity = system_identifier or _resolve_system_identifier(
        ctx,
        environment,
    )
    client = adapter or make_wal_adapter(
        ctx,
        environment,
        identity,
    )
    store = MetadataStore(ctx.project.state_dir)
    manifests: list[PitrBaseBackupManifest] = []
    for base_backup_id in client.list_base_backups():
        raw = client.read_base_manifest(base_backup_id)
        manifest = _validate_manifest_ownership(
            ctx,
            environment,
            identity,
            raw,
        )
        store.save_pitr_base_manifest(manifest)
        manifests.append(manifest)
    return sorted(
        manifests,
        key=lambda item: (
            item.completed_at or item.started_at,
            item.base_backup_id,
        ),
        reverse=True,
    )


def verify_base_backup(
    ctx: "ServiceContext",
    environment: str,
    base_backup_id: str,
    *,
    adapter: WalS3Adapter | None = None,
    system_identifier: str | None = None,
) -> PitrBaseBackupManifest:
    identity = system_identifier or _resolve_system_identifier(
        ctx,
        environment,
    )
    client = adapter or make_wal_adapter(
        ctx,
        environment,
        identity,
    )
    raw = client.read_base_manifest(base_backup_id)
    manifest = _validate_manifest_ownership(
        ctx,
        environment,
        identity,
        raw,
    )
    client.verify_base_backup(base_backup_id)
    MetadataStore(
        ctx.project.state_dir
    ).save_pitr_base_manifest(manifest)
    return manifest


def _remote_wal_receipts(
    ctx: "ServiceContext",
    environment: str,
    system_identifier: str,
    adapter: WalS3Adapter,
) -> list[WalReceipt]:
    pitr = _pitr_config(ctx, environment)
    assert pitr.destination is not None
    receipts: list[WalReceipt] = []
    store = MetadataStore(ctx.project.state_dir)
    for item in adapter.list_wal():
        filename = item.key.rsplit("/", 1)[-1]
        if len(filename) != 24:
            continue
        modified = item.last_modified
        if (
            not isinstance(modified, datetime)
            or modified.tzinfo is None
            or modified.utcoffset() is None
        ):
            raise PitrError(
                f"Remote WAL {filename} has no authoritative modification time"
            )
        receipt = WalReceipt(
            project=ctx.project.config.project.name,
            environment=environment,
            cluster_id=str(pitr.cluster_id),
            system_identifier=system_identifier,
            filename=filename,
            timeline=int(filename[:8], 16),
            sha256=item.sha256,
            size=item.size,
            archived_at=_timestamp(modified),
            remote_uri=(
                f"s3://{pitr.destination.bucket}/{item.key}"
            ),
        )
        existing = store.find_wal_receipt(
            receipt.cluster_id,
            receipt.system_identifier,
            receipt.filename,
        )
        if existing is None:
            store.save_wal_receipt(receipt)
        elif not _same_wal_object(receipt, existing):
            raise PitrError(
                "Remote WAL conflicts with its immutable local receipt "
                f"for {receipt.filename!r}"
            )
        receipts.append(receipt)
    return receipts


def _new_database_name(database: str, token: str) -> str:
    suffix = f"_pitr_{token[:8]}"
    prefix = database[: 63 - len(suffix)]
    if not prefix:
        raise PitrError("Live database name leaves no room for a PITR suffix")
    return prefix + suffix


def plan_restore(
    ctx: "ServiceContext",
    environment: str,
    target_time: str | datetime,
    *,
    timeline: int | None = None,
    base_backup_id: str | None = None,
    adapter: WalS3Adapter | None = None,
    now: datetime | None = None,
) -> PitrRecoveryPlan:
    pitr = _pitr_config(ctx, environment)
    target = _parse_utc(target_time, "target_time")
    current = now or _now_utc()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if target > current.astimezone(timezone.utc):
        raise PitrError("PITR target_time cannot be in the future")

    identity = _resolve_system_identifier(ctx, environment)
    client = adapter or make_wal_adapter(
        ctx,
        environment,
        identity,
    )
    token = secrets.token_hex(12)
    plan_id = f"{environment}_pitr_plan_{token}"
    with _CoordinationGuard(
        client,
        purpose="pitr-plan",
        owner=plan_id,
    ) as coordination:
        bases = list_base_backups(
            ctx,
            environment,
            adapter=client,
            system_identifier=identity,
        )
        eligible = [
            manifest
            for manifest in bases
            if manifest.status == "complete"
            and manifest.completed_at is not None
            and _parse_utc(manifest.completed_at, "completed_at")
            <= target
            and (
                base_backup_id is None
                or manifest.base_backup_id == base_backup_id
            )
        ]
        if not eligible:
            raise PitrError(
                "No complete PITR base backup precedes the requested target"
            )
        base = max(
            eligible,
            key=lambda item: (
                _parse_utc(
                    item.completed_at or item.started_at,
                    "completed_at",
                ),
                item.base_backup_id,
            ),
        )
        coordination.renew()
        client.verify_base_backup(base.base_backup_id)

        receipts = _remote_wal_receipts(
            ctx,
            environment,
            identity,
            client,
        )
        timelines = {receipt.timeline for receipt in receipts}
        if timeline is None:
            if len(timelines) != 1:
                raise PitrError(
                    "PITR target timeline must be explicit when archive "
                    "inventory contains zero or multiple timelines"
                )
            target_timeline = next(iter(timelines))
        else:
            if timeline < 1:
                raise ValueError("timeline must be a positive integer")
            target_timeline = timeline
        if base.timeline != target_timeline:
            raise PitrError(
                "Timeline-fork recovery requires an explicit compatible "
                "base; odooctl will not infer an ancestor timeline"
            )

        by_sequence: dict[int, WalReceipt] = {}
        for receipt in receipts:
            if receipt.timeline != target_timeline:
                continue
            sequence_number = wal_sequence(
                receipt.filename,
                base.wal_segment_size,
            )
            if sequence_number in by_sequence:
                raise PitrError(
                    "PITR archive contains duplicate WAL sequence identity: "
                    f"{receipt.filename}"
                )
            by_sequence[sequence_number] = receipt
        start_sequence = wal_sequence(
            base.end_wal,
            base.wal_segment_size,
        )
        ordered_sequences = sorted(
            sequence
            for sequence in by_sequence
            if sequence >= start_sequence
        )
        if not ordered_sequences:
            first_name = wal_name_for_sequence(
                start_sequence,
                target_timeline,
                base.wal_segment_size,
            )
            raise PitrError(
                "The WAL archive has no segment at or after the selected "
                f"base; first required segment: {first_name}"
            )
        if ordered_sequences[0] != start_sequence:
            missing_name = wal_name_for_sequence(
                start_sequence,
                target_timeline,
                base.wal_segment_size,
            )
            raise PitrError(
                "The WAL archive is not contiguous from the selected base; "
                f"first missing segment: {missing_name}"
            )
        for previous, current_sequence in zip(
            ordered_sequences,
            ordered_sequences[1:],
        ):
            if current_sequence != previous + 1:
                missing_name = wal_name_for_sequence(
                    previous + 1,
                    target_timeline,
                    base.wal_segment_size,
                )
                raise PitrError(
                    "The WAL archive is not contiguous from the selected "
                    f"base; first missing segment: {missing_name}"
                )
        selected = [
            by_sequence[sequence]
            for sequence in ordered_sequences
        ]
        if len(selected) > MAX_PITR_WAL_SEGMENTS:
            raise PitrError(
                "PITR plan exceeds the safe WAL segment-count limit"
            )

        # Provider timestamps prove object age, not PostgreSQL replay time.
        # Only the isolated recovery runtime may prove temporal reachability.
        for receipt in selected:
            coordination.renew()
            verified = client.verify_wal(
                receipt.filename,
                expected_sha256=receipt.sha256,
            )
            if verified.size != receipt.size:
                raise PitrError(
                    f"Remote WAL size changed for {receipt.filename}"
                )

        plan = PitrRecoveryPlan(
            plan_id=plan_id,
            project=ctx.project.config.project.name,
            environment=environment,
            cluster_id=str(pitr.cluster_id),
            system_identifier=identity,
            base_backup_id=base.base_backup_id,
            database=ctx.project.config.env(environment).db_name,
            new_database=_new_database_name(
                ctx.project.config.env(environment).db_name,
                token,
            ),
            target_time=_timestamp(target),
            target_timeline=target_timeline,
            first_wal=selected[0].filename,
            last_wal=selected[-1].filename,
            wal_count=len(selected),
            wal_bytes=sum(receipt.size for receipt in selected),
            recovery_image=base.postgres_image,
        )
        coordination.renew()
        pin = client.create_pitr_pin(
            kind="plan",
            pin_id=plan.plan_id,
            environment=environment,
            base_backup_id=base.base_backup_id,
            wal_segments=_pin_segments(selected),
            lease=coordination.lease,
        )
        _assert_remote_pin(
            pin,
            kind="plan",
            pin_id=plan.plan_id,
            environment=environment,
            base_backup_id=base.base_backup_id,
            receipts=selected,
        )
        MetadataStore(
            ctx.project.state_dir
        ).save_pitr_recovery_plan(plan)
        return plan


def _validate_plan_ownership(
    ctx: "ServiceContext",
    environment: str,
    plan: PitrRecoveryPlan,
) -> None:
    pitr = _pitr_config(ctx, environment)
    expected = (
        ctx.project.config.project.name,
        environment,
        str(pitr.cluster_id),
        ctx.project.config.env(environment).db_name,
    )
    actual = (
        plan.project,
        plan.environment,
        plan.cluster_id,
        plan.database,
    )
    if actual != expected:
        raise PitrError("PITR plan identity does not match this target")
    if plan.filestore_policy != "database_only":
        raise PitrError("PITR plan lacks the database-only safety policy")


def _physical_from_download(
    base: PitrBaseBackupManifest,
    downloaded: Path,
) -> PhysicalBaseBackup:
    pgdata = downloaded / "pgdata"
    manifest = pgdata / "backup_manifest"
    if (
        not pgdata.is_dir()
        or pgdata.is_symlink()
        or not manifest.is_file()
        or manifest.is_symlink()
        or _sha256_file(manifest)
        != base.checksums.get("pgdata/backup_manifest")
    ):
        raise PitrError(
            "Downloaded physical base backup failed local identity checks"
        )
    inspection = PostgresPitrInspection(
        server_version_num=base.postgres_major * 10000,
        server_major=base.postgres_major,
        system_identifier=base.system_identifier,
        timeline=base.timeline,
        wal_segment_size_bytes=base.wal_segment_size,
        wal_level="replica",
        archive_mode="on",
        archive_command="verified remote archive",
        archive_library="",
        tablespaces=(),
    )
    return PhysicalBaseBackup(
        pgdata=pgdata,
        tablespace_root=None,
        tablespaces=(),
        inspection=inspection,
        manifest_sha256=_sha256_file(manifest),
    )


def _verify_recovered_database(db, database: str) -> None:
    db.ping(database)
    db.psql(
        database,
        "DO $$ BEGIN "
        "IF to_regclass('public.ir_module_module') IS NULL THEN "
        "RAISE EXCEPTION 'Recovered database is not an Odoo database'; "
        "END IF; END $$;",
    )


def _cutover_plan_from_metadata(
    metadata: PitrRestoreMetadata,
) -> DatabaseCutoverPlan | None:
    """Rebuild the exact database identity fence saved before any rename."""

    if metadata.cutover_aside_database is None:
        return None
    if metadata.cutover_incoming_oid is None:
        raise PitrError("Durable PITR cutover intent is incomplete")
    return DatabaseCutoverPlan(
        cutover_id=metadata.restore_id,
        incoming_db=metadata.new_database,
        target_db=metadata.database,
        aside_db=metadata.cutover_aside_database,
        incoming_oid=metadata.cutover_incoming_oid,
        target_oid=metadata.cutover_target_oid,
    )


def _release_restore_pin(
    ctx: "ServiceContext",
    environment: str,
    metadata: PitrRestoreMetadata,
    *,
    adapter: WalS3Adapter | None,
) -> str | None:
    """Release the remote retention pin after cutover is durably finalized.

    Failure is deliberately non-destructive: retaining an extra pin preserves
    recovery data and the warning is stored for a later operator retry.
    """

    try:
        store = MetadataStore(ctx.project.state_dir)
        recovery_plan = store.get_pitr_recovery_plan(metadata.plan_id)
        base = store.get_pitr_base_manifest(metadata.base_backup_id)
        wal_names = wal_segment_range(
            recovery_plan.first_wal,
            recovery_plan.last_wal,
            base.wal_segment_size,
        )
        receipts = tuple(
            store.get_wal_receipt(
                metadata.cluster_id,
                metadata.system_identifier,
                filename,
            )
            for filename in wal_names
        )
        client = adapter or make_wal_adapter(
            ctx,
            environment,
            metadata.system_identifier,
        )
        with _CoordinationGuard(
            client,
            purpose="pitr-cutover-pin-release",
            owner=metadata.restore_id,
        ) as coordination:
            pin = client.get_pitr_pin(
                "restore",
                metadata.restore_id,
                lease=coordination.lease,
            )
            _assert_remote_pin(
                pin,
                kind="restore",
                pin_id=metadata.restore_id,
                environment=environment,
                base_backup_id=metadata.base_backup_id,
                receipts=receipts,
            )
            coordination.renew()
            client.release_pitr_pin(
                pin,
                lease=coordination.lease,
            )
    except Exception as exc:
        return "Finalized PITR cutover retained its restore pin: " + redact(
            str(exc)
        )
    return None


def execute_restore(
    ctx: "ServiceContext",
    environment: str,
    plan_id: str,
    *,
    adapter: WalS3Adapter | None = None,
    runtime: DockerPitrRuntime | None = None,
    db_adapter=None,
) -> PitrRestoreMetadata:
    _pitr_config(ctx, environment)
    store = MetadataStore(ctx.project.state_dir)
    plan = store.get_pitr_recovery_plan(plan_id)
    _validate_plan_ownership(ctx, environment, plan)
    if plan.status not in {"planned", "failed"}:
        raise PitrError(
            f"PITR plan {plan_id!r} cannot execute from status {plan.status!r}"
        )
    base = store.get_pitr_base_manifest(plan.base_backup_id)
    if (
        base.status != "complete"
        or base.system_identifier != plan.system_identifier
        or base.postgres_image != plan.recovery_image
    ):
        raise PitrError("PITR plan base backup identity is no longer valid")
    client = adapter or make_wal_adapter(
        ctx,
        environment,
        plan.system_identifier,
    )
    client.verify_base_backup(base.base_backup_id)
    wal_names = wal_segment_range(
        plan.first_wal,
        plan.last_wal,
        base.wal_segment_size,
    )
    if len(wal_names) != plan.wal_count:
        raise PitrError("PITR plan WAL count does not match its boundaries")
    wal_receipts = tuple(
        store.get_wal_receipt(
            plan.cluster_id,
            plan.system_identifier,
            filename,
        )
        for filename in wal_names
    )
    if sum(receipt.size for receipt in wal_receipts) != plan.wal_bytes:
        raise PitrError(
            "PITR plan WAL byte total no longer matches local receipts"
        )
    with _CoordinationGuard(
        client,
        purpose="pitr-execute-check",
        owner=plan.plan_id,
    ) as coordination:
        plan_pin = client.get_pitr_pin(
            "plan",
            plan.plan_id,
            lease=coordination.lease,
        )
        _assert_remote_pin(
            plan_pin,
            kind="plan",
            pin_id=plan.plan_id,
            environment=environment,
            base_backup_id=base.base_backup_id,
            receipts=wal_receipts,
        )

    restore_id = (
        f"{environment}_pitr_restore_{secrets.token_hex(12)}"
    )
    metadata = PitrRestoreMetadata(
        restore_id=restore_id,
        plan_id=plan.plan_id,
        base_backup_id=base.base_backup_id,
        project=plan.project,
        environment=environment,
        cluster_id=plan.cluster_id,
        system_identifier=plan.system_identifier,
        database=plan.database,
        new_database=plan.new_database,
        target_time=plan.target_time,
        target_timeline=plan.target_timeline,
        status="pending",
    )
    staging_root = ctx.project.state_dir / "pitr" / "restore-staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = staging_root / f".partial-{restore_id}"
    if os.path.lexists(staging):
        raise PitrError(f"PITR restore staging path exists: {staging}")
    staging.mkdir(mode=0o700)
    db = db_adapter or make_context_db_adapter(ctx.project)
    executing_plan = _validated_update(plan, status="executing")
    created_database = False
    durable_state_started = False
    try:
        store.save_pitr_restore(metadata)
        store.save_pitr_recovery_plan(executing_plan)
        durable_state_started = True
        base_root = staging / "base"
        downloaded = client.download_base_backup(
            base.base_backup_id,
            base_root,
        )
        wal_root = staging / "wal"
        wal_root.mkdir()
        total_wal_bytes = 0
        for filename, receipt in zip(wal_names, wal_receipts):
            info = client.download_wal(
                filename,
                wal_root / filename,
            )
            if (
                info.sha256 != receipt.sha256
                or info.size != receipt.size
            ):
                raise PitrError(
                    f"Downloaded WAL receipt mismatch for {filename}"
                )
            total_wal_bytes += info.size
        if total_wal_bytes != plan.wal_bytes:
            raise PitrError(
                "Downloaded WAL byte total does not match the recovery plan"
            )

        physical = _physical_from_download(base, downloaded)
        dump_path = staging / "recovered.dump"
        recovery_runtime = runtime or DockerPitrRuntime(
            ctx.project.state_dir / "pitr" / "runtime",
        )
        metadata = _validated_update(metadata, status="recovering")
        store.save_pitr_restore(metadata)
        recovery_target = _parse_utc(
            plan.target_time,
            "target_time",
        )
        recovery = recovery_runtime.recover_and_dump(
            PitrRecoverySpec(
                base_backup=physical,
                wal_archive_dir=wal_root,
                postgres_image=plan.recovery_image,
                target_time=recovery_target,
                target_timeline=plan.target_timeline,
                database=plan.database,
            ),
            dump_path,
        )
        recovery_status = recovery.status
        if (
            recovery.database != plan.database
            or recovery.target_time != recovery_target
            or str(recovery.target_timeline) != str(plan.target_timeline)
            or recovery.postgres_image != plan.recovery_image
            or Path(recovery.dump_path) != dump_path
            or recovery_status.in_recovery is not True
            or recovery_status.replay_paused is not True
            or recovery_status.timeline != plan.target_timeline
            or recovery_status.replay_lsn is None
            or (
                recovery_status.last_replay_timestamp is not None
                and recovery_status.last_replay_timestamp > recovery_target
            )
        ):
            raise PitrError(
                "Isolated PITR runtime returned an untrusted recovery result"
            )
        exists = getattr(db, "database_exists", None)
        if not callable(exists):
            raise PitrError(
                "Database adapter cannot prove the PITR target is new"
            )
        if exists(plan.new_database):
            raise PitrError(
                f"PITR target database already exists: {plan.new_database}"
            )
        create = getattr(db, "create", None)
        restore_into = getattr(db, "restore_into", None)
        if not callable(create) or not callable(restore_into):
            raise PitrError(
                "Database adapter cannot atomically create a new PITR target"
            )
        create(plan.new_database)
        created_database = True
        restore_into(plan.new_database, recovery.dump_path)
        _verify_recovered_database(db, plan.new_database)
        recovered_at = (
            recovery_status.last_replay_timestamp
            or recovery_target
        )
        metadata = _validated_update(
            metadata,
            recovered_at=_timestamp(recovered_at),
            recovered_lsn=recovery_status.replay_lsn,
            verified=True,
            status="verified",
            last_error=None,
        )
        verified_plan = _validated_update(
            executing_plan,
            status="verified",
        )
        handoff_warning: str | None = None
        handoff = _CoordinationGuard(
            client,
            purpose="pitr-restore-pin",
            owner=restore_id,
        )
        verified_committed = False
        try:
            plan_pin = client.get_pitr_pin(
                "plan",
                plan.plan_id,
                lease=handoff.lease,
            )
            _assert_remote_pin(
                plan_pin,
                kind="plan",
                pin_id=plan.plan_id,
                environment=environment,
                base_backup_id=base.base_backup_id,
                receipts=wal_receipts,
            )
            restore_pin = client.create_pitr_pin(
                kind="restore",
                pin_id=restore_id,
                environment=environment,
                base_backup_id=base.base_backup_id,
                wal_segments=_pin_segments(wal_receipts),
                lease=handoff.lease,
            )
            _assert_remote_pin(
                restore_pin,
                kind="restore",
                pin_id=restore_id,
                environment=environment,
                base_backup_id=base.base_backup_id,
                receipts=wal_receipts,
            )
            store.save_pitr_restore(metadata)
            store.save_pitr_recovery_plan(verified_plan)
            verified_committed = True
            try:
                handoff.renew()
                client.release_pitr_pin(
                    plan_pin,
                    lease=handoff.lease,
                )
            except Exception as exc:
                handoff_warning = (
                    "Verified restore retained an extra plan pin: "
                    + redact(str(exc))
                )
        finally:
            try:
                handoff.__exit__(None, None, None)
            except Exception as exc:
                if not verified_committed:
                    raise
                warning = (
                    "Verified restore could not release its coordination "
                    "lease: "
                    + redact(str(exc))
                )
                handoff_warning = (
                    f"{handoff_warning}; {warning}"
                    if handoff_warning
                    else warning
                )
        if handoff_warning:
            metadata = _validated_update(
                metadata,
                last_error=handoff_warning,
            )
            try:
                store.save_pitr_restore(metadata)
            except Exception:
                pass
        return metadata
    except BaseException as exc:
        if created_database:
            drop = getattr(db, "drop", None)
            if callable(drop):
                try:
                    drop(plan.new_database)
                except Exception:
                    pass
        if durable_state_started:
            safe_error = redact(str(exc))
            store.save_pitr_restore(
                _validated_update(
                    metadata,
                    status="failed",
                    verified=False,
                    last_error=safe_error,
                )
            )
            store.save_pitr_recovery_plan(
                _validated_update(executing_plan, status="failed")
            )
        raise
    finally:
        if staging.is_dir() and not staging.is_symlink():
            shutil.rmtree(staging)


def cutover_restore(
    ctx: "ServiceContext",
    environment: str,
    restore_id: str,
    *,
    confirm_environment: str,
    confirm_database: str,
    accept_database_only: bool,
    db_adapter=None,
    adapter: WalS3Adapter | None = None,
) -> PitrRestoreMetadata:
    _pitr_config(ctx, environment)
    store = MetadataStore(ctx.project.state_dir)
    metadata = store.get_pitr_restore(restore_id)
    expected_database = ctx.project.config.env(environment).db_name
    if (
        confirm_environment != environment
        or confirm_database != expected_database
        or not accept_database_only
    ):
        raise PitrError(
            "PITR cutover requires exact environment/database "
            "confirmations and explicit database-only acceptance"
        )
    ownership_invalid = (
        metadata.project != ctx.project.config.project.name
        or metadata.environment != environment
        or metadata.database != expected_database
        or not metadata.verified
    )
    actionable = (
        (metadata.status == "verified" and not metadata.cutover)
        or (
            metadata.status == "cutover"
            and metadata.cutover
            and not metadata.cutover_finalized
        )
    )
    if ownership_invalid or not actionable:
        if (
            not ownership_invalid
            and metadata.status == "cutover"
            and metadata.cutover
            and metadata.cutover_finalized
        ):
            return metadata
        raise PitrError(
            "Only a verified PITR restore owned by this target can be "
            "promoted or have its durable cutover finalized"
        )
    db = db_adapter or make_context_db_adapter(ctx.project)
    cutover_plan = _cutover_plan_from_metadata(metadata)
    if cutover_plan is None:
        _verify_recovered_database(db, metadata.new_database)
        cutover_plan = plan_database_cutover(
            db,
            incoming_db=metadata.new_database,
            target_db=metadata.database,
            cutover_id=metadata.restore_id,
        )
        metadata = _validated_update(
            metadata,
            cutover_aside_database=cutover_plan.aside_db,
            cutover_incoming_oid=cutover_plan.incoming_oid,
            cutover_target_oid=cutover_plan.target_oid,
            cutover_started_at=_timestamp(),
            last_error=None,
        )
        # No database rename may occur until this exact identity fence is
        # durable. A retry can then reconcile using stable PostgreSQL OIDs.
        store.save_pitr_restore(metadata)

    if not metadata.cutover:
        try:
            promoted = promote_database_cutover(db, cutover_plan)
        except DatabaseCutoverError as exc:
            store.save_pitr_restore(
                _validated_update(
                    metadata,
                    last_error=redact(str(exc)),
                )
            )
            raise
        if not promoted.promoted:
            raise PitrError("PITR database cutover did not reach promoted state")
        metadata = _validated_update(
            metadata,
            cutover=True,
            status="cutover",
            cutover_completed_at=_timestamp(),
            last_error=None,
        )
        # This is the durable signal that the live name now refers to the
        # recovered database. The original DB cannot be deleted before it.
        store.save_pitr_restore(metadata)

    try:
        finalized = finalize_database_cutover(
            db,
            cutover_plan,
            cutover_durably_recorded=True,
        )
    except DatabaseCutoverError as exc:
        store.save_pitr_restore(
            _validated_update(
                metadata,
                last_error=redact(str(exc)),
            )
        )
        raise
    if not finalized.promoted:
        raise PitrError("PITR database cutover finalization lost promotion")

    metadata = _validated_update(
        metadata,
        cutover_finalized=True,
        last_error=None,
    )
    store.save_pitr_restore(metadata)
    pin_warning = _release_restore_pin(
        ctx,
        environment,
        metadata,
        adapter=adapter,
    )
    if pin_warning:
        metadata = _validated_update(metadata, last_error=pin_warning)
        store.save_pitr_restore(metadata)
    return metadata


def _pinned_base_ids(store: MetadataStore, environment: str) -> set[str]:
    pinned = {
        plan.base_backup_id
        for plan in store.list_pitr_recovery_plans(
            environment=environment
        )
        if plan.status in {"planned", "executing", "failed"}
    }
    pinned.update(
        restore.base_backup_id
        for restore in store.list_pitr_restores(
            environment=environment
        )
        if restore.status != "cutover"
    )
    return pinned


def reconcile_retention(
    ctx: "ServiceContext",
    environment: str,
    *,
    adapter: WalS3Adapter | None = None,
    now: datetime | None = None,
) -> PitrRetentionResult:
    pitr = _pitr_config(ctx, environment)
    current = now or _now_utc()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(timezone.utc)
    identity = _resolve_system_identifier(ctx, environment)
    client = adapter or make_wal_adapter(
        ctx,
        environment,
        identity,
    )
    owner = f"retention-{environment}-{secrets.token_hex(8)}"
    with _CoordinationGuard(
        client,
        purpose="pitr-retention",
        owner=owner,
    ) as coordination:
        return _reconcile_retention_locked(
            ctx,
            environment,
            pitr=pitr,
            current=current,
            identity=identity,
            client=client,
            coordination=coordination,
        )


def _reconcile_retention_locked(
    ctx: "ServiceContext",
    environment: str,
    *,
    pitr,
    current: datetime,
    identity: str,
    client: WalS3Adapter,
    coordination: _CoordinationGuard,
) -> PitrRetentionResult:
    # Build and validate the entire inventory before the first delete.
    bases = list_base_backups(
        ctx,
        environment,
        adapter=client,
        system_identifier=identity,
    )
    if not bases:
        raise PitrError(
            "PITR retention refuses to run without a complete base backup"
        )
    remote_pins = client.list_pitr_pins(
        lease=coordination.lease,
        environment=environment,
    )
    # Verify every base and capture the provider timestamp of its immutable
    # completion marker before deciding or deleting anything. Publisher clocks
    # in manifests are not authoritative for grace or ordering.
    marker_times: dict[str, datetime] = {}
    for manifest in bases:
        coordination.renew()
        verified = client.verify_base_backup(manifest.base_backup_id)
        marker = getattr(verified, "manifest", None)
        modified = getattr(marker, "last_modified", None)
        if (
            not isinstance(modified, datetime)
            or modified.tzinfo is None
            or modified.utcoffset() is None
        ):
            raise PitrError(
                "PITR base completion marker has no authoritative "
                f"modification time: {manifest.base_backup_id}"
            )
        marker_times[manifest.base_backup_id] = modified.astimezone(
            timezone.utc
        )

    store = MetadataStore(ctx.project.state_dir)
    ordered = sorted(
        bases,
        key=lambda item: (
            marker_times[item.base_backup_id],
            item.base_backup_id,
        ),
        reverse=True,
    )
    retained = {
        item.base_backup_id
        for item in ordered[: pitr.retention.base_backups]
    }
    retained.update(_pinned_base_ids(store, environment))
    retained.update(pin.base_backup_id for pin in remote_pins)
    grace_cutoff = current - timedelta(
        hours=pitr.retention.grace_hours
    )
    retained.update(
        item.base_backup_id
        for item in ordered
        if marker_times[item.base_backup_id] >= grace_cutoff
    )
    known_ids = {item.base_backup_id for item in ordered}
    if not retained.issubset(known_ids):
        missing = ", ".join(sorted(retained - known_ids))
        raise PitrError(
            "PITR retention cannot resolve pinned bases: " + missing
        )
    if not retained:
        raise PitrError("PITR retention must retain at least one base")

    remote_wal = _remote_wal_receipts(
        ctx,
        environment,
        identity,
        client,
    )
    base_by_id = {
        manifest.base_backup_id: manifest for manifest in ordered
    }
    earliest_sequence = min(
        wal_sequence(
            base_by_id[base_id].end_wal,
            base_by_id[base_id].wal_segment_size,
        )
        for base_id in retained
    )
    segment_sizes = {
        base_by_id[base_id].wal_segment_size
        for base_id in retained
    }
    timelines = {
        base_by_id[base_id].timeline for base_id in retained
    }
    if len(segment_sizes) != 1 or len(timelines) != 1:
        raise PitrError(
            "PITR retention refuses mixed WAL sizes or timelines"
        )
    segment_size = next(iter(segment_sizes))
    timeline = next(iter(timelines))

    timeline_receipts = [
        receipt
        for receipt in remote_wal
        if receipt.timeline == timeline
    ]
    receipts_by_sequence: dict[int, WalReceipt] = {}
    receipts_by_filename = {
        receipt.filename: receipt for receipt in remote_wal
    }
    if len(receipts_by_filename) != len(remote_wal):
        raise PitrError("PITR WAL inventory contains duplicate filenames")
    for pin in remote_pins:
        pinned_base = base_by_id[pin.base_backup_id]
        pinned_receipts: list[WalReceipt] = []
        for pinned_wal in pin.wal_segments:
            receipt = receipts_by_filename.get(pinned_wal.filename)
            if (
                receipt is None
                or receipt.sha256 != pinned_wal.sha256
                or receipt.size != pinned_wal.size
            ):
                raise PitrError(
                    f"Remote PITR pin {pin.pin_id!r} references missing "
                    "or changed WAL"
                )
            pinned_receipts.append(receipt)
        if (
            pinned_receipts[0].filename != pinned_base.end_wal
            or any(
                receipt.timeline != pinned_base.timeline
                for receipt in pinned_receipts
            )
        ):
            raise PitrError(
                f"Remote PITR pin {pin.pin_id!r} is incompatible with "
                "its base backup"
            )
        pinned_sequences = [
            wal_sequence(
                receipt.filename,
                pinned_base.wal_segment_size,
            )
            for receipt in pinned_receipts
        ]
        if any(
            current_sequence != previous + 1
            for previous, current_sequence in zip(
                pinned_sequences,
                pinned_sequences[1:],
            )
        ):
            raise PitrError(
                f"Remote PITR pin {pin.pin_id!r} has a WAL gap"
            )

    for receipt in timeline_receipts:
        sequence = wal_sequence(receipt.filename, segment_size)
        if sequence in receipts_by_sequence:
            raise PitrError(
                "PITR retained WAL graph contains duplicate sequence "
                f"identity: {receipt.filename}"
            )
        receipts_by_sequence[sequence] = receipt

    retained_sequences = sorted(
        sequence
        for sequence in receipts_by_sequence
        if sequence >= earliest_sequence
    )
    if not retained_sequences or retained_sequences[0] != earliest_sequence:
        raise PitrError(
            "PITR retained WAL graph is not contiguous from its earliest "
            "retained base"
        )
    for previous, next_sequence in zip(
        retained_sequences,
        retained_sequences[1:],
    ):
        if next_sequence != previous + 1:
            raise PitrError(
                "PITR retained WAL graph has a gap after "
                + wal_name_for_sequence(
                    previous,
                    timeline,
                    segment_size,
                )
            )

    # Byte-verify both retained and deletion-candidate WAL before the first
    # destructive call. A corrupt old candidate also blocks the run: deletion
    # must never turn an ambiguous archive into an apparently clean one.
    for receipt in sorted(
        timeline_receipts,
        key=lambda item: wal_sequence(
            item.filename,
            segment_size,
        ),
    ):
        coordination.renew()
        verified = client.verify_wal(
            receipt.filename,
            expected_sha256=receipt.sha256,
        )
        if verified.size != receipt.size:
            raise PitrError(
                f"Remote WAL size changed for {receipt.filename}"
            )

    delete_bases = [
        manifest
        for manifest in reversed(ordered)
        if manifest.base_backup_id not in retained
    ]
    delete_wal = [
        receipt
        for receipt in timeline_receipts
        if wal_sequence(receipt.filename, segment_size)
        < earliest_sequence
        and _parse_utc(receipt.archived_at, "archived_at")
        < grace_cutoff
    ]

    deleted_bases: list[str] = []
    deleted_wal: list[str] = []
    for manifest in delete_bases:
        coordination.renew()
        client.delete_base_backup(
            manifest.base_backup_id,
            before_delete=lambda _key: coordination.renew(),
        )
        deleted_bases.append(manifest.base_backup_id)
    for receipt in sorted(
        delete_wal,
        key=lambda item: wal_sequence(
            item.filename,
            segment_size,
        ),
    ):
        coordination.renew()
        client.delete_wal(receipt.filename)
        deleted_wal.append(receipt.filename)
    return PitrRetentionResult(
        retained_base_backup_ids=tuple(sorted(retained)),
        deleted_base_backup_ids=tuple(deleted_bases),
        deleted_wal_filenames=tuple(deleted_wal),
    )
