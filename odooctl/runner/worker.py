"""Privileged runner worker — claims and executes queued operations.

The runner:
1. Loads the registry and iterates registered projects.
2. Claims the oldest pending queue entry (atomic POSIX rename).
3. Verifies the capability token (signature, expiry, scope).
4. Checks the nonce has not been replayed (single-use enforcement).
5. Acquires the per-environment lock.
6. Executes the appropriate service call.
7. Emits operation events and transitions status QUEUED→RUNNING→SUCCEEDED/FAILED.
8. Appends an audit trail entry.

This module is privileged — it imports ``odooctl.adapters`` / ``odooctl.odoo``
transitively via the service layer. It must never be imported by odooctl.api.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from odooctl.api.queue import OperationQueue, QueueEntry
from odooctl.operations.audit import AUDIT_KEY_ENV_VAR, AuditEntry, AuditStore
from odooctl.operations.engine import OperationContext
from odooctl.operations.locks import EnvironmentLock, LockAcquisitionError
from odooctl.operations.models import (
    OperationKind,
    OperationStatus,
    _utcnow,
)
from odooctl.operations.store import OperationStore
from odooctl.security import rbac, tokens
from odooctl.security.principals import Principal, PrincipalKind, Role
from odooctl.security.tokens import TokenError
from odooctl.adapters.db import make_db_adapter as make_context_db_adapter
from odooctl.adapters.dr_runtime import DockerComposeDrillRuntime
from odooctl.migration.rehearse import rehearse_upgrade, UpgradeResult
from odooctl.services.backup import run_backup
from odooctl.services.clone import run_clone
from odooctl.services.context import ServiceContext
from odooctl.services.dr import run_dr_drill
from odooctl.services.snapshots import (
    run_snapshot_create,
    run_snapshot_reconcile,
    run_snapshot_restore,
)
from odooctl.utils.shell import redact

if TYPE_CHECKING:
    from odooctl.registry import Registry


_KIND_ACTION: dict[str, rbac.Action] = {
    "backup": rbac.Action.BACKUP,
    "restore": rbac.Action.RESTORE,
    "clone": rbac.Action.CLONE,
    "deploy": rbac.Action.DEPLOY,
    "promote": rbac.Action.PROMOTE,
    "env_create": rbac.Action.ENV,
    "env_destroy": rbac.Action.ENV,
    "update_modules": rbac.Action.DEPLOY,
    "rollback": rbac.Action.RESTORE,
    "dr_drill": rbac.Action.RESTORE,
    "snapshot_create": rbac.Action.BACKUP,
    "snapshot_reconcile": rbac.Action.BACKUP,
    "snapshot_restore": rbac.Action.RESTORE,
    "pitr_base_create": rbac.Action.BACKUP,
    "pitr_restore": rbac.Action.RESTORE,
    "pitr_cutover": rbac.Action.RESTORE,
    "pitr_reconcile": rbac.Action.BACKUP,
    "migrate_rehearsal": rbac.Action.RESTORE,
}


# Consumed nonces are retained for twice the maximum capability-token TTL, so
# a nonce always outlives every token that could carry it (replay within the
# token validity window stays blocked) while the store cannot grow unbounded.
NONCE_RETENTION_SECONDS = 7200  # 2 h = 2 × max token TTL


class NonceStore:
    """Tracks consumed capability token nonces to prevent replay attacks.

    Nonces are stored in ``{state_dir}/consumed_nonces.json`` as a mapping of
    ``{nonce: consumed_at_iso}``. Entries older than
    :data:`NONCE_RETENTION_SECONDS` are purged on each :meth:`mark_consumed`.
    The legacy format (``{"nonces": [nonce, ...]}``) is still accepted on
    read; legacy entries are migrated to a now-timestamp on first write (they
    remain consumed until they age out).
    """

    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / "consumed_nonces.json"
        self._lock_path = state_dir / "consumed_nonces.lock"
        state_dir.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, str]:
        """Return ``{nonce: expiry_iso}``, migrating the legacy list form.

        A truncated/corrupt file (e.g. a crash during a non-atomic write in an
        older version) yields ``{}`` — with atomic writes below this is no
        longer produced, but reads stay defensive.
        """
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text()).get("nonces", {})
        except Exception:
            return {}
        default_expiry = (
            datetime.now(timezone.utc) + timedelta(seconds=NONCE_RETENTION_SECONDS)
        ).isoformat()
        if isinstance(raw, dict):
            return {str(n): str(ts) for n, ts in raw.items()}
        if isinstance(raw, list):
            # Legacy format: no timestamp — keep blocked for a full retention
            # window so replay stays blocked; ages out on the normal purge.
            return {str(n): default_expiry for n in raw}
        return {}

    def _write_atomic(self, nonces: dict[str, str]) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"nonces": nonces}))
        os.replace(tmp, self._path)

    @staticmethod
    def _purge(nonces: dict[str, str], now: datetime) -> dict[str, str]:
        kept: dict[str, str] = {}
        for name, ts in nonces.items():
            try:
                expiry = datetime.fromisoformat(ts)
            except (TypeError, ValueError):
                kept[name] = (now + timedelta(seconds=NONCE_RETENTION_SECONDS)).isoformat()
                continue
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry >= now:
                kept[name] = ts
        return kept

    def is_consumed(self, nonce: str) -> bool:
        return nonce in self._load()

    def consume(self, nonce: str, *, expires_at: datetime | None = None) -> bool:
        """Atomically claim *nonce*. Returns False if it was already consumed.

        The whole check-and-mark runs under an exclusive file lock, closing the
        TOCTOU window between a separate ``is_consumed``/``mark_consumed`` pair
        and across concurrent runner processes. The nonce is retained until at
        least its token's expiry (``expires_at``), never purged early.
        """
        with self._lock_path.open("w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            now = datetime.now(timezone.utc)
            nonces = self._purge(self._load(), now)
            if nonce in nonces:
                self._write_atomic(nonces)
                return False
            floor = now + timedelta(seconds=NONCE_RETENTION_SECONDS)
            expiry = max(floor, expires_at) if expires_at else floor
            nonces[nonce] = expiry.isoformat()
            self._write_atomic(nonces)
            return True

    def mark_consumed(self, nonce: str, *, expires_at: datetime | None = None) -> None:
        # Backward-compatible unconditional mark, now atomic + locked.
        self.consume(nonce, expires_at=expires_at)


class RunnerWorker:
    """Claims and executes one queued operation per ``claim_and_run()`` call."""

    def __init__(self, registry: "Registry", api_key: str) -> None:
        self._registry = registry
        self._api_key = api_key
        self.last_run_ok: bool = True
        # One-time notice when the audit chain is unkeyed: without
        # ODOOCTL_AUDIT_KEY the hash chain is plain SHA-256, so an attacker with
        # filesystem write access could forge or rehash entries. The chain still
        # detects truncation via the high-water mark, but tamper-evidence needs
        # the key.
        if not os.environ.get(AUDIT_KEY_ENV_VAR):
            warnings.warn(
                "Audit chain is unkeyed (ODOOCTL_AUDIT_KEY not set); entries are "
                "not cryptographically tamper-evident. Set ODOOCTL_AUDIT_KEY for "
                "HMAC-protected audit logging.",
                stacklevel=2,
            )

    def claim_and_run(self) -> bool:
        """Claim and execute one operation. Returns True if work was done.

        The outcome of the most recent executed operation is recorded on
        ``self.last_run_ok`` so callers (``run_loop``) can report failures.
        """
        for proj_name, proj in self._registry.projects.items():
            from odooctl.registry import context_from_registered

            try:
                # Path containment: the privileged runner must not load a config
                # that escapes the registered project root (codex re-scan #6).
                ctx = context_from_registered(proj)
            except Exception:
                continue

            queue = OperationQueue(ctx.state_dir)
            entry = queue.claim_next()
            if entry is None:
                continue

            self.last_run_ok = self._execute_entry(entry, queue, ctx)
            return True
        self.last_run_ok = True
        return False

    def _execute_entry(self, entry: QueueEntry, queue: OperationQueue, ctx) -> bool:
        store = OperationStore(ctx.state_dir)
        audit = AuditStore(ctx.state_dir)
        nonce_store = NonceStore(ctx.state_dir)
        svc_ctx = ServiceContext(project=ctx)

        # Re-check status: the operation may have been cancelled after we claimed
        # the queue entry (post-claim race). Skip execution and clean up.
        try:
            if store.load(entry.op_id).status == OperationStatus.CANCELLED:
                queue.complete(entry.op_id)
                return True
        except KeyError:
            pass

        # Verify the capability token
        try:
            payload = tokens.verify(
                self._api_key,
                entry.token,
                action=entry.kind,
                environment=entry.environment,
                project=entry.project,
            )
        except TokenError as exc:
            store.update_status(
                entry.op_id, OperationStatus.FAILED, error=redact(f"token error: {exc}")
            )
            queue.fail(entry.op_id)
            return False

        # Defensive RBAC floor: do not trust queue shape alone. Reconstruct the
        # token-derived principal and enforce the same protected-env floor that
        # the API applied before enqueueing.
        try:
            action = _KIND_ACTION[entry.kind]
            protected = ctx.config.is_protected(entry.environment)
            rbac.require(_principal_from_payload(payload), action, protected=protected)
        except (KeyError, ValueError, rbac.AccessDenied) as exc:
            store.update_status(
                entry.op_id, OperationStatus.FAILED, error=redact(f"rbac error: {exc}")
            )
            queue.fail(entry.op_id)
            return False

        # Single-use nonce check: atomic claim under a file lock closes the
        # check-then-mark race and retains the nonce until the token's own
        # expiry so a long-TTL token can never be replayed after purge.
        nonce = payload.get("nonce", "")
        token_exp = payload.get("exp")
        expires_at = None
        if isinstance(token_exp, (int, float)):
            expires_at = datetime.fromtimestamp(token_exp, tz=timezone.utc)
        if not nonce_store.consume(nonce, expires_at=expires_at):
            store.update_status(
                entry.op_id,
                OperationStatus.FAILED,
                error=f"token nonce already consumed: {nonce}",
            )
            queue.fail(entry.op_id)
            return False

        # Transition to RUNNING and emit start event
        store.update_status(entry.op_id, OperationStatus.RUNNING)
        op = store.load(entry.op_id)
        op_ctx = OperationContext(op, store)
        op_ctx.emit(
            f"operation started: {entry.kind} on {entry.environment}",
            phase="start",
        )

        # Acquire per-environment lock and execute
        lock = EnvironmentLock(entry.environment, ctx.state_dir, entry.op_id)
        outcome = "failed"
        error_msg: str | None = None

        try:
            lock.__enter__()
            try:
                _dispatch(entry, svc_ctx, op_ctx)
                outcome = "succeeded"
            except Exception as exc:
                # Second redaction layer: this string is persisted into the
                # operation store and streamed to API clients.
                error_msg = redact(str(exc))
            finally:
                lock.__exit__(None, None, None)
        except LockAcquisitionError as exc:
            error_msg = f"lock acquisition failed: {exc}"

        # Finalise operation status
        if outcome == "succeeded":
            op_ctx.emit("operation completed", phase="end", level="info")
            store.update_status(entry.op_id, OperationStatus.SUCCEEDED)
            queue.complete(entry.op_id)
        else:
            op_ctx.emit(f"operation failed: {error_msg}", phase="end", level="error")
            store.update_status(entry.op_id, OperationStatus.FAILED, error=error_msg)
            queue.fail(entry.op_id)

        audit.append(
            AuditEntry(
                actor=entry.actor,
                action=entry.kind,
                target=entry.environment,
                params_redacted=entry.params_redacted,
                outcome=outcome,
                op_id=entry.op_id,
                timestamp=_utcnow(),
            )
        )
        return outcome == "succeeded"

    def run_loop(self, *, once: bool = False, fail_fast: bool = False) -> bool:
        """Process the queue in a loop.

        :param once: If True, process at most one item and return (used by
            ``odooctl runner --once``).
        :param fail_fast: If True, stop looping as soon as an operation fails.
        :returns: True if every executed operation succeeded, False if the
            last executed operation failed (``once``) or a failure stopped the
            loop (``fail_fast``). ``odooctl runner`` exits non-zero on False.
        """
        while True:
            did_work = self.claim_and_run()
            if once:
                return self.last_run_ok
            if did_work and fail_fast and not self.last_run_ok:
                return False
            if not did_work:
                time.sleep(1)


def _dispatch(entry: QueueEntry, svc_ctx: ServiceContext, op_ctx: OperationContext) -> None:
    """Dispatch a queued entry to the appropriate service call."""
    kind = entry.kind
    env = entry.environment
    params = entry.params_redacted

    if kind == OperationKind.BACKUP.value:
        result = run_backup(svc_ctx, env)
        op_ctx.emit(
            f"backup complete: {result.backup_id}",
            phase="backup",
            data={
                "backup_id": result.backup_id,
                "remote_uri": result.remote_uri,
                "remote_status": result.remote_status,
            },
        )
        if result.remote_error:
            op_ctx.emit(
                f"remote backup warning: {result.remote_error}",
                phase="remote_backup",
                level="warning",
                data={"remote_status": result.remote_status},
            )

    elif kind == OperationKind.CLONE.value:
        source = params.get("source", "production")
        result = run_clone(svc_ctx, source, env)
        op_ctx.emit(f"clone complete: {result.url}", phase="clone")

    elif kind == OperationKind.DR_DRILL.value:
        cfg = svc_ctx.project.config
        cfg.env(env)
        runtime = DockerComposeDrillRuntime(svc_ctx.project)

        def healthcheck_fn(url: str) -> bool:
            from odooctl.odoo.healthcheck import check_url

            check_url(
                url,
                timeout=cfg.healthcheck.timeout_seconds,
                retries=cfg.healthcheck.retries,
                interval=cfg.healthcheck.interval_seconds,
            )
            return True

        result = run_dr_drill(
            environment=env,
            expected_project=cfg.project.name,
            backups_root=svc_ctx.project.backups_dir,
            healthcheck_fn=healthcheck_fn,
            is_protected_fn=cfg.is_protected,
            runtime_filestore_root=(
                f"{cfg.odoo.filestore_container_path.rstrip('/')}/filestore"
            ),
            prepare_runtime_fn=runtime.prepare,
            restore_database_fn=runtime.restore_database,
            restore_filestore_fn=runtime.restore_filestore,
            start_runtime_fn=runtime.start,
            stop_runtime_fn=runtime.stop,
        )
        if result.status != "success":
            raise RuntimeError(result.message or "DR drill failed")
        op_ctx.emit(f"DR drill complete: {result.backup_id}", phase="dr_drill")

    elif kind == OperationKind.SNAPSHOT_CREATE.value:
        result = run_snapshot_create(svc_ctx, env)
        op_ctx.emit(
            f"snapshot {result.status}: {result.snapshot_id}",
            phase="snapshot",
            data={
                "snapshot_id": result.snapshot_id,
                "provider": result.provider,
                "status": result.status,
                "source_resource_id": result.source_resource_id,
            },
        )

    elif kind == OperationKind.SNAPSHOT_RECONCILE.value:
        snapshot_id = str(params.get("snapshot_id", ""))
        if not snapshot_id:
            raise ValueError("snapshot_reconcile requires 'snapshot_id' in params")
        result = run_snapshot_reconcile(
            svc_ctx,
            snapshot_id,
            expected_environment=env,
        )
        op_ctx.emit(
            f"snapshot {result.status}: {result.snapshot_id}",
            phase="snapshot",
            data={
                "snapshot_id": result.snapshot_id,
                "status": result.status,
                "source_resource_id": result.source_resource_id,
            },
        )

    elif kind == OperationKind.SNAPSHOT_RESTORE.value:
        snapshot_id = str(params.get("snapshot_id", ""))
        if not snapshot_id:
            raise ValueError("snapshot_restore requires 'snapshot_id' in params")
        execute = params.get("execute", False)
        if not isinstance(execute, bool):
            raise ValueError("snapshot_restore 'execute' must be a boolean")
        result = run_snapshot_restore(
            svc_ctx,
            snapshot_id,
            execute=execute,
            confirm_snapshot_id=params.get("confirm_snapshot"),
            confirm_resource_id=params.get("confirm_resource"),
            expected_environment=env,
        )
        op_ctx.emit(
            (
                f"snapshot recovery {result.status}"
                if result.executed
                else "snapshot recovery planned"
            ),
            phase="snapshot_restore",
            data={
                "snapshot_id": snapshot_id,
                "source_resource_id": result.plan.source_resource_id,
                "status": result.status,
                "message": result.message,
                "restored_resource_ids": list(result.restored_resource_ids),
                "plan": {
                    "provider": result.plan.provider,
                    "commands": [list(command) for command in result.plan.commands],
                    "destructive": result.plan.destructive,
                    "notes": list(result.plan.notes),
                },
            },
        )

    elif kind == OperationKind.PITR_BASE_CREATE.value:
        from odooctl.services.pitr import create_base_backup

        manifest = create_base_backup(svc_ctx, env)
        op_ctx.emit(
            f"PITR base complete: {manifest.base_backup_id}",
            phase="pitr_base",
            data={
                "base_backup_id": manifest.base_backup_id,
                "system_identifier": manifest.system_identifier,
                "end_wal": manifest.end_wal,
                "remote_uri": manifest.remote_uri,
            },
        )

    elif kind == OperationKind.PITR_RECONCILE.value:
        from odooctl.services.pitr import reconcile_retention

        result = reconcile_retention(svc_ctx, env)
        op_ctx.emit(
            "PITR retention reconciliation complete",
            phase="pitr_retention",
            data={
                "retained_base_backup_ids": list(
                    result.retained_base_backup_ids
                ),
                "deleted_base_backup_ids": list(
                    result.deleted_base_backup_ids
                ),
                "deleted_wal_filenames": list(
                    result.deleted_wal_filenames
                ),
            },
        )

    elif kind == OperationKind.PITR_RESTORE.value:
        from odooctl.services.pitr import execute_restore

        plan_id = params.get("plan_id")
        if not isinstance(plan_id, str) or not plan_id:
            raise ValueError(
                "pitr_restore requires a non-empty string 'plan_id'"
            )
        result = execute_restore(svc_ctx, env, plan_id)
        op_ctx.emit(
            f"PITR restore verified: {result.restore_id}",
            phase="pitr_restore",
            data={
                "restore_id": result.restore_id,
                "new_database": result.new_database,
                "target_time": result.target_time,
                "recovered_lsn": result.recovered_lsn,
            },
        )

    elif kind == OperationKind.PITR_CUTOVER.value:
        from odooctl.services.pitr import cutover_restore

        restore_id = params.get("restore_id")
        confirm_environment = params.get("confirm_environment")
        confirm_database = params.get("confirm_database")
        accept_database_only = params.get("accept_database_only")
        if not isinstance(restore_id, str) or not restore_id:
            raise ValueError(
                "pitr_cutover requires a non-empty string 'restore_id'"
            )
        if not isinstance(confirm_environment, str):
            raise ValueError(
                "pitr_cutover requires string 'confirm_environment'"
            )
        if not isinstance(confirm_database, str):
            raise ValueError(
                "pitr_cutover requires string 'confirm_database'"
            )
        if not isinstance(accept_database_only, bool):
            raise ValueError(
                "pitr_cutover 'accept_database_only' must be a boolean"
            )
        result = cutover_restore(
            svc_ctx,
            env,
            restore_id,
            confirm_environment=confirm_environment,
            confirm_database=confirm_database,
            accept_database_only=accept_database_only,
        )
        op_ctx.emit(
            f"PITR cutover complete: {result.restore_id}",
            phase="pitr_cutover",
            data={
                "restore_id": result.restore_id,
                "database": result.database,
                "filestore_consistency": (
                    result.filestore_consistency
                ),
            },
        )

    elif kind == OperationKind.MIGRATE_REHEARSAL.value:
        from odooctl.migration.matrix import supported_paths

        cfg = svc_ctx.project.config
        env_cfg = cfg.env(env)
        db_adapter = make_context_db_adapter(svc_ctx.project)
        target_version = params.get("to", "")
        if not target_version:
            raise ValueError("migrate_rehearsal requires 'to' version in params")
        use_openupgrade = bool(params.get("openupgrade", False))
        keep_throwaway = bool(params.get("keep", False))
        from_version = cfg.project.odoo_version

        matrix_paths = supported_paths(from_version=from_version, to_version=target_version)
        if not matrix_paths:
            raise ValueError(
                f"No supported migration path from {from_version!r} to {target_version!r}; "
                "check the migration matrix for supported paths."
            )
        path_requires_ou = any(p.requires_openupgrade for p in matrix_paths)

        def _upgrade_fn(throwaway_db: str, tgt_ver: str) -> UpgradeResult:
            from odooctl.adapters.docker_compose import DockerComposeAdapter

            compose = DockerComposeAdapter(
                cfg.runtime.compose_file, project_dir=str(svc_ctx.project.root)
            )
            if use_openupgrade:
                from odooctl.migration.openupgrade import openupgrade_db_command

                cmd = openupgrade_db_command(throwaway_db, tgt_ver)
                if cmd is None:
                    raise ValueError(
                        f"OpenUpgrade does not support target version {tgt_ver!r}; "
                        "check PINNED_BRANCHES or remove --openupgrade"
                    )
            else:
                cmd = [
                    "odoo",
                    "--database",
                    throwaway_db,
                    "--update",
                    "all",
                    "--stop-after-init",
                ]
            try:
                compose.exec(cfg.odoo.service, cmd, stream=True)
                return UpgradeResult(ok=True)
            except Exception as exc:
                return UpgradeResult(ok=False, warnings=[str(exc)])

        def _healthcheck_fn(db_name: str) -> bool:
            # Ping the throwaway DB — after --stop-after-init Odoo is not running,
            # so an HTTP check against the source env URL would test the wrong target.
            try:
                db_adapter.ping(db_name)
                return True
            except Exception:
                return False

        report_dir = svc_ctx.project.state_dir / "migration_reports"

        result = rehearse_upgrade(
            source_env=env,
            source_version=from_version,
            target_version=target_version,
            source_db=env_cfg.db_name,
            db_adapter=db_adapter,
            healthcheck_fn=_healthcheck_fn,
            upgrade_fn=_upgrade_fn,
            report_dir=report_dir,
            keep=keep_throwaway,
            requires_openupgrade=path_requires_ou,
            use_openupgrade=use_openupgrade,
        )
        if result.status != "success":
            raise RuntimeError(result.message or "Migration rehearsal failed")
        op_ctx.emit(
            f"migrate rehearsal complete: {env} {result.source_version} → {target_version}",
            phase="migrate_rehearsal",
        )

    else:
        raise ValueError(f"Unsupported operation kind in runner: {kind!r}")


def _principal_from_payload(payload: dict) -> Principal:
    roles_raw = payload.get("roles", [])
    roles: list[Role] = []
    if isinstance(roles_raw, list):
        for role in roles_raw:
            try:
                roles.append(Role(role))
            except ValueError:
                continue

    subject = str(payload.get("sub", "api-client"))
    return Principal(
        id=subject,
        org_id=str(payload.get("org", "default")),
        kind=PrincipalKind.TOKEN,
        roles=frozenset(roles),
        display=subject,
    )
