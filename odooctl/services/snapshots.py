"""Snapshot DR orchestration and manifest persistence."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from odooctl.adapters.snapshots import (
    ProviderRestoreResult,
    ProviderSnapshot,
    SnapshotCreateRequest,
    SnapshotCreateProviderError,
    SnapshotProvider,
    SnapshotRestoreProviderError,
    SnapshotRestorePlan,
    make_snapshot_provider,
)
from odooctl.metadata.models import (
    SnapshotManifest,
    SnapshotRestoreMetadata,
    now_utc,
)
from odooctl.metadata.store import MetadataStore
from odooctl.services.context import ServiceContext


@dataclass(frozen=True)
class SnapshotRestoreOutcome:
    plan: SnapshotRestorePlan
    executed: bool
    restored_resource_ids: tuple[str, ...] = ()
    message: str | None = None
    status: str = "planned"


class SnapshotCreateFailed(RuntimeError):
    """Creation failed after durable local intent was recorded."""

    def __init__(self, manifest: SnapshotManifest, cause: Exception):
        super().__init__(
            f"Snapshot {manifest.snapshot_id} is {manifest.status}: {cause}. "
            f"Inspect it with 'odooctl dr snapshot list --json' and retry "
            f"discovery with 'odooctl dr snapshot reconcile "
            f"{manifest.snapshot_id}'."
        )
        self.manifest = manifest


def _assert_snapshot_credentials(ctx: ServiceContext) -> None:
    missing = ctx.project.config.missing_snapshot_env_vars()
    if missing:
        raise RuntimeError(
            "Missing snapshot provider environment variables: "
            + ", ".join(missing)
        )


def _new_snapshot_id(environment: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{environment}-{timestamp}-{secrets.token_hex(4)}"


def _configured_snapshot_identity(ctx: ServiceContext) -> tuple[str, dict[str, str]]:
    snapshots = ctx.project.config.snapshots
    if snapshots.provider == "aws_ebs" and snapshots.aws_ebs is not None:
        return snapshots.aws_ebs.instance_id, {"region": snapshots.aws_ebs.region}
    if (
        snapshots.provider == "hetzner_cloud"
        and snapshots.hetzner_cloud is not None
    ):
        context = (
            snapshots.hetzner_cloud.context or "environment-credentials"
        )
        return snapshots.hetzner_cloud.server, {"context": context}
    raise RuntimeError("No snapshot provider identity is configured")


def _merge_provider_snapshot(
    manifest: SnapshotManifest,
    provider_snapshot: ProviderSnapshot,
    *,
    last_error: str | None = None,
) -> SnapshotManifest:
    completed_at = (
        manifest.completed_at
        if manifest.completed_at
        else now_utc() if provider_snapshot.status == "complete" else None
    )
    return SnapshotManifest.model_validate(
        {
            **manifest.model_dump(),
            "source_resource_id": provider_snapshot.source_resource_id,
            "resources": list(provider_snapshot.resources),
            "scope": list(provider_snapshot.scope),
            "consistency": provider_snapshot.consistency,
            "recovery_notes": list(provider_snapshot.recovery_notes),
            "provider_scope": dict(provider_snapshot.provider_scope),
            "provider_metadata": dict(provider_snapshot.provider_metadata),
            "status": provider_snapshot.status,
            "completed_at": completed_at,
            "last_error": last_error,
        }
    )


def run_snapshot_create(
    ctx: ServiceContext,
    environment: str,
    *,
    trigger: str = "explicit",
    portable_backup_id: str | None = None,
    provider: SnapshotProvider | None = None,
) -> SnapshotManifest:
    cfg = ctx.project.config
    cfg.env(environment)
    if trigger not in {"explicit", "pre_deploy"}:
        raise ValueError(f"Unsupported snapshot trigger: {trigger}")
    if cfg.snapshots.provider == "none":
        raise RuntimeError(
            "Snapshot creation is disabled because snapshots.provider is none"
        )
    if environment != cfg.snapshots.environment:
        raise RuntimeError(
            f"Snapshot provider is bound to environment "
            f"{cfg.snapshots.environment!r}, not {environment!r}"
        )
    if provider is None:
        _assert_snapshot_credentials(ctx)

    snapshot_id = _new_snapshot_id(environment)
    description = (
        f"odooctl DR snapshot {snapshot_id} for "
        f"{cfg.project.name}/{environment}"
    )
    selected = provider or make_snapshot_provider(cfg.snapshots)
    source_resource_id, provider_scope = _configured_snapshot_identity(ctx)
    store = MetadataStore(ctx.project.state_dir)
    manifest = SnapshotManifest(
        snapshot_id=snapshot_id,
        project=cfg.project.name,
        environment=environment,
        provider=cfg.snapshots.provider,
        source_resource_id=source_resource_id,
        trigger=trigger,
        portable_backup_id=portable_backup_id,
        description=description,
        status="requested",
        provider_scope=provider_scope,
    )
    # Persist intent before crossing the first cloud mutation boundary.
    store.save_snapshot_manifest(manifest)

    def persist_progress(progress: ProviderSnapshot) -> None:
        nonlocal manifest
        manifest = _merge_provider_snapshot(manifest, progress)
        store.save_snapshot_manifest(manifest)

    try:
        created = selected.create(
            SnapshotCreateRequest(
                snapshot_id=snapshot_id,
                project=cfg.project.name,
                environment=environment,
                description=description,
                progress=persist_progress,
            )
        )
    except Exception as exc:
        if isinstance(exc, SnapshotCreateProviderError) and exc.snapshot:
            manifest = _merge_provider_snapshot(
                manifest,
                exc.snapshot,
                last_error=str(exc),
            )
        else:
            manifest = manifest.model_copy(
                update={"last_error": str(exc)}
            )
        store.save_snapshot_manifest(manifest)
        raise SnapshotCreateFailed(manifest, exc) from exc

    manifest = _merge_provider_snapshot(manifest, created)
    store.save_snapshot_manifest(manifest)
    return manifest


def run_snapshot_reconcile(
    ctx: ServiceContext,
    snapshot_id: str,
    *,
    expected_environment: str | None = None,
    provider: SnapshotProvider | None = None,
) -> SnapshotManifest:
    manifest = get_snapshot(ctx, snapshot_id)
    if (
        expected_environment is not None
        and manifest.environment != expected_environment
    ):
        raise RuntimeError(
            f"Snapshot {snapshot_id} belongs to environment "
            f"{manifest.environment!r}, not locked environment "
            f"{expected_environment!r}"
        )
    cfg = ctx.project.config
    if cfg.snapshots.provider == "none":
        raise RuntimeError(
            "Snapshot reconciliation is disabled because snapshots.provider is none"
        )
    if manifest.environment != cfg.snapshots.environment:
        raise RuntimeError(
            f"Snapshot {snapshot_id} is labelled for environment "
            f"{manifest.environment!r}, but the provider is bound to "
            f"{cfg.snapshots.environment!r}"
        )
    if manifest.provider != cfg.snapshots.provider:
        raise RuntimeError(
            f"Snapshot {snapshot_id} uses provider {manifest.provider}, but the "
            f"project is configured for {cfg.snapshots.provider}"
        )
    if provider is None:
        _assert_snapshot_credentials(ctx)
    selected = provider or make_snapshot_provider(cfg.snapshots)
    store = MetadataStore(ctx.project.state_dir)
    try:
        refreshed = selected.reconcile(manifest)
    except Exception as exc:
        failed = manifest.model_copy(update={"last_error": str(exc)})
        store.save_snapshot_manifest(failed)
        raise
    manifest = _merge_provider_snapshot(manifest, refreshed)
    store.save_snapshot_manifest(manifest)
    return manifest


def get_snapshot(ctx: ServiceContext, snapshot_id: str) -> SnapshotManifest:
    manifest = MetadataStore(ctx.project.state_dir).get_snapshot(snapshot_id)
    if manifest.project != ctx.project.config.project.name:
        raise RuntimeError(
            f"Snapshot {snapshot_id} belongs to project {manifest.project!r}, "
            f"not {ctx.project.config.project.name!r}"
        )
    return manifest


def list_snapshots(
    ctx: ServiceContext,
    environment: str | None = None,
) -> list[SnapshotManifest]:
    if environment is not None:
        ctx.project.config.env(environment)
    return MetadataStore(ctx.project.state_dir).list_snapshots(environment)


def plan_snapshot_restore(
    ctx: ServiceContext,
    snapshot_id: str,
    *,
    provider: SnapshotProvider | None = None,
) -> tuple[SnapshotManifest, SnapshotRestorePlan]:
    manifest = get_snapshot(ctx, snapshot_id)
    cfg = ctx.project.config
    if cfg.snapshots.provider == "none":
        raise RuntimeError(
            "Snapshot restore is disabled because snapshots.provider is none"
        )
    if manifest.environment != cfg.snapshots.environment:
        raise RuntimeError(
            f"Snapshot {snapshot_id} is labelled for environment "
            f"{manifest.environment!r}, but the provider is bound to "
            f"{cfg.snapshots.environment!r}"
        )
    if manifest.provider != cfg.snapshots.provider:
        raise RuntimeError(
            f"Snapshot {snapshot_id} uses provider {manifest.provider}, but the "
            f"project is configured for {cfg.snapshots.provider}"
        )
    selected = provider or make_snapshot_provider(cfg.snapshots)
    return manifest, selected.plan_restore(manifest)


def run_snapshot_restore(
    ctx: ServiceContext,
    snapshot_id: str,
    *,
    execute: bool = False,
    confirm_snapshot_id: str | None = None,
    confirm_resource_id: str | None = None,
    expected_environment: str | None = None,
    provider: SnapshotProvider | None = None,
    prepared: tuple[SnapshotManifest, SnapshotRestorePlan] | None = None,
) -> SnapshotRestoreOutcome:
    if prepared is None:
        manifest, plan = plan_snapshot_restore(
            ctx,
            snapshot_id,
            provider=provider,
        )
    else:
        manifest, plan = prepared
        if manifest.snapshot_id != snapshot_id or plan.snapshot_id != snapshot_id:
            raise RuntimeError("Prepared snapshot restore plan identity changed")
        current = get_snapshot(ctx, snapshot_id)
        if current.model_dump() != manifest.model_dump():
            raise RuntimeError(
                f"Snapshot {snapshot_id} changed after the recovery plan was "
                "prepared; review a fresh plan"
            )
    if (
        expected_environment is not None
        and manifest.environment != expected_environment
    ):
        raise RuntimeError(
            f"Snapshot {snapshot_id} belongs to environment "
            f"{manifest.environment!r}, not locked environment "
            f"{expected_environment!r}"
        )
    store = MetadataStore(ctx.project.state_dir)
    if not execute:
        metadata = SnapshotRestoreMetadata(
            snapshot_id=manifest.snapshot_id,
            project=manifest.project,
            environment=manifest.environment,
            provider=manifest.provider,
            source_resource_id=manifest.source_resource_id,
            executed=False,
            status="planned",
            message="Restore plan generated; no provider resources changed.",
        )
        store.save_snapshot_restore(metadata)
        return SnapshotRestoreOutcome(
            plan=plan,
            executed=False,
            message=metadata.message,
            status="planned",
        )

    if confirm_snapshot_id != manifest.snapshot_id:
        raise RuntimeError(
            "Snapshot confirmation does not match exactly. "
            f"Type {manifest.snapshot_id!r}."
        )
    if confirm_resource_id != manifest.source_resource_id:
        raise RuntimeError(
            "Source-resource confirmation does not match exactly. "
            f"Type {manifest.source_resource_id!r}."
        )

    if provider is None:
        _assert_snapshot_credentials(ctx)
    selected = provider or make_snapshot_provider(ctx.project.config.snapshots)

    def persist_restore_progress(progress: ProviderRestoreResult) -> None:
        store.save_snapshot_restore(
            SnapshotRestoreMetadata(
                snapshot_id=manifest.snapshot_id,
                project=manifest.project,
                environment=manifest.environment,
                provider=manifest.provider,
                source_resource_id=manifest.source_resource_id,
                executed=True,
                restored_resource_ids=list(progress.restored_resource_ids),
                status=progress.status,
                message=progress.message,
            )
        )

    try:
        restored: ProviderRestoreResult = selected.restore(
            manifest,
            progress=persist_restore_progress,
        )
    except Exception as exc:
        partial_ids = (
            exc.restored_resource_ids
            if isinstance(exc, SnapshotRestoreProviderError)
            else ()
        )
        store.save_snapshot_restore(
            SnapshotRestoreMetadata(
                snapshot_id=manifest.snapshot_id,
                project=manifest.project,
                environment=manifest.environment,
                provider=manifest.provider,
                source_resource_id=manifest.source_resource_id,
                executed=True,
                restored_resource_ids=list(partial_ids),
                status="failed",
                message=str(exc),
            )
        )
        raise

    store.save_snapshot_restore(
        SnapshotRestoreMetadata(
            snapshot_id=manifest.snapshot_id,
            project=manifest.project,
            environment=manifest.environment,
            provider=manifest.provider,
            source_resource_id=manifest.source_resource_id,
            executed=True,
            restored_resource_ids=list(restored.restored_resource_ids),
            status=restored.status,
            message=restored.message,
        )
    )
    return SnapshotRestoreOutcome(
        plan=plan,
        executed=True,
        restored_resource_ids=restored.restored_resource_ids,
        message=restored.message,
        status=restored.status,
    )
