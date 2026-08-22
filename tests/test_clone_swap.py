from __future__ import annotations

import pytest

from odooctl.odoo.db_swap import quote_identifier, quote_literal, swap_temp_database


class FakePostgres:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def psql(self, db_name: str, sql: str) -> None:
        self.calls.append((db_name, sql))


def test_swap_temp_database_terminates_drops_and_renames_target():
    pg = FakePostgres()

    swap_temp_database(pg, temp_db="odoo_staging_incoming", target_db="odoo_staging", target_env_name="staging")

    assert pg.calls == [
        (
            "postgres",
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'odoo_staging' AND pid <> pg_backend_pid();",
        ),
        ("postgres", 'DROP DATABASE IF EXISTS "odoo_staging";'),
        (
            "postgres",
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'odoo_staging_incoming' AND pid <> pg_backend_pid();",
        ),
        ("postgres", 'ALTER DATABASE "odoo_staging_incoming" RENAME TO "odoo_staging";'),
    ]


def test_swap_refuses_protected_target():
    pg = FakePostgres()
    with pytest.raises(RuntimeError, match="protected environment 'production'"):
        swap_temp_database(
            pg,
            temp_db="prod_incoming",
            target_db="prod",
            target_env_name="production",
            is_protected_fn=lambda name: name == "production",
        )
    assert pg.calls == []


def test_swap_refuses_protected_tier_target_with_non_production_name():
    pg = FakePostgres()
    with pytest.raises(RuntimeError, match="protected environment 'prod-eu'"):
        swap_temp_database(
            pg,
            temp_db="prod_eu_incoming",
            target_db="prod_eu",
            target_env_name="prod-eu",
            is_protected_fn=lambda name: True,  # config says tier: production
        )
    assert pg.calls == []


def test_swap_allows_unprotected_target_per_config_policy():
    pg = FakePostgres()
    swap_temp_database(
        pg,
        temp_db="odoo_staging_incoming",
        target_db="odoo_staging",
        target_env_name="staging",
        is_protected_fn=lambda name: name == "production",
    )
    assert len(pg.calls) == 4


def test_database_quoting_handles_special_characters():
    assert quote_identifier('weird"db') == '"weird""db"'
    assert quote_literal("team's db") == "'team''s db'"


# --- Re-scan #3: crash/failure-safe swap when the adapter can query existence ---


class ExistsAwarePostgres:
    """Fake with database_exists — exercises the rename-aside swap path."""

    def __init__(self, existing):
        self.databases = set(existing)
        self.renames: list[tuple[str, str]] = []
        self.drops: list[str] = []
        self.terminations: list[str] = []
        self.fail_promote = False

    def database_exists(self, name):
        return name in self.databases

    def psql(self, db_name, sql):
        if "pg_terminate_backend" in sql:
            import re
            name = re.search(r"datname = '(.+?)'", sql).group(1)
            self.terminations.append(name)
        elif "RENAME TO" in sql:
            # ALTER DATABASE "old" RENAME TO "new";
            import re
            m = re.search(r'ALTER DATABASE "(.+?)" RENAME TO "(.+?)"', sql)
            old, new = m.group(1), m.group(2)
            if self.fail_promote and old.endswith("_incoming"):
                raise RuntimeError("rename failed")
            self.renames.append((old, new))
            self.databases.discard(old)
            self.databases.add(new)
        elif "DROP DATABASE IF EXISTS" in sql:
            import re
            name = re.search(r'DROP DATABASE IF EXISTS "(.+?)"', sql).group(1)
            self.drops.append(name)
            self.databases.discard(name)


def test_swap_rename_aside_keeps_target_present_throughout():
    pg = ExistsAwarePostgres(existing={"odoo_staging", "odoo_staging_incoming"})
    swap_temp_database(pg, temp_db="odoo_staging_incoming", target_db="odoo_staging", target_env_name="staging")
    # Final state: target present with promoted data, aside dropped.
    assert "odoo_staging" in pg.databases
    assert "odoo_staging__old_swap" not in pg.databases
    assert "odoo_staging_incoming" not in pg.databases
    assert "odoo_staging_incoming" in pg.terminations


def test_swap_restores_original_when_promotion_rename_fails():
    pg = ExistsAwarePostgres(existing={"odoo_staging", "odoo_staging_incoming"})
    pg.fail_promote = True
    with pytest.raises(RuntimeError, match="rename failed"):
        swap_temp_database(pg, temp_db="odoo_staging_incoming", target_db="odoo_staging", target_env_name="staging")
    # The original database is restored — the target name is never left absent.
    assert "odoo_staging" in pg.databases


def test_swap_into_fresh_target_that_does_not_exist():
    pg = ExistsAwarePostgres(existing={"odoo_staging_incoming"})  # no live target yet
    swap_temp_database(pg, temp_db="odoo_staging_incoming", target_db="odoo_staging", target_env_name="staging")
    assert "odoo_staging" in pg.databases
