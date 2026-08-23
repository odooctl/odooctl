"""Tests for the m17 §3 pull-based sync service and CLI command."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from odooctl.main import app
from odooctl.services import sync as sync_svc
from odooctl.services.context import ServiceContext
from odooctl.services.models import DeployResult, SyncOutcome

cli_runner = CliRunner()

SYNC_CONFIG = """\
project:
  name: demo
  odoo_version: "19.0"
runtime:
  compose_file: docker-compose.yml
odoo:
  image: registry/odoo:latest
  service: odoo
environments:
  production:
    branch: main
    domain: odoo.example.com
    db_name: odoo_prod
    filestore_path: /srv/filestore/prod
    auto_deploy: true
  staging:
    branch: staging
    domain: staging.example.com
    db_name: odoo_staging
    filestore_path: /srv/filestore/staging
"""

FETCH = ("git", "fetch", "--all", "--quiet")


def _make_ctx(tmp_path: Path) -> ServiceContext:
    cfg_path = tmp_path / "odooctl.yml"
    cfg_path.write_text(SYNC_CONFIG)
    return ServiceContext.from_config_path(str(cfg_path), root=tmp_path)


def _make_run(mapping: dict):
    """Fake shell run: mapping values are stdout strings or (stdout, returncode)."""
    def fake_run(args, check=True, cwd=None, **kwargs):
        response = mapping.get(tuple(args), "")
        stdout, returncode = response if isinstance(response, tuple) else (response, 0)
        return SimpleNamespace(stdout=stdout, returncode=returncode, stderr="", args=list(args))
    return fake_run


class _FakeMetaStore:
    def __init__(self, deployments: dict):
        self._deployments = deployments

    def latest_deployment(self, environment: str) -> dict | None:
        return self._deployments.get(environment)


def _patch(monkeypatch, run_mapping: dict, deployments: dict) -> None:
    monkeypatch.setattr(sync_svc, "run", _make_run(run_mapping))
    monkeypatch.setattr(sync_svc, "MetadataStore", lambda root: _FakeMetaStore(deployments))


def _deploy_recorder(calls: list):
    def fake_deploy(ctx, environment):
        calls.append(environment)
        return DeployResult(environment=environment, status="success", backup_id="bk-1")
    return fake_deploy


# ─── check_sync / run_sync ───────────────────────────────────────────────────

def test_sync_up_to_date_is_noop(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path)
    _patch(monkeypatch, {
        ("git", "rev-parse", "--short", "main@{upstream}"): "abc1234",
    }, {"production": {"commit": "abc1234", "status": "success"}})

    calls: list = []
    outcome = sync_svc.run_sync(ctx, "production", deploy=_deploy_recorder(calls))

    assert outcome.status == "up_to_date"
    assert calls == []
    assert outcome.remote_commit == "abc1234"
    assert outcome.deployed_commit == "abc1234"


def test_sync_behind_with_auto_deploy_runs_deploy(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path)
    _patch(monkeypatch, {
        ("git", "rev-parse", "--short", "main@{upstream}"): "newHead",
        ("git", "rev-list", "--count", "oldHead..newHead"): "3",
        ("git", "rev-list", "--count", "newHead..oldHead"): "0",
    }, {"production": {"commit": "oldHead", "status": "success"}})

    calls: list = []
    outcome = sync_svc.run_sync(ctx, "production", deploy=_deploy_recorder(calls))

    assert outcome.status == "deployed"
    assert calls == ["production"]
    assert outcome.backup_id == "bk-1"
    assert outcome.behind == 3


def test_sync_behind_without_auto_deploy_is_disabled(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path)
    _patch(monkeypatch, {
        ("git", "rev-parse", "--short", "staging@{upstream}"): "newHead",
        ("git", "rev-list", "--count", "oldHead..newHead"): "2",
        ("git", "rev-list", "--count", "newHead..oldHead"): "0",
    }, {"staging": {"commit": "oldHead", "status": "success"}})

    calls: list = []
    outcome = sync_svc.run_sync(ctx, "staging", deploy=_deploy_recorder(calls))

    assert outcome.status == "disabled"
    assert calls == []
    assert "auto_deploy is false" in outcome.message


def test_sync_force_overrides_disabled_auto_deploy(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path)
    _patch(monkeypatch, {
        ("git", "rev-parse", "--short", "staging@{upstream}"): "newHead",
        ("git", "rev-list", "--count", "oldHead..newHead"): "2",
        ("git", "rev-list", "--count", "newHead..oldHead"): "0",
    }, {"staging": {"commit": "oldHead", "status": "success"}})

    calls: list = []
    outcome = sync_svc.run_sync(ctx, "staging", force=True, deploy=_deploy_recorder(calls))

    assert outcome.status == "deployed"
    assert calls == ["staging"]


def test_sync_diverged_is_noop(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path)
    _patch(monkeypatch, {
        ("git", "rev-parse", "--short", "main@{upstream}"): "newHead",
        ("git", "rev-list", "--count", "oldHead..newHead"): "2",
        ("git", "rev-list", "--count", "newHead..oldHead"): "1",
    }, {"production": {"commit": "oldHead", "status": "success"}})

    calls: list = []
    outcome = sync_svc.run_sync(ctx, "production", deploy=_deploy_recorder(calls))

    assert outcome.status == "diverged"
    assert calls == []


def test_sync_never_deployed_is_noop(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path)
    _patch(monkeypatch, {
        ("git", "rev-parse", "--short", "main@{upstream}"): "abc1234",
    }, {})

    calls: list = []
    outcome = sync_svc.run_sync(ctx, "production", deploy=_deploy_recorder(calls))

    assert outcome.status == "never_deployed"
    assert calls == []
    assert "odooctl deploy production" in outcome.message


def test_sync_no_remote_ref(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path)
    _patch(monkeypatch, {}, {"production": {"commit": "abc1234", "status": "success"}})

    outcome = sync_svc.check_sync(ctx, "production")

    assert outcome.status == "no_remote"


def test_sync_fetch_failure(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path)
    _patch(monkeypatch, {FETCH: ("", 128)}, {"production": {"commit": "abc1234", "status": "success"}})

    outcome = sync_svc.check_sync(ctx, "production")

    assert outcome.status == "fetch_failed"


def test_sync_falls_back_to_origin_ref(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path)
    _patch(monkeypatch, {
        ("git", "rev-parse", "--short", "main@{upstream}"): ("", 128),
        ("git", "rev-parse", "--short", "origin/main"): "abc1234",
    }, {"production": {"commit": "abc1234", "status": "success"}})

    outcome = sync_svc.check_sync(ctx, "production")

    assert outcome.status == "up_to_date"
    assert outcome.remote_commit == "abc1234"


def test_sync_reports_deploy_failed_instead_of_up_to_date(tmp_path, monkeypatch):
    """A failed deploy with no new remote commits must not read as up_to_date."""
    ctx = _make_ctx(tmp_path)
    _patch(monkeypatch, {
        ("git", "rev-parse", "--short", "main@{upstream}"): "abc1234",
    }, {"production": {"commit": "abc1234", "status": "failed", "message": "health check failed"}})

    calls: list = []
    outcome = sync_svc.run_sync(ctx, "production", deploy=_deploy_recorder(calls))

    assert outcome.status == "deploy_failed"
    assert calls == []
    assert "health check failed" in outcome.message
    assert outcome.status in sync_svc.ATTENTION_STATUSES


def test_sync_retries_failed_deploy_when_new_commits_arrive(tmp_path, monkeypatch):
    """New commits after a failed deploy flip back to behind → auto-heal."""
    ctx = _make_ctx(tmp_path)
    _patch(monkeypatch, {
        ("git", "rev-parse", "--short", "main@{upstream}"): "fixHead",
        ("git", "rev-list", "--count", "badHead..fixHead"): "1",
        ("git", "rev-list", "--count", "fixHead..badHead"): "0",
    }, {"production": {"commit": "badHead", "status": "failed"}})

    calls: list = []
    outcome = sync_svc.run_sync(ctx, "production", deploy=_deploy_recorder(calls))

    assert outcome.status == "deployed"
    assert calls == ["production"]


def test_sync_dirty_worktree_blocks_deploy_without_operation(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path)
    _patch(monkeypatch, {
        ("git", "rev-parse", "--short", "main@{upstream}"): "newHead",
        ("git", "rev-list", "--count", "oldHead..newHead"): "2",
        ("git", "rev-list", "--count", "newHead..oldHead"): "0",
        ("git", "status", "--porcelain"): " M odooctl.yml\n",
    }, {"production": {"commit": "oldHead", "status": "success"}})

    calls: list = []
    outcome = sync_svc.run_sync(ctx, "production", deploy=_deploy_recorder(calls))

    assert outcome.status == "dirty_worktree"
    assert calls == []
    assert "odooctl.yml" in outcome.message
    assert outcome.status in sync_svc.ATTENTION_STATUSES


def test_sync_dirty_worktree_not_checked_when_auto_deploy_disabled(tmp_path, monkeypatch):
    """Behind with auto_deploy off stays 'disabled' even if the tree is dirty."""
    ctx = _make_ctx(tmp_path)
    _patch(monkeypatch, {
        ("git", "rev-parse", "--short", "staging@{upstream}"): "newHead",
        ("git", "rev-list", "--count", "oldHead..newHead"): "2",
        ("git", "rev-list", "--count", "newHead..oldHead"): "0",
        ("git", "status", "--porcelain"): " M odooctl.yml\n",
    }, {"staging": {"commit": "oldHead", "status": "success"}})

    outcome = sync_svc.check_sync(ctx, "staging")

    assert outcome.status == "disabled"


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _invoke_cli(tmp_path, monkeypatch, outcome: SyncOutcome, extra_args: list | None = None):
    cfg_path = tmp_path / "odooctl.yml"
    cfg_path.write_text(SYNC_CONFIG)
    from odooctl.commands import sync as sync_cmd

    monkeypatch.setattr(sync_cmd, "run_sync", lambda ctx, env, force=False, deploy=None: outcome)
    args = ["sync", "production", "--config", str(cfg_path)] + (extra_args or [])
    return cli_runner.invoke(app, args)


def test_cli_sync_up_to_date_exits_zero(tmp_path, monkeypatch):
    outcome = SyncOutcome(environment="production", branch="main", status="up_to_date", message="ok")
    result = _invoke_cli(tmp_path, monkeypatch, outcome)
    assert result.exit_code == 0, result.output
    assert "up_to_date" in result.output


def test_cli_sync_diverged_exits_nonzero(tmp_path, monkeypatch):
    outcome = SyncOutcome(environment="production", branch="main", status="diverged", message="diverged")
    result = _invoke_cli(tmp_path, monkeypatch, outcome)
    assert result.exit_code == 1


def test_cli_sync_json_output(tmp_path, monkeypatch):
    import json

    outcome = SyncOutcome(
        environment="production", branch="main", status="deployed",
        remote_commit="abc", deployed_commit="old", ahead=0, behind=2,
        message="deployed", backup_id="bk-1",
    )
    result = _invoke_cli(tmp_path, monkeypatch, outcome, ["--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "deployed"
    assert data["backup_id"] == "bk-1"


# ─── schedule integration ────────────────────────────────────────────────────

def test_schedule_sync_defaults_to_five_minutes_systemd(tmp_path):
    from odooctl.commands.schedule import render

    cfg_path = tmp_path / "odooctl.yml"
    cfg_path.write_text(SYNC_CONFIG)
    output = render("sync", "production", str(cfg_path))
    assert "OnCalendar=*:0/5" in output
    assert "sync production --config" in output


def test_schedule_sync_defaults_to_five_minutes_cron(tmp_path):
    from odooctl.commands.schedule import render

    cfg_path = tmp_path / "odooctl.yml"
    cfg_path.write_text(SYNC_CONFIG)
    output = render("sync", "production", str(cfg_path), format="cron")
    assert output.startswith("*/5 * * * * cd ")
    assert " sync production --config " in output


def test_schedule_backup_still_defaults_to_daily(tmp_path):
    from odooctl.commands.schedule import render

    cfg_path = tmp_path / "odooctl.yml"
    cfg_path.write_text(SYNC_CONFIG)
    output = render("backup", "production", str(cfg_path))
    assert "OnCalendar=daily" in output
