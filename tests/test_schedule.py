from pathlib import Path
import re
from types import SimpleNamespace

from typer.testing import CliRunner

from odooctl.commands.schedule import ScheduleSpec, build_spec, render
from odooctl.main import app

runner = CliRunner()


def _write_config(path: Path, *, project_name: str = "demo") -> None:
    path.write_text(
        f"""project:\n  name: {project_name}\n  odoo_version: "19.0"\nruntime:\n  compose_file: docker-compose.yml\nenvironments:\n  production:\n    branch: main\n    domain: odoo.example.com\n    db_name: odoo_prod\n    filestore_path: /var/lib/odoo/filestore/odoo_prod\nodoo:\n  image: registry/odoo:latest\n"""
    )


def _enable_pitr(path: Path) -> None:
    with path.open("a") as handle:
        handle.write(
            """pitr:
  enabled: true
  environment: production
  cluster_id: primary-cluster
  system_identifier: "7429384729384729"
  recovery_image: postgres@sha256:1111111111111111111111111111111111111111111111111111111111111111
  filestore_policy: database_only
  destination:
    bucket: pitr-archive
    prefix: demo/pitr
"""
        )


def test_render_systemd_timer_for_backup(tmp_path: Path):
    config = tmp_path / "odooctl.yml"
    _write_config(config)

    output = render(
        "backup",
        "production",
        str(config),
        interval="03:15",
        user="odoo",
        odooctl_bin="/usr/local/bin/odooctl",
    )
    unit_name = build_spec(
        "backup",
        "production",
        str(config),
        interval="03:15",
        user="odoo",
        odooctl_bin="/usr/local/bin/odooctl",
    ).unit_name

    assert f"# /etc/systemd/system/{unit_name}.service" in output
    assert "WorkingDirectory=" + str(tmp_path) in output
    assert "User=odoo" in output
    assert "ExecStart=/usr/local/bin/odooctl --project-dir" in output
    assert "backup production --verify --config" in output
    assert "OnCalendar=03:15" in output
    assert "Persistent=true" in output


def test_render_cron_alias_for_doctor(tmp_path: Path):
    config = tmp_path / "odooctl.yml"
    _write_config(config)

    output = render("doctor", "production", str(config), format="cron", interval="hourly")

    assert output.startswith("0 * * * * cd ")
    assert "odooctl --project-dir" in output
    assert " doctor --config " in output
    assert "doctor production" not in output


def test_schedule_cli_outputs_cron(tmp_path: Path):
    config = tmp_path / "odooctl.yml"
    _write_config(config)

    result = runner.invoke(
        app,
        [
            "schedule",
            "backup",
            "--env",
            "production",
            "--config",
            str(config),
            "--format",
            "cron",
            "--interval",
            "weekly",
            "--user",
            "odoo",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output.startswith("0 2 * * 0 odoo cd ")
    assert "backup production --verify" in result.output


def test_schedule_rejects_unknown_environment(tmp_path: Path):
    config = tmp_path / "odooctl.yml"
    _write_config(config)

    result = runner.invoke(app, ["schedule", "backup", "--env", "staging", "--config", str(config)])

    assert result.exit_code != 0
    assert "Unknown environment: staging" in result.output


def test_render_remote_verification_uses_real_backup_remote_command(tmp_path: Path):
    config = tmp_path / "odooctl.yml"
    _write_config(config)

    output = render(
        "backup-remote-verify",
        "production",
        str(config),
        interval="hourly",
    )

    assert "ExecStart=odooctl --project-dir" in output
    assert "backup-remote verify production --config" in output
    assert "OnCalendar=hourly" in output


def test_render_dr_drill_timer_uses_nested_dr_command(tmp_path: Path):
    config = tmp_path / "odooctl.yml"
    _write_config(config)

    output = render(
        "dr-drill",
        "production",
        str(config),
        interval="weekly",
    )
    unit_name = build_spec(
        "dr-drill",
        "production",
        str(config),
        interval="weekly",
    ).unit_name

    assert f"# /etc/systemd/system/{unit_name}.service" in output
    assert "dr drill production --config" in output
    assert "OnCalendar=weekly" in output


def test_render_pitr_schedules_use_registered_non_destructive_commands(
    tmp_path: Path,
):
    config = tmp_path / "odooctl.yml"
    _write_config(config)
    _enable_pitr(config)

    base = build_spec("pitr-base", "production", str(config))
    reconcile = build_spec(
        "pitr-reconcile",
        "production",
        str(config),
    )

    assert base.invocation_tokens[-6:] == (
        "pitr",
        "base",
        "create",
        "production",
        "--config",
        str(config),
    )
    assert reconcile.invocation_tokens[-6:] == (
        "pitr",
        "retention",
        "reconcile",
        "production",
        "--config",
        str(config),
    )
    assert "restore" not in base.invocation_tokens
    assert "restore" not in reconcile.invocation_tokens


def test_pitr_schedule_requires_enabled_bound_environment(tmp_path: Path):
    config = tmp_path / "odooctl.yml"
    _write_config(config)

    result = runner.invoke(
        app,
        [
            "schedule",
            "pitr-base",
            "--env",
            "production",
            "--config",
            str(config),
        ],
    )

    assert result.exit_code != 0
    assert "pitr.enabled: true" in result.output


def test_systemd_unit_namespace_is_stable_and_root_scoped(tmp_path: Path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_config = first_root / "odooctl.yml"
    second_config = second_root / "odooctl.yml"
    _write_config(first_config, project_name="shared-name")
    _write_config(second_config, project_name="shared-name")

    first = build_spec("backup", "production", str(first_config))
    first_again = build_spec("backup", "production", str(first_config))
    second = build_spec("backup", "production", str(second_config))

    assert first.unit_name == first_again.unit_name
    assert first.unit_name != second.unit_name
    assert re.fullmatch(r"[A-Za-z0-9_-]+", first.unit_name)


def test_systemd_unit_namespace_includes_safe_project_identity(tmp_path: Path):
    first_config = tmp_path / "first.yml"
    second_config = tmp_path / "second.yml"
    _write_config(first_config, project_name='"Sales / EU"')
    _write_config(second_config, project_name='"Sales : EU"')

    first = build_spec("backup", "production", str(first_config))
    second = build_spec("backup", "production", str(second_config))

    assert first.unit_name != second.unit_name
    assert "Sales-EU" in first.unit_name
    assert re.fullmatch(r"[A-Za-z0-9_-]+", first.unit_name)


def test_systemd_unit_digest_distinguishes_normalized_environment_collision(
    tmp_path: Path,
):
    common = {
        "command": "backup",
        "project_root": tmp_path,
        "config_path": tmp_path / "odooctl.yml",
        "interval": "daily",
        "project_name": "demo",
    }

    dotted = ScheduleSpec(environment="prod.eu", **common)
    dashed = ScheduleSpec(environment="prod-eu", **common)

    assert dotted.unit_name != dashed.unit_name
    assert dotted.unit_name.endswith("-backup-prod-eu")
    assert dashed.unit_name.endswith("-backup-prod-eu")


def test_systemd_schedule_loads_secrets_from_environment_file(tmp_path: Path):
    config = tmp_path / "odooctl.yml"
    _write_config(config)

    output = render(
        "backup",
        "production",
        str(config),
        environment_file="/etc/odooctl/demo.env",
    )

    assert "EnvironmentFile=/etc/odooctl/demo.env" in output
    assert "ODOO_DB_PASSWORD" not in output


def test_cron_schedule_sources_environment_file_without_rendering_secrets(tmp_path: Path):
    config = tmp_path / "odooctl.yml"
    _write_config(config)

    output = render(
        "dr-drill",
        "production",
        str(config),
        format="cron",
        environment_file=".odooctl/schedule secrets.env",
    )

    expected_file = tmp_path / ".odooctl" / "schedule secrets.env"
    assert f"set -a && . '{expected_file}' && set +a &&" in output
    assert "dr drill production" in output


def test_schedule_cli_accepts_environment_file(tmp_path: Path):
    config = tmp_path / "odooctl.yml"
    _write_config(config)

    result = runner.invoke(
        app,
        [
            "schedule",
            "backup-remote-verify",
            "--env",
            "production",
            "--config",
            str(config),
            "--environment-file",
            "/etc/odooctl/demo.env",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "EnvironmentFile=/etc/odooctl/demo.env" in result.output
    assert "backup-remote verify production" in result.output


def test_remote_verify_schedule_tokens_execute_registered_cli(tmp_path: Path, monkeypatch):
    config = tmp_path / "odooctl.yml"
    _write_config(config)
    monkeypatch.setattr(
        "odooctl.services.remote_backup.verify_remote_backup",
        lambda ctx, environment, backup: SimpleNamespace(
            backup_id="production_2026-07-30_020000",
            object_count=3,
            verified_at="2026-07-30T02:00:00Z",
            uri="s3://backups/production_2026-07-30_020000",
        ),
    )
    monkeypatch.setattr(
        "odooctl.services.remote_backup.prune_remote_backups",
        lambda ctx, *, environment, protected_backup_ids=(): SimpleNamespace(
            deleted_backup_ids=(),
            error=None,
        ),
    )
    spec = build_spec(
        "backup-remote-verify",
        "production",
        str(config),
    )

    result = runner.invoke(app, list(spec.invocation_tokens[1:]))

    assert result.exit_code == 0, result.output
    assert "verified 3 objects" in result.output


def test_remote_verify_schedule_fails_when_retention_reconciliation_alerts(
    tmp_path: Path,
    monkeypatch,
):
    config = tmp_path / "odooctl.yml"
    _write_config(config)
    monkeypatch.setattr(
        "odooctl.services.remote_backup.verify_remote_backup",
        lambda ctx, environment, backup: SimpleNamespace(
            backup_id="production_2026-07-30_020000",
            object_count=3,
            verified_at="2026-07-30T02:00:00Z",
            uri="s3://backups/production_2026-07-30_020000",
            manifest=SimpleNamespace(),
        ),
    )
    monkeypatch.setattr(
        "odooctl.services.remote_backup.prune_remote_backups",
        lambda ctx, *, environment, protected_backup_ids=(): SimpleNamespace(
            deleted_backup_ids=(),
            error="provider delete failed",
        ),
    )
    spec = build_spec(
        "backup-remote-verify",
        "production",
        str(config),
    )

    result = runner.invoke(app, list(spec.invocation_tokens[1:]))

    assert result.exit_code != 0
    assert "retention reconciliation failed" in str(result.exception)


def test_backup_schedule_tokens_execute_with_verification(tmp_path: Path, monkeypatch):
    config = tmp_path / "odooctl.yml"
    _write_config(config)
    observed = {}

    def fake_execute(environment, config_path, *, verify):
        observed.update(
            environment=environment,
            config_path=config_path,
            verify=verify,
        )
        return "production_2026-07-30_020000"

    monkeypatch.setattr("odooctl.commands.backup.execute", fake_execute)
    spec = build_spec("backup", "production", str(config))

    result = runner.invoke(app, list(spec.invocation_tokens[1:]))

    assert result.exit_code == 0, result.output
    assert observed["environment"] == "production"
    assert observed["verify"] is True


def test_doctor_schedule_tokens_execute_without_invalid_environment_arg(
    tmp_path: Path,
    monkeypatch,
):
    config = tmp_path / "odooctl.yml"
    _write_config(config)
    calls = []
    monkeypatch.setattr(
        "odooctl.commands.doctor.execute",
        lambda config_path, json_output=False: calls.append((config_path, json_output)),
    )
    spec = build_spec("doctor", "production", str(config))

    result = runner.invoke(app, list(spec.invocation_tokens[1:]))

    assert result.exit_code == 0, result.output
    assert calls == [(str(config), False)]


def test_dr_drill_schedule_tokens_execute_registered_cli(tmp_path: Path, monkeypatch):
    from odooctl.services.dr import DrDrillResult

    config = tmp_path / "odooctl.yml"
    _write_config(config)
    drill_calls = []

    def fake_drill(**kwargs):
        drill_calls.append(kwargs)
        return DrDrillResult(
            status="success",
            environment="production",
            backup_id="production_2026-07-30_020000",
            database="odoo_prod_dr_drill",
            filestore_path="/var/lib/odoo/filestore/odoo_prod_dr_drill",
        )

    monkeypatch.setattr(
        "odooctl.services.dr.run_dr_drill",
        fake_drill,
    )
    spec = build_spec("dr-drill", "production", str(config))

    result = runner.invoke(app, list(spec.invocation_tokens[1:]))

    assert result.exit_code == 0, result.output
    assert "DR drill for 'production': success" in result.output
    assert drill_calls[0]["expected_project"] == "demo"
