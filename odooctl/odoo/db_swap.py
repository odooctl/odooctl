from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable, Protocol


class SwapPsqlAdapter(Protocol):
    def psql(self, db_name: str, sql: str) -> None: ...


def quote_identifier(name: str) -> str:
    """Return a PostgreSQL quoted identifier for a database name."""
    if "\x00" in name:
        raise ValueError("database name cannot contain NUL bytes")
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


_POSTGRES_IDENTIFIER_MAX_BYTES = 63
_CUTOVER_ASIDE_MARKER = "__odooctl_cutover_"
_CUTOVER_HASH_LENGTH = 32


def _validate_database_name(name: str, field_name: str) -> None:
    if not name:
        raise ValueError(f"{field_name} cannot be empty")
    if "\x00" in name:
        raise ValueError(f"{field_name} cannot contain NUL bytes")
    if len(name.encode("utf-8")) > _POSTGRES_IDENTIFIER_MAX_BYTES:
        raise ValueError(
            f"{field_name} exceeds PostgreSQL's "
            f"{_POSTGRES_IDENTIFIER_MAX_BYTES}-byte identifier limit"
        )


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def database_cutover_aside_name(
    *,
    target_db: str,
    incoming_db: str,
    cutover_id: str,
) -> str:
    """Return the deterministic, collision-resistant name for the old DB.

    PostgreSQL silently truncates identifiers beyond 63 bytes. Keeping the
    full hash suffix inside that limit prevents two distinct cutovers from
    accidentally sharing an aside name.
    """
    _validate_database_name(target_db, "target_db")
    _validate_database_name(incoming_db, "incoming_db")
    if not cutover_id:
        raise ValueError("cutover_id cannot be empty")
    if "\x00" in cutover_id:
        raise ValueError("cutover_id cannot contain NUL bytes")

    digest_input = "\0".join(
        ("odooctl-database-cutover-v1", cutover_id, target_db, incoming_db)
    ).encode()
    digest = hashlib.sha256(digest_input).hexdigest()[:_CUTOVER_HASH_LENGTH]
    suffix = f"{_CUTOVER_ASIDE_MARKER}{digest}"
    prefix_bytes = _POSTGRES_IDENTIFIER_MAX_BYTES - len(suffix.encode())
    aside = f"{_truncate_utf8(target_db, prefix_bytes)}{suffix}"
    _validate_database_name(aside, "aside_db")
    return aside


def database_oid_query(db_name: str) -> str:
    """Build the scalar OID lookup used by cutover-capable adapters."""
    _validate_database_name(db_name, "db_name")
    return f"SELECT oid::text FROM pg_database WHERE datname = {quote_literal(db_name)};"


def _validated_oid(value: object, *, db_name: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise RuntimeError(f"Invalid PostgreSQL OID returned for database {db_name!r}")
    try:
        oid = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid PostgreSQL OID returned for database {db_name!r}") from exc
    if oid < 1 or oid > 0xFFFFFFFF:
        raise RuntimeError(f"Invalid PostgreSQL OID returned for database {db_name!r}")
    return oid


def database_oid(
    pg: SwapPsqlAdapter,
    db_name: str,
    *,
    maintenance_db: str = "postgres",
) -> int | None:
    """Return a database's stable OID, or ``None`` when it does not exist.

    New adapters can expose ``psql_scalar(database, sql)``. The fallback
    ``database_oid(name)`` hook keeps the primitive easy to integrate with
    adapters that already implement a native scalar lookup.
    """
    query_scalar = getattr(pg, "psql_scalar", None)
    if callable(query_scalar):
        return _validated_oid(
            query_scalar(maintenance_db, database_oid_query(db_name)),
            db_name=db_name,
        )

    lookup = getattr(pg, "database_oid", None)
    if callable(lookup):
        return _validated_oid(lookup(db_name), db_name=db_name)

    raise TypeError(
        "Crash-reconcilable database cutover requires an adapter with "
        "psql_scalar(database, sql) or database_oid(name)"
    )


@dataclass(frozen=True)
class DatabaseCutoverPlan:
    """Durable identity fence for one database cutover."""

    cutover_id: str
    incoming_db: str
    target_db: str
    aside_db: str
    incoming_oid: int
    target_oid: int | None
    maintenance_db: str = "postgres"

    def __post_init__(self) -> None:
        _validate_database_name(self.incoming_db, "incoming_db")
        _validate_database_name(self.target_db, "target_db")
        _validate_database_name(self.aside_db, "aside_db")
        _validate_database_name(self.maintenance_db, "maintenance_db")
        if not self.cutover_id:
            raise ValueError("cutover_id cannot be empty")
        if "\x00" in self.cutover_id:
            raise ValueError("cutover_id cannot contain NUL bytes")
        if self.incoming_db == self.target_db:
            raise ValueError("incoming_db must differ from target_db")
        if self.maintenance_db in {
            self.incoming_db,
            self.target_db,
            self.aside_db,
        }:
            raise ValueError("maintenance_db must be separate from cutover databases")
        expected_aside = database_cutover_aside_name(
            target_db=self.target_db,
            incoming_db=self.incoming_db,
            cutover_id=self.cutover_id,
        )
        if self.aside_db != expected_aside:
            raise ValueError("aside_db does not match the deterministic cutover name")
        if _validated_oid(self.incoming_oid, db_name=self.incoming_db) is None:
            raise ValueError("incoming_oid cannot be empty")
        _validated_oid(self.target_oid, db_name=self.target_db)
        if self.target_oid == self.incoming_oid:
            raise ValueError("incoming and target databases must have distinct OIDs")


class DatabaseCutoverState(str, Enum):
    READY = "ready"
    TARGET_MOVED_ASIDE = "target_moved_aside"
    PROMOTED = "promoted"
    FINALIZED = "finalized"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DatabaseCutoverStatus:
    plan: DatabaseCutoverPlan
    state: DatabaseCutoverState
    observed_incoming_oid: int | None
    observed_target_oid: int | None
    observed_aside_oid: int | None
    observation_complete: bool = True

    @property
    def promoted(self) -> bool | None:
        if self.state in {
            DatabaseCutoverState.PROMOTED,
            DatabaseCutoverState.FINALIZED,
        }:
            return True
        if self.state in {
            DatabaseCutoverState.READY,
            DatabaseCutoverState.TARGET_MOVED_ASIDE,
        }:
            return False
        return None


class DatabaseCutoverError(RuntimeError):
    """A cutover failure with the last reconciled physical state attached."""

    def __init__(
        self,
        message: str,
        status: DatabaseCutoverStatus,
        *,
        promoted: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self._promoted = promoted

    @property
    def promoted(self) -> bool | None:
        if self._promoted is not None:
            return self._promoted
        return self.status.promoted
        return self.status.promoted


class DatabaseCutoverConflict(DatabaseCutoverError):
    """Observed names/OIDs cannot belong to the recorded cutover."""


class DatabaseCutoverCleanupError(DatabaseCutoverError):
    """Promotion succeeded, but removal of the durably-released old DB failed."""

    def __init__(self, message: str, status: DatabaseCutoverStatus) -> None:
        super().__init__(message, status, promoted=True)


class DatabaseCutoverFenceError(RuntimeError):
    """The caller has not yet durably recorded the successful promotion."""


def _unknown_cutover_status(plan: DatabaseCutoverPlan) -> DatabaseCutoverStatus:
    return DatabaseCutoverStatus(
        plan=plan,
        state=DatabaseCutoverState.UNKNOWN,
        observed_incoming_oid=None,
        observed_target_oid=None,
        observed_aside_oid=None,
        observation_complete=False,
    )


def plan_database_cutover(
    pg: SwapPsqlAdapter,
    *,
    incoming_db: str,
    target_db: str,
    cutover_id: str,
    maintenance_db: str = "postgres",
) -> DatabaseCutoverPlan:
    """Capture the stable identities that must be persisted before promotion."""
    _validate_database_name(incoming_db, "incoming_db")
    _validate_database_name(target_db, "target_db")
    _validate_database_name(maintenance_db, "maintenance_db")
    if incoming_db == target_db:
        raise ValueError("incoming_db must differ from target_db")

    aside_db = database_cutover_aside_name(
        target_db=target_db,
        incoming_db=incoming_db,
        cutover_id=cutover_id,
    )
    if maintenance_db in {incoming_db, target_db, aside_db}:
        raise ValueError("maintenance_db must be separate from cutover databases")

    incoming_oid = database_oid(
        pg,
        incoming_db,
        maintenance_db=maintenance_db,
    )
    if incoming_oid is None:
        raise RuntimeError(f"Incoming database {incoming_db!r} does not exist")
    target_oid = database_oid(pg, target_db, maintenance_db=maintenance_db)
    aside_oid = database_oid(pg, aside_db, maintenance_db=maintenance_db)
    if aside_oid is not None:
        raise RuntimeError(
            f"Cutover aside database {aside_db!r} is already occupied; refusing to overwrite it"
        )
    if target_oid == incoming_oid:
        raise RuntimeError("Incoming and target database OIDs unexpectedly match")

    return DatabaseCutoverPlan(
        cutover_id=cutover_id,
        incoming_db=incoming_db,
        target_db=target_db,
        aside_db=aside_db,
        incoming_oid=incoming_oid,
        target_oid=target_oid,
        maintenance_db=maintenance_db,
    )


def _conflict_status(
    plan: DatabaseCutoverPlan,
    *,
    incoming_oid: int | None,
    target_oid: int | None,
    aside_oid: int | None,
) -> DatabaseCutoverStatus:
    return DatabaseCutoverStatus(
        plan=plan,
        state=DatabaseCutoverState.CONFLICT,
        observed_incoming_oid=incoming_oid,
        observed_target_oid=target_oid,
        observed_aside_oid=aside_oid,
    )


def reconcile_database_cutover(
    pg: SwapPsqlAdapter,
    plan: DatabaseCutoverPlan,
) -> DatabaseCutoverStatus:
    """Classify a cutover solely by the durable plan and stable database OIDs."""
    try:
        incoming_oid = database_oid(
            pg,
            plan.incoming_db,
            maintenance_db=plan.maintenance_db,
        )
        target_oid = database_oid(
            pg,
            plan.target_db,
            maintenance_db=plan.maintenance_db,
        )
        aside_oid = database_oid(
            pg,
            plan.aside_db,
            maintenance_db=plan.maintenance_db,
        )
    except Exception as exc:
        raise DatabaseCutoverError(
            "Could not determine the database cutover state",
            _unknown_cutover_status(plan),
        ) from exc

    observed = (incoming_oid, target_oid, aside_oid)
    if plan.target_oid is None:
        states = {
            (plan.incoming_oid, None, None): DatabaseCutoverState.READY,
            (None, plan.incoming_oid, None): DatabaseCutoverState.PROMOTED,
        }
    else:
        states = {
            (
                plan.incoming_oid,
                plan.target_oid,
                None,
            ): DatabaseCutoverState.READY,
            (
                plan.incoming_oid,
                None,
                plan.target_oid,
            ): DatabaseCutoverState.TARGET_MOVED_ASIDE,
            (
                None,
                plan.incoming_oid,
                plan.target_oid,
            ): DatabaseCutoverState.PROMOTED,
            (
                None,
                plan.incoming_oid,
                None,
            ): DatabaseCutoverState.FINALIZED,
        }
    state = states.get(observed)
    if state is None:
        status = _conflict_status(
            plan,
            incoming_oid=incoming_oid,
            target_oid=target_oid,
            aside_oid=aside_oid,
        )
        raise DatabaseCutoverConflict(
            "Database names/OIDs conflict with the recorded cutover plan",
            status,
        )
    return DatabaseCutoverStatus(
        plan=plan,
        state=state,
        observed_incoming_oid=incoming_oid,
        observed_target_oid=target_oid,
        observed_aside_oid=aside_oid,
    )


def _cutover_failure(
    pg: SwapPsqlAdapter,
    plan: DatabaseCutoverPlan,
    message: str,
) -> DatabaseCutoverError:
    try:
        status = reconcile_database_cutover(pg, plan)
    except DatabaseCutoverError as reconcile_error:
        return DatabaseCutoverError(
            f"{message}; the resulting state could not be determined",
            reconcile_error.status,
        )
    return DatabaseCutoverError(message, status)


def _restore_old_target_after_failed_promotion(
    pg: SwapPsqlAdapter,
    plan: DatabaseCutoverPlan,
    promotion_error: Exception,
) -> DatabaseCutoverStatus:
    try:
        rename_database(
            pg,
            plan.aside_db,
            plan.target_db,
            maintenance_db=plan.maintenance_db,
        )
    except Exception:
        failure = _cutover_failure(
            pg,
            plan,
            "Database promotion failed and restoring the old target also failed",
        )
        if failure.status.state is DatabaseCutoverState.READY:
            raise DatabaseCutoverError(
                "Database promotion failed; the original target was restored "
                "despite a lost rollback response",
                failure.status,
            ) from promotion_error
        if failure.promoted:
            return failure.status
        raise failure from promotion_error

    try:
        status = reconcile_database_cutover(pg, plan)
    except DatabaseCutoverError as reconcile_error:
        raise DatabaseCutoverError(
            "Database promotion failed; the rollback response was received "
            "but its resulting state is unknown",
            reconcile_error.status,
        ) from promotion_error
    if status.state is not DatabaseCutoverState.READY:
        raise DatabaseCutoverError(
            "Database promotion failed and rollback did not restore the ready state",
            status,
        ) from promotion_error
    raise DatabaseCutoverError(
        "Database promotion failed; the original target was restored",
        status,
    ) from promotion_error


def promote_database_cutover(
    pg: SwapPsqlAdapter,
    plan: DatabaseCutoverPlan,
) -> DatabaseCutoverStatus:
    """Promote the recorded incoming DB without deleting the old database.

    The returned ``PROMOTED`` status is the caller's signal to durably record
    cutover success. Only after that write succeeds may
    :func:`finalize_database_cutover` be called.
    """
    status = reconcile_database_cutover(pg, plan)
    if status.promoted:
        return status

    if status.state is DatabaseCutoverState.READY and plan.target_oid is not None:
        try:
            terminate_connections(
                pg,
                plan.target_db,
                maintenance_db=plan.maintenance_db,
            )
        except Exception as exc:
            failure = _cutover_failure(
                pg,
                plan,
                "Could not terminate connections to the original target database",
            )
            if failure.promoted:
                return failure.status
            raise failure from exc
        status = reconcile_database_cutover(pg, plan)
        if status.promoted:
            return status
        if status.state is DatabaseCutoverState.READY:
            try:
                rename_database(
                    pg,
                    plan.target_db,
                    plan.aside_db,
                    maintenance_db=plan.maintenance_db,
                )
            except Exception as exc:
                failure = _cutover_failure(
                    pg,
                    plan,
                    "Could not move the original target database aside",
                )
                if failure.status.state is DatabaseCutoverState.TARGET_MOVED_ASIDE:
                    status = failure.status
                elif failure.promoted:
                    return failure.status
                else:
                    raise failure from exc
            else:
                status = reconcile_database_cutover(pg, plan)
        elif status.state is not DatabaseCutoverState.TARGET_MOVED_ASIDE:
            raise DatabaseCutoverError(
                "Original target identity changed before it could be moved aside",
                status,
            )

    if status.promoted:
        return status
    if status.state not in {
        DatabaseCutoverState.READY,
        DatabaseCutoverState.TARGET_MOVED_ASIDE,
    }:
        raise DatabaseCutoverError(
            "Database cutover is not in a promotable state",
            status,
        )

    try:
        rename_database(
            pg,
            plan.incoming_db,
            plan.target_db,
            maintenance_db=plan.maintenance_db,
        )
    except Exception as exc:
        try:
            after_failure = reconcile_database_cutover(pg, plan)
        except DatabaseCutoverError as reconcile_error:
            raise DatabaseCutoverError(
                "Database promotion response was lost and the resulting "
                "state could not be determined",
                reconcile_error.status,
            ) from exc
        if after_failure.promoted:
            return after_failure
        if after_failure.state is DatabaseCutoverState.TARGET_MOVED_ASIDE:
            rollback_result = _restore_old_target_after_failed_promotion(
                pg,
                plan,
                exc,
            )
            if rollback_result.promoted:
                return rollback_result
        raise DatabaseCutoverError(
            "Database promotion failed before changing database identities",
            after_failure,
        ) from exc

    try:
        promoted = reconcile_database_cutover(pg, plan)
    except DatabaseCutoverError as reconcile_error:
        raise DatabaseCutoverError(
            "Database promotion command completed but the resulting state could not be determined",
            reconcile_error.status,
        ) from reconcile_error
    if not promoted.promoted:
        raise DatabaseCutoverError(
            "Database promotion command did not produce a promoted state",
            promoted,
        )
    return promoted


def finalize_database_cutover(
    pg: SwapPsqlAdapter,
    plan: DatabaseCutoverPlan,
    *,
    cutover_durably_recorded: bool,
) -> DatabaseCutoverStatus:
    """Remove the old DB only after the caller durably records promotion."""
    if not cutover_durably_recorded:
        raise DatabaseCutoverFenceError(
            "Refusing to remove the old database before cutover is durably recorded"
        )

    status = reconcile_database_cutover(pg, plan)
    if not status.promoted:
        raise DatabaseCutoverError(
            "Cannot finalize a database cutover that has not been promoted",
            status,
        )
    if status.state is DatabaseCutoverState.FINALIZED:
        return status
    if plan.target_oid is None:
        return replace(status, state=DatabaseCutoverState.FINALIZED)

    try:
        terminate_connections(
            pg,
            plan.aside_db,
            maintenance_db=plan.maintenance_db,
        )
    except Exception as exc:
        raise DatabaseCutoverCleanupError(
            "Database promotion succeeded, but connections to the old "
            "database could not be terminated",
            status,
        ) from exc

    try:
        status = reconcile_database_cutover(pg, plan)
    except DatabaseCutoverError as reconcile_error:
        raise DatabaseCutoverCleanupError(
            "Database promotion succeeded, but the old database identity "
            "could not be re-verified before cleanup",
            reconcile_error.status,
        ) from reconcile_error
    if status.state is DatabaseCutoverState.FINALIZED:
        return status
    if status.state is not DatabaseCutoverState.PROMOTED:
        raise DatabaseCutoverCleanupError(
            "Database promotion succeeded, but the old database identity changed before cleanup",
            status,
        )

    try:
        drop_database(
            pg,
            plan.aside_db,
            maintenance_db=plan.maintenance_db,
        )
    except Exception as exc:
        try:
            after_failure = reconcile_database_cutover(pg, plan)
        except DatabaseCutoverError as reconcile_error:
            raise DatabaseCutoverCleanupError(
                "Database promotion succeeded, but old-database cleanup "
                "failed and its result could not be determined",
                reconcile_error.status,
            ) from exc
        if after_failure.state is DatabaseCutoverState.FINALIZED:
            return after_failure
        raise DatabaseCutoverCleanupError(
            "Database promotion succeeded, but the old database remains "
            "and cleanup must be retried",
            after_failure,
        ) from exc

    try:
        finalized = reconcile_database_cutover(pg, plan)
    except DatabaseCutoverError as reconcile_error:
        raise DatabaseCutoverCleanupError(
            "Database promotion succeeded and cleanup returned, but the "
            "resulting state could not be determined",
            reconcile_error.status,
        ) from reconcile_error
    if finalized.state is not DatabaseCutoverState.FINALIZED:
        raise DatabaseCutoverCleanupError(
            "Database promotion succeeded, but old-database cleanup did "
            "not reach the finalized state",
            finalized,
        )
    return finalized


def terminate_connections(
    pg: SwapPsqlAdapter, db_name: str, *, maintenance_db: str = "postgres"
) -> None:
    pg.psql(
        maintenance_db,
        "SELECT pg_terminate_backend(pid) "
        "FROM pg_stat_activity "
        f"WHERE datname = {quote_literal(db_name)} AND pid <> pg_backend_pid();",
    )


def drop_database(pg: SwapPsqlAdapter, db_name: str, *, maintenance_db: str = "postgres") -> None:
    pg.psql(maintenance_db, f"DROP DATABASE IF EXISTS {quote_identifier(db_name)};")


def rename_database(
    pg: SwapPsqlAdapter, old_name: str, new_name: str, *, maintenance_db: str = "postgres"
) -> None:
    pg.psql(
        maintenance_db,
        f"ALTER DATABASE {quote_identifier(old_name)} RENAME TO {quote_identifier(new_name)};",
    )


def swap_temp_database(
    pg: SwapPsqlAdapter,
    *,
    temp_db: str,
    target_db: str,
    target_env_name: str,
    is_protected_fn: Callable[[str], bool] | None = None,
    maintenance_db: str = "postgres",
) -> None:
    """Promote a prepared temp DB into the target DB name, crash/failure-safe.

    ``is_protected_fn`` (typically ``OdooCtlConfig.is_protected``) guards
    against accidental promotion over a protected environment such as
    production; callers that omit it must enforce that policy themselves
    before invoking this function. Callers are expected to restore and
    sanitize ``temp_db`` before invoking this function.

    When the adapter exposes ``database_exists`` (the real Postgres adapters),
    the swap moves the live target aside, promotes the temp DB, and only drops
    the aside copy once the new DB is live — restoring the original if the
    promotion rename fails. The target name is therefore never left without a
    database (Opus M3 / codex re-scan #3). Adapters without ``database_exists``
    fall back to the drop-then-rename path.
    """
    if is_protected_fn is not None and is_protected_fn(target_env_name):
        raise RuntimeError(
            f"Refusing to swap a temporary database into protected environment '{target_env_name}'"
        )
    if temp_db == target_db:
        raise RuntimeError("Temporary database name must differ from target database name")

    exists_fn = getattr(pg, "database_exists", None)
    if not callable(exists_fn):
        # Legacy path for adapters that cannot query database existence. On a
        # crash between drop and rename the target name is briefly absent, but
        # the data survives under ``temp_db`` and is recoverable.
        terminate_connections(pg, target_db, maintenance_db=maintenance_db)
        drop_database(pg, target_db, maintenance_db=maintenance_db)
        rename_database(pg, temp_db, target_db, maintenance_db=maintenance_db)
        return

    aside_db = f"{target_db}__old_swap"
    if exists_fn(aside_db):
        terminate_connections(pg, aside_db, maintenance_db=maintenance_db)
        drop_database(pg, aside_db, maintenance_db=maintenance_db)

    target_existed = bool(exists_fn(target_db))
    if target_existed:
        terminate_connections(pg, target_db, maintenance_db=maintenance_db)
        rename_database(pg, target_db, aside_db, maintenance_db=maintenance_db)
    try:
        rename_database(pg, temp_db, target_db, maintenance_db=maintenance_db)
    except Exception:
        # Promotion failed: restore the original so the target is never absent.
        if target_existed and exists_fn(aside_db):
            rename_database(pg, aside_db, target_db, maintenance_db=maintenance_db)
        raise
    if target_existed and exists_fn(aside_db):
        terminate_connections(pg, aside_db, maintenance_db=maintenance_db)
        drop_database(pg, aside_db, maintenance_db=maintenance_db)
