from pathlib import Path

import pytest

from odooctl.config import example_config, load_config
from odooctl.odoo.sanitize import (
    default_sql,
    guarded_column_update,
    guarded_update,
    profile_sql,
    sanitize_database,
    verification_checks,
)


class FakePostgres:
    def psql(self, db_name, sql):
        pass

    def psql_file(self, db_name, sql_file):
        pass


def _config(tmp_path: Path):
    path = tmp_path / "odooctl.yml"
    path.write_text(example_config())
    return load_config(path)


def test_default_sanitization_disables_dangerous_integrations(tmp_path: Path):
    cfg = _config(tmp_path)
    sql = "\n".join(default_sql(cfg.env("staging"), cfg))
    assert "UPDATE ir_mail_server SET active = false" in sql
    assert "UPDATE fetchmail_server SET active = false" in sql
    assert "UPDATE ir_cron SET active = false" in sql
    assert "payment_provider" in sql
    assert "queue_job" in sql
    assert "base_automation" in sql
    assert "mail_mail" in sql
    assert "https://staging.odoo.example.com" in sql


def test_sanitization_preserves_exactly_one_invalid_outgoing_mail_sink(tmp_path: Path):
    cfg = _config(tmp_path)
    sql = "\n".join(default_sql(cfg.env("staging"), cfg))

    assert "UPDATE ir_mail_server SET active = false" in sql
    assert "neutralization - disable emails" in sql
    assert "smtp_host = ''invalid''" in sql
    assert "WHERE id = (SELECT MIN(id)" in sql


def test_missing_configured_sanitization_sql_fails(tmp_path: Path, monkeypatch):
    cfg = _config(tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        sanitize_database(FakePostgres(), "odoo_staging", cfg.env("staging"), cfg)


def test_sanitization_profiles_are_explicit_and_distinct(tmp_path: Path):
    cfg = _config(tmp_path)
    env = cfg.env("staging")

    strict_sql = "\n".join(profile_sql("strict", env, cfg))
    normal_sql = "\n".join(profile_sql("normal", env, cfg))
    minimal_sql = "\n".join(profile_sql("minimal", env, cfg))

    assert "UPDATE ir_mail_server SET active = false" in strict_sql
    assert "UPDATE ir_cron SET active = false" in strict_sql
    assert "UPDATE ir_mail_server SET active = false" in normal_sql
    assert "UPDATE ir_cron SET active = false" in normal_sql
    assert "UPDATE ir_mail_server SET active = false" in minimal_sql
    assert "key LIKE 'auth_%'" in strict_sql
    assert "key LIKE 'auth_%'" not in normal_sql


def test_minimal_profile_keeps_crons_disabled(tmp_path: Path):
    # Audit F7: crons must stay disabled even under the minimal profile — a
    # cloned production database with live crons can send mail and charge cards.
    cfg = _config(tmp_path)
    minimal_sql = "\n".join(profile_sql("minimal", cfg.env("staging"), cfg))
    assert "UPDATE ir_cron SET active = false" in minimal_sql


def test_minimal_profile_covers_mandatory_baseline(tmp_path: Path):
    cfg = _config(tmp_path)
    minimal_sql = "\n".join(profile_sql("minimal", cfg.env("staging"), cfg))
    assert "UPDATE ir_mail_server SET active = false" in minimal_sql
    assert "UPDATE fetchmail_server SET active = false" in minimal_sql
    assert "UPDATE ir_cron SET active = false" in minimal_sql
    assert "payment_provider" in minimal_sql
    assert "payment_acquirer" in minimal_sql
    assert "web.base.url" in minimal_sql
    assert "web.base.url.freeze" in minimal_sql


def test_minimal_profile_excludes_aggressive_credential_scrubs(tmp_path: Path):
    cfg = _config(tmp_path)
    minimal_sql = "\n".join(profile_sql("minimal", cfg.env("staging"), cfg))
    assert "auth_passkey_key" not in minimal_sql
    assert "auth_oauth_provider" not in minimal_sql
    assert "iap_account" not in minimal_sql


def test_default_sanitization_scrubs_webhooks_and_environment_secrets(tmp_path: Path):
    cfg = _config(tmp_path)

    sql = "\n".join(default_sql(cfg.env("staging"), cfg))

    assert "webhook" in sql
    assert "callback" in sql
    assert "ir_act_server" in sql
    assert "neutralization - disable webhook" in sql
    assert "api_key" in sql
    assert "secret" in sql
    assert "token" in sql


def test_sanitization_rotates_odoo_database_secret_after_credential_scrub(tmp_path: Path):
    cfg = _config(tmp_path)
    stmts = default_sql(cfg.env("staging"), cfg)
    scrub_index = next(
        i for i, sql in enumerate(stmts)
        if "key ILIKE '%api_key%'" in sql
    )
    rotate_index = next(
        i for i, sql in enumerate(stmts)
        if "VALUES ('database.secret', gen_random_uuid()::text)" in sql
    )

    assert rotate_index > scrub_index
    assert "ON CONFLICT (key) DO UPDATE" in stmts[rotate_index]


def test_verification_requires_nonempty_database_secret(tmp_path: Path):
    cfg = _config(tmp_path)
    checks = dict(verification_checks("normal", cfg.env("staging"), cfg))

    assert "database secret rotated" in checks
    assert "length(value) >= 32" in checks["database secret rotated"]


def test_base_url_is_rewritten_and_frozen(tmp_path: Path):
    cfg = _config(tmp_path)
    stmts = default_sql(cfg.env("staging"), cfg)
    sql = "\n".join(stmts)
    assert "WHERE key = 'web.base.url';" in sql
    assert "UPDATE ir_config_parameter SET value = 'True' WHERE key = 'web.base.url.freeze';" in sql
    # Freeze must be set even when the parameter does not exist yet.
    assert any(
        "INSERT INTO ir_config_parameter" in s and "web.base.url.freeze" in s and "WHERE NOT EXISTS" in s
        for s in stmts
    )


def test_base_url_statements_respect_rewrite_knob(tmp_path: Path):
    cfg = _config(tmp_path)
    cfg.sanitization.rewrite_base_url = False
    sql = "\n".join(default_sql(cfg.env("staging"), cfg))
    assert "web.base.url" not in sql


def test_payment_disabled_in_modern_and_legacy_tables(tmp_path: Path):
    cfg = _config(tmp_path)
    stmts = default_sql(cfg.env("staging"), cfg)
    provider = [s for s in stmts if "payment_provider" in s]
    acquirer = [s for s in stmts if "payment_acquirer" in s]
    assert provider and acquirer
    # Both are guarded so they no-op on Odoo versions missing the table.
    assert all("to_regclass('public.payment_provider')" in s for s in provider)
    assert all("to_regclass('public.payment_acquirer')" in s for s in acquirer)
    assert any("SET state = ''disabled''" in s for s in provider)
    assert any("SET state = ''disabled''" in s for s in acquirer)


def test_oauth_providers_disabled_and_secrets_cleared(tmp_path: Path):
    cfg = _config(tmp_path)
    stmts = default_sql(cfg.env("staging"), cfg)
    oauth = [s for s in stmts if "auth_oauth_provider" in s]
    assert oauth
    # Column-guarded: each statement no-ops when the column does not exist.
    assert all("information_schema.columns" in s for s in oauth)
    joined = "\n".join(oauth)
    assert "SET enabled = false" in joined
    assert "SET active = false" in joined
    assert "SET client_secret = NULL" in joined


def test_iap_tokens_cleared_guarded(tmp_path: Path):
    cfg = _config(tmp_path)
    stmts = default_sql(cfg.env("staging"), cfg)
    iap = [s for s in stmts if "iap_account" in s]
    assert iap
    assert all("information_schema.columns" in s for s in iap)
    assert any("SET account_token = NULL" in s for s in iap)


def test_odoo19_passkeys_deleted_guarded(tmp_path: Path):
    cfg = _config(tmp_path)
    stmts = default_sql(cfg.env("staging"), cfg)
    passkey = [s for s in stmts if "auth_passkey_key" in s]
    assert passkey
    # Guarded delete: no-ops on pre-19 databases without the table.
    assert all("to_regclass('public.auth_passkey_key')" in s for s in passkey)
    assert any("DELETE FROM auth_passkey_key" in s for s in passkey)


def test_guarded_update_noops_when_table_missing():
    stmt = guarded_update("does_not_exist", "UPDATE does_not_exist SET x = 1;")
    assert stmt.startswith("DO $$ BEGIN IF to_regclass('public.does_not_exist') IS NOT NULL THEN")
    assert "EXECUTE 'UPDATE does_not_exist SET x = 1;'" in stmt
    assert stmt.endswith("END IF; END $$;")


def test_guarded_column_update_noops_when_column_missing():
    stmt = guarded_column_update("some_table", "some_col", "UPDATE some_table SET some_col = NULL;")
    assert "information_schema.columns" in stmt
    assert "table_name = 'some_table'" in stmt
    assert "column_name = 'some_col'" in stmt
    assert "EXECUTE 'UPDATE some_table SET some_col = NULL;'" in stmt


def test_guarded_helpers_escape_single_quotes():
    stmt = guarded_update("t", "UPDATE t SET state = 'disabled';")
    assert "''disabled''" in stmt
    stmt = guarded_column_update("t", "c", "UPDATE t SET c = 'x';")
    assert "''x''" in stmt


def test_optional_table_verification_queries_are_planned_dynamically(tmp_path: Path):
    cfg = _config(tmp_path)
    checks = dict(verification_checks("normal", cfg.env("staging"), cfg))

    for name in (
        "incoming mail servers disabled",
        "payment providers disabled",
        "legacy payment acquirers disabled",
        "server action webhooks neutralized",
    ):
        assert "EXECUTE 'SELECT EXISTS (" in checks[name]
