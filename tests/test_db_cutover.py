from __future__ import annotations

import re

import pytest

from odooctl.odoo.db_swap import (
    DatabaseCutoverCleanupError,
    DatabaseCutoverConflict,
    DatabaseCutoverError,
    DatabaseCutoverFenceError,
    DatabaseCutoverState,
    database_cutover_aside_name,
    database_oid_query,
    finalize_database_cutover,
    plan_database_cutover,
    promote_database_cutover,
    reconcile_database_cutover,
)


def _unquote_identifier(value: str) -> str:
    assert value.startswith('"') and value.endswith('"')
    return value[1:-1].replace('""', '"')


class CutoverPostgres:
    def __init__(self, databases: dict[str, int]):
        self.databases = dict(databases)
        self.sql: list[tuple[str, str]] = []
        self.scalar_sql: list[tuple[str, str]] = []
        self.fail_rename_before: set[tuple[str, str]] = set()
        self.fail_rename_after: set[tuple[str, str]] = set()
        self.fail_drop_before: set[str] = set()
        self.fail_drop_after: set[str] = set()
        self.fail_scalar = False

    def psql_scalar(self, db_name: str, sql: str) -> str | None:
        self.scalar_sql.append((db_name, sql))
        if self.fail_scalar:
            raise RuntimeError("OID query unavailable")
        for name, oid in self.databases.items():
            if sql == database_oid_query(name):
                return str(oid)
        return None

    def psql(self, db_name: str, sql: str) -> None:
        self.sql.append((db_name, sql))
        rename = re.fullmatch(
            r'ALTER DATABASE (".*") RENAME TO (".*");',
            sql,
        )
        if rename:
            old_name = _unquote_identifier(rename.group(1))
            new_name = _unquote_identifier(rename.group(2))
            transition = (old_name, new_name)
            if transition in self.fail_rename_before:
                self.fail_rename_before.remove(transition)
                raise RuntimeError("rename failed before commit")
            oid = self.databases.pop(old_name)
            if new_name in self.databases:
                raise RuntimeError("destination already exists")
            self.databases[new_name] = oid
            if transition in self.fail_rename_after:
                self.fail_rename_after.remove(transition)
                raise RuntimeError("rename response lost")
            return

        drop = re.fullmatch(r'DROP DATABASE IF EXISTS (".*");', sql)
        if drop:
            name = _unquote_identifier(drop.group(1))
            if name in self.fail_drop_before:
                self.fail_drop_before.remove(name)
                raise RuntimeError("drop failed before commit")
            self.databases.pop(name, None)
            if name in self.fail_drop_after:
                self.fail_drop_after.remove(name)
                raise RuntimeError("drop response lost")


def _plan(pg: CutoverPostgres):
    return plan_database_cutover(
        pg,
        incoming_db="odoo_recovered",
        target_db="odoo",
        cutover_id="restore-20260730",
    )


def test_aside_name_is_deterministic_unique_and_within_postgres_limit():
    first = database_cutover_aside_name(
        target_db="é" * 30,
        incoming_db="odoo_recovered",
        cutover_id="restore-one",
    )
    repeated = database_cutover_aside_name(
        target_db="é" * 30,
        incoming_db="odoo_recovered",
        cutover_id="restore-one",
    )
    other = database_cutover_aside_name(
        target_db="é" * 30,
        incoming_db="odoo_recovered",
        cutover_id="restore-two",
    )

    assert first == repeated
    assert first != other
    assert len(first.encode()) <= 63
    assert "__odooctl_cutover_" in first


def test_oid_query_safely_quotes_database_literal():
    assert database_oid_query("team's db") == (
        "SELECT oid::text FROM pg_database WHERE datname = 'team''s db';"
    )


def test_plan_captures_stable_oids_and_refuses_occupied_aside():
    pg = CutoverPostgres({"odoo": 100, "odoo_recovered": 200})
    plan = _plan(pg)

    assert plan.target_oid == 100
    assert plan.incoming_oid == 200
    assert pg.scalar_sql

    pg.databases[plan.aside_db] = 300
    with pytest.raises(RuntimeError, match="already occupied"):
        _plan(pg)


def test_promote_preserves_old_database_until_durable_finalize():
    pg = CutoverPostgres({"odoo": 100, "odoo_recovered": 200})
    plan = _plan(pg)

    promoted = promote_database_cutover(pg, plan)

    assert promoted.state is DatabaseCutoverState.PROMOTED
    assert promoted.promoted is True
    assert pg.databases == {"odoo": 200, plan.aside_db: 100}

    with pytest.raises(DatabaseCutoverFenceError):
        finalize_database_cutover(
            pg,
            plan,
            cutover_durably_recorded=False,
        )
    assert pg.databases == {"odoo": 200, plan.aside_db: 100}

    finalized = finalize_database_cutover(
        pg,
        plan,
        cutover_durably_recorded=True,
    )
    assert finalized.state is DatabaseCutoverState.FINALIZED
    assert pg.databases == {"odoo": 200}


def test_promote_into_fresh_target_is_retry_safe_and_needs_no_cleanup():
    pg = CutoverPostgres({"odoo_recovered": 200})
    plan = _plan(pg)
    assert plan.target_oid is None

    first = promote_database_cutover(pg, plan)
    retry = promote_database_cutover(pg, plan)
    finalized = finalize_database_cutover(
        pg,
        plan,
        cutover_durably_recorded=True,
    )

    assert first.state is DatabaseCutoverState.PROMOTED
    assert retry.state is DatabaseCutoverState.PROMOTED
    assert finalized.state is DatabaseCutoverState.FINALIZED
    assert pg.databases == {"odoo": 200}


def test_retry_resumes_after_crash_between_the_two_renames():
    pg = CutoverPostgres({"odoo": 100, "odoo_recovered": 200})
    plan = _plan(pg)
    pg.databases[plan.aside_db] = pg.databases.pop("odoo")

    interrupted = reconcile_database_cutover(pg, plan)
    completed = promote_database_cutover(pg, plan)

    assert interrupted.state is DatabaseCutoverState.TARGET_MOVED_ASIDE
    assert completed.state is DatabaseCutoverState.PROMOTED
    assert pg.databases == {"odoo": 200, plan.aside_db: 100}


def test_lost_move_aside_response_is_reconciled_and_promotion_continues():
    pg = CutoverPostgres({"odoo": 100, "odoo_recovered": 200})
    plan = _plan(pg)
    pg.fail_rename_after.add(("odoo", plan.aside_db))

    status = promote_database_cutover(pg, plan)

    assert status.state is DatabaseCutoverState.PROMOTED
    assert pg.databases == {"odoo": 200, plan.aside_db: 100}


def test_lost_promotion_response_is_reconciled_as_success():
    pg = CutoverPostgres({"odoo": 100, "odoo_recovered": 200})
    plan = _plan(pg)
    pg.fail_rename_after.add(("odoo_recovered", "odoo"))

    status = promote_database_cutover(pg, plan)

    assert status.state is DatabaseCutoverState.PROMOTED
    assert status.promoted is True
    assert pg.databases == {"odoo": 200, plan.aside_db: 100}


def test_failed_promotion_rolls_old_database_back_to_target():
    pg = CutoverPostgres({"odoo": 100, "odoo_recovered": 200})
    plan = _plan(pg)
    pg.fail_rename_before.add(("odoo_recovered", "odoo"))

    with pytest.raises(
        DatabaseCutoverError,
        match="original target was restored",
    ) as raised:
        promote_database_cutover(pg, plan)

    assert raised.value.promoted is False
    assert raised.value.status.state is DatabaseCutoverState.READY
    assert pg.databases == {"odoo": 100, "odoo_recovered": 200}


def test_failed_rollback_reports_old_database_safely_moved_aside():
    pg = CutoverPostgres({"odoo": 100, "odoo_recovered": 200})
    plan = _plan(pg)
    pg.fail_rename_before.add(("odoo_recovered", "odoo"))
    pg.fail_rename_before.add((plan.aside_db, "odoo"))

    with pytest.raises(DatabaseCutoverError) as raised:
        promote_database_cutover(pg, plan)

    assert raised.value.promoted is False
    assert raised.value.status.state is DatabaseCutoverState.TARGET_MOVED_ASIDE
    assert pg.databases == {
        "odoo_recovered": 200,
        plan.aside_db: 100,
    }


def test_lost_rollback_response_reports_original_target_restored():
    pg = CutoverPostgres({"odoo": 100, "odoo_recovered": 200})
    plan = _plan(pg)
    pg.fail_rename_before.add(("odoo_recovered", "odoo"))
    pg.fail_rename_after.add((plan.aside_db, "odoo"))

    with pytest.raises(
        DatabaseCutoverError,
        match="restored despite a lost rollback response",
    ) as raised:
        promote_database_cutover(pg, plan)

    assert raised.value.promoted is False
    assert raised.value.status.state is DatabaseCutoverState.READY
    assert pg.databases == {"odoo": 100, "odoo_recovered": 200}


def test_lost_drop_response_is_reconciled_as_finalized():
    pg = CutoverPostgres({"odoo": 100, "odoo_recovered": 200})
    plan = _plan(pg)
    promote_database_cutover(pg, plan)
    pg.fail_drop_after.add(plan.aside_db)

    finalized = finalize_database_cutover(
        pg,
        plan,
        cutover_durably_recorded=True,
    )

    assert finalized.state is DatabaseCutoverState.FINALIZED
    assert finalized.promoted is True
    assert pg.databases == {"odoo": 200}


def test_finalize_retry_reconciles_already_removed_old_database():
    pg = CutoverPostgres({"odoo": 100, "odoo_recovered": 200})
    plan = _plan(pg)
    promote_database_cutover(pg, plan)
    finalize_database_cutover(
        pg,
        plan,
        cutover_durably_recorded=True,
    )

    retried = finalize_database_cutover(
        pg,
        plan,
        cutover_durably_recorded=True,
    )

    assert retried.state is DatabaseCutoverState.FINALIZED
    assert retried.promoted is True
    assert pg.databases == {"odoo": 200}


def test_cleanup_failure_explicitly_reports_promotion_and_preserves_old_db():
    pg = CutoverPostgres({"odoo": 100, "odoo_recovered": 200})
    plan = _plan(pg)
    promote_database_cutover(pg, plan)
    pg.fail_drop_before.add(plan.aside_db)

    with pytest.raises(
        DatabaseCutoverCleanupError,
        match="promotion succeeded",
    ) as raised:
        finalize_database_cutover(
            pg,
            plan,
            cutover_durably_recorded=True,
        )

    assert raised.value.promoted is True
    assert raised.value.status.state is DatabaseCutoverState.PROMOTED
    assert pg.databases == {"odoo": 200, plan.aside_db: 100}

    retried = finalize_database_cutover(
        pg,
        plan,
        cutover_durably_recorded=True,
    )
    assert retried.state is DatabaseCutoverState.FINALIZED


@pytest.mark.parametrize(
    ("location", "foreign_oid"),
    [
        ("odoo", 999),
        ("odoo_recovered", 999),
    ],
)
def test_reconcile_refuses_reused_database_names(location, foreign_oid):
    pg = CutoverPostgres({"odoo": 100, "odoo_recovered": 200})
    plan = _plan(pg)
    pg.databases[location] = foreign_oid

    with pytest.raises(DatabaseCutoverConflict) as raised:
        reconcile_database_cutover(pg, plan)

    assert raised.value.status.state is DatabaseCutoverState.CONFLICT
    assert raised.value.promoted is None


def test_reconcile_refuses_foreign_database_at_aside_name():
    pg = CutoverPostgres({"odoo": 100, "odoo_recovered": 200})
    plan = _plan(pg)
    pg.databases[plan.aside_db] = 999

    with pytest.raises(DatabaseCutoverConflict):
        promote_database_cutover(pg, plan)

    assert pg.databases[plan.aside_db] == 999


def test_state_lookup_failure_is_typed_as_unknown():
    pg = CutoverPostgres({"odoo": 100, "odoo_recovered": 200})
    plan = _plan(pg)
    pg.fail_scalar = True

    with pytest.raises(DatabaseCutoverError) as raised:
        reconcile_database_cutover(pg, plan)

    assert raised.value.status.state is DatabaseCutoverState.UNKNOWN
    assert raised.value.promoted is None
