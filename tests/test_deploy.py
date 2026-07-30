from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from odooctl.commands import deploy as deploy_cmd
from odooctl.metadata.models import SnapshotManifest
from odooctl.services import deploy as deploy_svc
from odooctl.services.models import BackupResult
from odooctl.services.snapshots import SnapshotCreateFailed


class DummyStore:
    def __init__(self):
        self.saved = []

    def save_deployment(self, metadata):
        self.saved.append(metadata)
        return Path("/tmp/deployment.json")


class DummyCompose:
    def __init__(self, compose_file: str, **kwargs):
        self.compose_file = compose_file
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def pull(self, service: str | None = None):
        self.calls.append(("pull", (service,)))

    def up(self, service: str | None = None):
        self.calls.append(("up", (service,)))

    def restart(self, service: str):
        self.calls.append(("restart", (service,)))


class DummyPostgres:
    def __init__(self, config):
        self.config = config

    def ping(self, db_name: str):
        return None


CONFIG = """project:\n  name: demo\n  odoo_version: \"19.0\"\nruntime:\n  compose_file: docker-compose.yml\nhealthcheck:\n  path: /web/health\n  timeout_seconds: 10\n  retries: 3\n  interval_seconds: 1\nodoo:\n  image: registry/odoo:latest\n  service: odoo\nenvironments:\n  production:\n    branch: main\n    domain: odoo.example.com\n    db_name: odoo_prod\n    filestore_path: /srv/filestore/prod\n    update_modules: [sale, stock]\n    sanitize: true\n  staging:\n    branch: staging\n    domain: staging.example.com\n    db_name: odoo_staging\n    filestore_path: /srv/filestore/staging\n    update_modules: [sale]\n    sanitize: true\n"""


def test_deploy_production_runs_backup_pull_update_and_records_metadata(tmp_path: Path, monkeypatch):
    config = tmp_path / "odooctl.yml"
    config.write_text(CONFIG.replace("/srv/filestore/prod", str(tmp_path / "srv/filestore/prod")))
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "srv/filestore/prod").mkdir(parents=True)
    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")

    events: list[tuple[str, tuple[object, ...]]] = []
    store = DummyStore()
    compose = DummyCompose("docker-compose.yml")

    monkeypatch.setattr(deploy_svc, "backup_execute", lambda ctx, environment: (events.append(("backup", (environment,))) or BackupResult(backup_id="production_2026")))
    monkeypatch.setattr(deploy_svc, "git_commit", lambda cwd=None: "feedbeef")
    monkeypatch.setattr(deploy_svc, "run", lambda args, stream=True, cwd=None: events.append(("run", (tuple(args), stream))))
    monkeypatch.setattr(deploy_svc, "PostgresAdapter", DummyPostgres)
    monkeypatch.setattr(deploy_svc, "DockerComposeAdapter", lambda compose_file, **kwargs: compose)
    monkeypatch.setattr(deploy_svc, "update_modules_compose", lambda compose_obj, service, db_name, modules, **kwargs: events.append(("update", (service, db_name, tuple(modules)))))
    monkeypatch.setattr(deploy_svc, "check_url", lambda url, **kwargs: events.append(("healthcheck", (url, kwargs["timeout"], kwargs["retries"], kwargs["interval"]))))
    monkeypatch.setattr(deploy_svc, "MetadataStore", lambda root: store)
    monkeypatch.setattr(deploy_svc, "_assert_clean_worktree", lambda *args, **kwargs: None)

    deploy_cmd.execute("production", "main", str(config))

    assert events[0] == ("backup", ("production",))
    assert events[1] == ("run", (("git", "fetch", "--all"), True))
    assert events[2] == ("run", (("git", "checkout", "main"), True))
    assert events[3] == ("run", (("git", "pull", "--ff-only"), True))
    assert compose.calls[:2] == [("pull", ("odoo",)), ("up", ("odoo",))]
    assert events[4] == ("update", ("odoo", "odoo_prod", ("sale", "stock")))
    assert events[5] == ("healthcheck", ("https://odoo.example.com/web/health", 10, 3, 1))
    assert store.saved[-1].status == "success"
    assert store.saved[-1].backup == "production_2026"
    assert store.saved[-1].commit == "feedbeef"
    assert store.saved[-1].message is None


TIER_CONFIG = """project:\n  name: demo\n  odoo_version: \"19.0\"\nruntime:\n  compose_file: docker-compose.yml\nhealthcheck:\n  path: /web/health\n  timeout_seconds: 10\n  retries: 3\n  interval_seconds: 1\nodoo:\n  image: registry/odoo:latest\n  service: odoo\nenvironments:\n  prod-eu:\n    tier: production\n    branch: main\n    domain: odoo.example.com\n    db_name: odoo_prod\n    filestore_path: /srv/filestore/prod\n    update_modules: [sale, stock]\n    sanitize: true\n  staging:\n    branch: staging\n    domain: staging.example.com\n    db_name: odoo_staging\n    filestore_path: /srv/filestore/staging\n    update_modules: [sale]\n    sanitize: true\n"""


def _patch_happy_deploy(monkeypatch, events, store, compose):
    monkeypatch.setattr(deploy_svc, "backup_execute", lambda ctx, environment: (events.append(("backup", (environment,))) or BackupResult(backup_id="backup_2026")))
    monkeypatch.setattr(deploy_svc, "git_commit", lambda cwd=None: "feedbeef")
    monkeypatch.setattr(deploy_svc, "run", lambda args, stream=True, cwd=None: events.append(("run", (tuple(args), stream))))
    monkeypatch.setattr(deploy_svc, "PostgresAdapter", DummyPostgres)
    monkeypatch.setattr(deploy_svc, "DockerComposeAdapter", lambda compose_file, **kwargs: compose)
    monkeypatch.setattr(deploy_svc, "update_modules_compose", lambda compose_obj, service, db_name, modules, **kwargs: events.append(("update", (service, db_name, tuple(modules)))))
    monkeypatch.setattr(deploy_svc, "check_url", lambda url, **kwargs: events.append(("healthcheck", (url,))))
    monkeypatch.setattr(deploy_svc, "MetadataStore", lambda root: store)
    monkeypatch.setattr(deploy_svc, "_assert_clean_worktree", lambda *args, **kwargs: None)


def test_deploy_protected_tier_env_runs_pre_deploy_backup(tmp_path: Path, monkeypatch):
    """An env named something other than 'production' but with tier: production
    must get the same pre-deploy backup protection."""
    config = tmp_path / "odooctl.yml"
    config.write_text(TIER_CONFIG.replace("/srv/filestore/prod", str(tmp_path / "srv/filestore/prod")))
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "srv/filestore/prod").mkdir(parents=True)
    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")

    events: list[tuple[str, tuple[object, ...]]] = []
    store = DummyStore()
    compose = DummyCompose("docker-compose.yml")
    _patch_happy_deploy(monkeypatch, events, store, compose)

    deploy_cmd.execute("prod-eu", "main", str(config))

    assert events[0] == ("backup", ("prod-eu",))
    assert store.saved[-1].status == "success"
    assert store.saved[-1].backup == "backup_2026"


def test_deploy_unprotected_env_skips_pre_deploy_backup(tmp_path: Path, monkeypatch):
    config = tmp_path / "odooctl.yml"
    config.write_text(TIER_CONFIG.replace("/srv/filestore/staging", str(tmp_path / "srv/filestore/staging")))
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "srv/filestore/staging").mkdir(parents=True)
    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")

    events: list[tuple[str, tuple[object, ...]]] = []
    store = DummyStore()
    compose = DummyCompose("docker-compose.yml")
    _patch_happy_deploy(monkeypatch, events, store, compose)

    deploy_cmd.execute("staging", "staging", str(config))

    assert not any(event == "backup" for event, _ in events)
    assert store.saved[-1].backup is None
    assert store.saved[-1].status == "success"


def test_deploy_protected_tier_env_restarts_on_failure(tmp_path: Path, monkeypatch):
    """The recovery restart guard must also be config-driven, not name-driven."""
    config = tmp_path / "odooctl.yml"
    config.write_text(TIER_CONFIG.replace("/srv/filestore/prod", str(tmp_path / "srv/filestore/prod")))
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "srv/filestore/prod").mkdir(parents=True)
    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")

    store = DummyStore()
    compose = DummyCompose("docker-compose.yml")

    monkeypatch.setattr(deploy_svc, "backup_execute", lambda ctx, environment: BackupResult(backup_id="backup_2026"))
    monkeypatch.setattr(deploy_svc, "git_commit", lambda cwd=None: "feedbeef")
    monkeypatch.setattr(deploy_svc, "run", lambda args, stream=True, cwd=None: None)
    monkeypatch.setattr(deploy_svc, "PostgresAdapter", DummyPostgres)
    monkeypatch.setattr(deploy_svc, "DockerComposeAdapter", lambda compose_file, **kwargs: compose)
    monkeypatch.setattr(deploy_svc, "update_modules_compose", lambda *args, **kwargs: None)
    monkeypatch.setattr(deploy_svc, "check_url", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("healthcheck failed")))
    monkeypatch.setattr(deploy_svc, "MetadataStore", lambda root: store)
    monkeypatch.setattr(deploy_svc, "_assert_clean_worktree", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError):
        deploy_cmd.execute("prod-eu", "main", str(config))

    assert compose.calls[-1] == ("restart", ("odoo",))
    assert store.saved[-1].status == "failed"


def test_deploy_production_restarts_on_failure_and_records_message(tmp_path: Path, monkeypatch):
    config = tmp_path / "odooctl.yml"
    config.write_text(CONFIG.replace("/srv/filestore/prod", str(tmp_path / "srv/filestore/prod")))
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "srv/filestore/prod").mkdir(parents=True)
    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")

    store = DummyStore()
    compose = DummyCompose("docker-compose.yml")

    monkeypatch.setattr(deploy_svc, "backup_execute", lambda ctx, environment: BackupResult(backup_id="production_2026"))
    monkeypatch.setattr(deploy_svc, "git_commit", lambda cwd=None: "feedbeef")
    monkeypatch.setattr(deploy_svc, "run", lambda args, stream=True, cwd=None: None)
    monkeypatch.setattr(deploy_svc, "PostgresAdapter", DummyPostgres)
    monkeypatch.setattr(deploy_svc, "DockerComposeAdapter", lambda compose_file, **kwargs: compose)
    monkeypatch.setattr(deploy_svc, "update_modules_compose", lambda *args, **kwargs: None)
    monkeypatch.setattr(deploy_svc, "check_url", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("healthcheck failed")))
    monkeypatch.setattr(deploy_svc, "MetadataStore", lambda root: store)
    monkeypatch.setattr(deploy_svc, "_assert_clean_worktree", lambda *args, **kwargs: None)

    try:
        deploy_cmd.execute("production", "main", str(config))
    except RuntimeError:
        pass

    assert compose.calls[-1] == ("restart", ("odoo",))
    assert store.saved[-1].status == "failed"
    assert "healthcheck failed" in (store.saved[-1].message or "")


def test_deploy_production_records_recovery_restart_failure_honestly(tmp_path: Path, monkeypatch):
    config = tmp_path / "odooctl.yml"
    config.write_text(CONFIG.replace("/srv/filestore/prod", str(tmp_path / "srv/filestore/prod")))
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "srv/filestore/prod").mkdir(parents=True)
    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")

    store = DummyStore()

    class FailingRestartCompose(DummyCompose):
        def restart(self, service: str):
            super().restart(service)
            raise RuntimeError("container restart failed")

    compose = FailingRestartCompose("docker-compose.yml")

    monkeypatch.setattr(deploy_svc, "backup_execute", lambda ctx, environment: BackupResult(backup_id="production_2026"))
    monkeypatch.setattr(deploy_svc, "git_commit", lambda cwd=None: "feedbeef")
    monkeypatch.setattr(deploy_svc, "run", lambda args, stream=True, cwd=None: None)
    monkeypatch.setattr(deploy_svc, "PostgresAdapter", DummyPostgres)
    monkeypatch.setattr(deploy_svc, "DockerComposeAdapter", lambda compose_file, **kwargs: compose)
    monkeypatch.setattr(deploy_svc, "update_modules_compose", lambda *args, **kwargs: None)
    monkeypatch.setattr(deploy_svc, "check_url", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("healthcheck failed")))
    monkeypatch.setattr(deploy_svc, "MetadataStore", lambda root: store)
    monkeypatch.setattr(deploy_svc, "_assert_clean_worktree", lambda *args, **kwargs: None)

    try:
        deploy_cmd.execute("production", "main", str(config))
    except RuntimeError:
        pass

    assert compose.calls[-1] == ("restart", ("odoo",))
    assert store.saved[-1].status == "failed"
    message = store.saved[-1].message
    assert message.startswith("healthcheck failed")
    # The DB may have been mutated (module update ran), so the pre-deploy
    # backup restore is attempted; in this fixture it cannot succeed.
    assert "pre-deploy backup restore FAILED" in message
    assert "restore manually from backup production_2026" in message
    assert "recovery restart failed: container restart failed" in message


def test_deploy_missing_environment_fails_before_any_action(tmp_path: Path, monkeypatch):
    config = tmp_path / "odooctl.yml"
    config.write_text(CONFIG)
    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")

    called = []
    monkeypatch.setattr(deploy_svc, "backup_execute", lambda *args, **kwargs: called.append("backup"))
    monkeypatch.setattr(deploy_svc, "run", lambda *args, **kwargs: called.append("run"))
    monkeypatch.setattr(deploy_svc, "DockerComposeAdapter", lambda *args, **kwargs: called.append("compose"))

    try:
        deploy_cmd.execute("preview", "main", str(config))
    except KeyError as exc:
        assert "Unknown environment 'preview'" in str(exc)

    assert called == []


def test_deploy_invalid_branch_fails_preflight(tmp_path: Path, monkeypatch):
    config = tmp_path / "odooctl.yml"
    config.write_text(CONFIG.replace("/srv/filestore/staging", str(tmp_path / "srv/filestore/staging")))
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "srv/filestore/staging").mkdir(parents=True)
    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")

    called = []
    monkeypatch.setattr(deploy_svc, "backup_execute", lambda *args, **kwargs: called.append("backup"))
    monkeypatch.setattr(deploy_svc, "run", lambda *args, **kwargs: called.append("run"))
    monkeypatch.setattr(deploy_svc, "DockerComposeAdapter", lambda *args, **kwargs: called.append("compose"))
    monkeypatch.setattr(deploy_svc, "PostgresAdapter", DummyPostgres)

    try:
        deploy_cmd.execute("staging", "feature/x", str(config))
    except RuntimeError as exc:
        assert "Branch 'feature/x' is not allowed for environment 'staging'" in str(exc)

    assert called == []


def test_deploy_missing_compose_file_fails_preflight(tmp_path: Path, monkeypatch):
    config = tmp_path / "odooctl.yml"
    config.write_text(CONFIG.replace("/srv/filestore/staging", str(tmp_path / "srv/filestore/staging")))
    (tmp_path / "srv/filestore/staging").mkdir(parents=True)
    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")

    called = []
    monkeypatch.setattr(deploy_svc, "backup_execute", lambda *args, **kwargs: called.append("backup"))
    monkeypatch.setattr(deploy_svc, "run", lambda *args, **kwargs: called.append("run"))
    monkeypatch.setattr(deploy_svc, "PostgresAdapter", DummyPostgres)

    try:
        deploy_cmd.execute("staging", "staging", str(config))
    except FileNotFoundError as exc:
        assert "Compose file not found" in str(exc)

    assert called == []


def test_deploy_missing_target_paths_fails_preflight(tmp_path: Path, monkeypatch):
    config = tmp_path / "odooctl.yml"
    config.write_text(CONFIG)
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")

    called = []
    monkeypatch.setattr(deploy_svc, "backup_execute", lambda *args, **kwargs: called.append("backup"))
    monkeypatch.setattr(deploy_svc, "run", lambda *args, **kwargs: called.append("run"))
    monkeypatch.setattr(deploy_svc, "DockerComposeAdapter", lambda *args, **kwargs: called.append("compose"))
    monkeypatch.setattr(deploy_svc, "PostgresAdapter", DummyPostgres)

    try:
        deploy_cmd.execute("production", "main", str(config))
    except FileNotFoundError as exc:
        assert "Target filestore path not found" in str(exc)

    assert called == []


def test_deploy_unreachable_database_fails_before_rollout(tmp_path: Path, monkeypatch):
    config = tmp_path / "odooctl.yml"
    config.write_text(CONFIG.replace("/srv/filestore/prod", str(tmp_path / "srv/filestore/prod")))
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "srv/filestore/prod").mkdir(parents=True)
    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")

    called = []
    monkeypatch.setattr(deploy_svc, "backup_execute", lambda *args, **kwargs: called.append("backup"))
    monkeypatch.setattr(deploy_svc, "run", lambda *args, **kwargs: called.append("run"))
    monkeypatch.setattr(deploy_svc, "DockerComposeAdapter", lambda *args, **kwargs: called.append("compose"))

    class FailingPostgres(DummyPostgres):
        def ping(self, db_name: str):
            called.append("postgres")
            raise RuntimeError("connection refused")

    monkeypatch.setattr(deploy_svc, "PostgresAdapter", FailingPostgres)

    with pytest.raises(RuntimeError) as exc_info:
        deploy_cmd.execute("production", "main", str(config))

    assert "Postgres connectivity check failed for database 'odoo_prod'" in str(exc_info.value)
    assert "localhost:5432" in str(exc_info.value)
    assert "connection refused" in str(exc_info.value)

    assert called == ["postgres"]


def test_deploy_dirty_worktree_fails_before_rollout(tmp_path: Path, monkeypatch):
    config = tmp_path / "odooctl.yml"
    config.write_text(CONFIG.replace("/srv/filestore/prod", str(tmp_path / "srv/filestore/prod")))
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "srv/filestore/prod").mkdir(parents=True)
    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")

    called = []

    def fake_run(args, stream=True, cwd=None, **kwargs):
        if tuple(args) == ("git", "status", "--porcelain"):
            return SimpleNamespace(stdout=" M odooctl/main.py\n", returncode=0, stderr="", args=list(args))
        called.append("run")
        return SimpleNamespace(stdout="", returncode=0, stderr="", args=list(args))

    monkeypatch.setattr(deploy_svc, "backup_execute", lambda *args, **kwargs: called.append("backup"))
    monkeypatch.setattr(deploy_svc, "run", fake_run)
    monkeypatch.setattr(deploy_svc, "DockerComposeAdapter", lambda *args, **kwargs: called.append("compose"))
    monkeypatch.setattr(deploy_svc, "PostgresAdapter", DummyPostgres)

    with pytest.raises(RuntimeError) as exc_info:
        deploy_cmd.execute("production", "main", str(config))

    assert "Git worktree is dirty" in str(exc_info.value)
    assert "M odooctl/main.py" in str(exc_info.value)
    assert called == []


def test_deploy_missing_env_vars_fails_preflight_before_rollout(tmp_path: Path, monkeypatch):
    config = tmp_path / "odooctl.yml"
    config.write_text(CONFIG.replace("/srv/filestore/prod", str(tmp_path / "srv/filestore/prod")))
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "srv/filestore/prod").mkdir(parents=True)

    called = []
    monkeypatch.setattr(deploy_svc, "backup_execute", lambda *args, **kwargs: called.append("backup"))
    monkeypatch.setattr(deploy_svc, "run", lambda *args, **kwargs: called.append("run"))
    monkeypatch.setattr(deploy_svc, "DockerComposeAdapter", lambda *args, **kwargs: called.append("compose"))

    try:
        deploy_cmd.execute("production", "main", str(config))
    except RuntimeError as exc:
        assert "Missing required environment variables" in str(exc)

    assert called == []


def test_deploy_emits_stage_progress_messages(tmp_path: Path, monkeypatch, capsys):
    config = tmp_path / "odooctl.yml"
    config.write_text(CONFIG.replace("/srv/filestore/prod", str(tmp_path / "srv/filestore/prod")))
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "srv/filestore/prod").mkdir(parents=True)
    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")

    monkeypatch.setattr(deploy_svc, "backup_execute", lambda ctx, environment: BackupResult(backup_id="production_2026"))
    monkeypatch.setattr(deploy_svc, "git_commit", lambda cwd=None: "feedbeef")
    monkeypatch.setattr(deploy_svc, "run", lambda args, stream=True, cwd=None: None)
    monkeypatch.setattr(deploy_svc, "PostgresAdapter", DummyPostgres)
    monkeypatch.setattr(deploy_svc, "DockerComposeAdapter", lambda compose_file, **kwargs: DummyCompose(compose_file))
    monkeypatch.setattr(deploy_svc, "update_modules_compose", lambda *args, **kwargs: None)
    monkeypatch.setattr(deploy_svc, "check_url", lambda *args, **kwargs: None)
    monkeypatch.setattr(deploy_svc, "MetadataStore", lambda root: DummyStore())
    monkeypatch.setattr(deploy_svc, "_assert_clean_worktree", lambda *args, **kwargs: None)

    deploy_cmd.execute("production", "main", str(config))

    out = capsys.readouterr().out
    assert "[deploy] preflight" in out
    assert "[deploy] backup" in out
    assert "[deploy] rollout" in out
    assert "[deploy] verify" in out
    assert "[deploy] done" in out


def test_deploy_production_restores_pre_deploy_backup_when_db_mutated(tmp_path: Path, monkeypatch):
    config = tmp_path / "odooctl.yml"
    config.write_text(CONFIG.replace("/srv/filestore/prod", str(tmp_path / "srv/filestore/prod")))
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "srv/filestore/prod").mkdir(parents=True)
    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")

    store = DummyStore()
    compose = DummyCompose("docker-compose.yml")
    restores: list[tuple[str, str]] = []

    monkeypatch.setattr(deploy_svc, "backup_execute", lambda ctx, environment: BackupResult(backup_id="production_2026"))
    monkeypatch.setattr(deploy_svc, "git_commit", lambda cwd=None: "feedbeef")
    monkeypatch.setattr(deploy_svc, "run", lambda args, stream=True, cwd=None: None)
    monkeypatch.setattr(deploy_svc, "PostgresAdapter", DummyPostgres)
    monkeypatch.setattr(deploy_svc, "DockerComposeAdapter", lambda compose_file, **kwargs: compose)
    monkeypatch.setattr(deploy_svc, "update_modules_compose", lambda *args, **kwargs: None)
    monkeypatch.setattr(deploy_svc, "check_url", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("healthcheck failed")))
    monkeypatch.setattr(deploy_svc, "MetadataStore", lambda root: store)
    monkeypatch.setattr(deploy_svc, "_assert_clean_worktree", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "odooctl.services.restore.run_restore",
        lambda ctx, environment, backup="latest": restores.append((environment, backup)),
    )

    try:
        deploy_cmd.execute("production", "main", str(config))
    except RuntimeError:
        pass

    assert restores == [("production", "production_2026")]
    assert "database restored from pre-deploy backup production_2026" in store.saved[-1].message


def test_deploy_failure_before_rollout_does_not_touch_database(tmp_path: Path, monkeypatch):
    config = tmp_path / "odooctl.yml"
    config.write_text(CONFIG.replace("/srv/filestore/prod", str(tmp_path / "srv/filestore/prod")))
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "srv/filestore/prod").mkdir(parents=True)
    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")

    store = DummyStore()

    class FailingPullCompose(DummyCompose):
        def pull(self, service: str):
            raise RuntimeError("registry unreachable")

    compose = FailingPullCompose("docker-compose.yml")
    restores: list[tuple[str, str]] = []

    monkeypatch.setattr(deploy_svc, "backup_execute", lambda ctx, environment: BackupResult(backup_id="production_2026"))
    monkeypatch.setattr(deploy_svc, "git_commit", lambda cwd=None: "feedbeef")
    monkeypatch.setattr(deploy_svc, "run", lambda args, stream=True, cwd=None: None)
    monkeypatch.setattr(deploy_svc, "PostgresAdapter", DummyPostgres)
    monkeypatch.setattr(deploy_svc, "DockerComposeAdapter", lambda compose_file, **kwargs: compose)
    monkeypatch.setattr(deploy_svc, "MetadataStore", lambda root: store)
    monkeypatch.setattr(deploy_svc, "_assert_clean_worktree", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "odooctl.services.restore.run_restore",
        lambda ctx, environment, backup="latest": restores.append((environment, backup)),
    )

    try:
        deploy_cmd.execute("production", "main", str(config))
    except RuntimeError:
        pass

    # Failure before any module update: DB untouched, no restore attempted.
    assert restores == []
    assert "registry unreachable" in store.saved[-1].message


def test_required_pre_deploy_snapshot_runs_after_portable_backup(
    tmp_path: Path,
    monkeypatch,
):
    config = tmp_path / "odooctl.yml"
    config.write_text(
        CONFIG.replace(
            "/srv/filestore/prod",
            str(tmp_path / "srv/filestore/prod"),
        )
        + """
snapshots:
  provider: hetzner_cloud
  pre_deploy: required
  hetzner_cloud:
    server: odoo-prod
    recovery_server_type: cx23
    recovery_location: nbg1
    recovery_network: odoo-recovery
"""
    )
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "srv/filestore/prod").mkdir(parents=True)
    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")

    events: list[tuple[str, tuple[object, ...]]] = []
    store = DummyStore()
    compose = DummyCompose("docker-compose.yml")
    _patch_happy_deploy(monkeypatch, events, store, compose)
    monkeypatch.setattr(
        "odooctl.services.snapshots.run_snapshot_create",
        lambda ctx, environment, **kwargs: (
            events.append(
                (
                    "snapshot",
                    (environment, kwargs["portable_backup_id"]),
                )
            )
            or SimpleNamespace(
                snapshot_id="production-snapshot-1",
                status="complete",
            )
        ),
    )

    deploy_cmd.execute("production", "main", str(config))

    assert events[0] == ("backup", ("production",))
    assert events[1] == (
        "snapshot",
        ("production", "backup_2026"),
    )
    assert events[2][0] == "run"
    assert store.saved[-1].snapshot == "production-snapshot-1"
    assert store.saved[-1].snapshot_status == "complete"


def test_required_pre_deploy_snapshot_pending_stops_before_rollout(
    tmp_path: Path,
    monkeypatch,
):
    config = tmp_path / "odooctl.yml"
    config.write_text(
        CONFIG.replace(
            "/srv/filestore/prod",
            str(tmp_path / "srv/filestore/prod"),
        )
        + """
snapshots:
  provider: hetzner_cloud
  pre_deploy: required
  hetzner_cloud:
    server: odoo-prod
    recovery_server_type: cx23
    recovery_location: nbg1
    recovery_network: odoo-recovery
"""
    )
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "srv/filestore/prod").mkdir(parents=True)
    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")

    events: list[tuple[str, tuple[object, ...]]] = []
    store = DummyStore()
    compose = DummyCompose("docker-compose.yml")
    _patch_happy_deploy(monkeypatch, events, store, compose)
    monkeypatch.setattr(
        "odooctl.services.snapshots.run_snapshot_create",
        lambda *args, **kwargs: SimpleNamespace(
            snapshot_id="production-snapshot-pending",
            status="pending",
        ),
    )

    with pytest.raises(RuntimeError, match="is pending"):
        deploy_cmd.execute("production", "main", str(config))

    assert events == [("backup", ("production",))]
    assert not any(call[0] in {"pull", "up"} for call in compose.calls)
    assert not any(call[0] == "restart" for call in compose.calls)
    assert store.saved[-1].snapshot == "production-snapshot-pending"
    assert store.saved[-1].snapshot_status == "pending"


def test_required_pre_deploy_snapshot_failure_stops_before_rollout(
    tmp_path: Path,
    monkeypatch,
):
    config = tmp_path / "odooctl.yml"
    config.write_text(
        CONFIG.replace(
            "/srv/filestore/prod",
            str(tmp_path / "srv/filestore/prod"),
        )
        + """
snapshots:
  provider: hetzner_cloud
  pre_deploy: required
  hetzner_cloud:
    server: odoo-prod
    recovery_server_type: cx23
    recovery_location: nbg1
    recovery_network: odoo-recovery
"""
    )
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "srv/filestore/prod").mkdir(parents=True)
    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")

    events: list[tuple[str, tuple[object, ...]]] = []
    store = DummyStore()
    compose = DummyCompose("docker-compose.yml")
    _patch_happy_deploy(monkeypatch, events, store, compose)
    monkeypatch.setattr(
        "odooctl.services.snapshots.run_snapshot_create",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("provider unavailable")
        ),
    )

    with pytest.raises(RuntimeError, match="pre-deploy snapshot failed"):
        deploy_cmd.execute("production", "main", str(config))

    assert events == [("backup", ("production",))]
    assert not any(call[0] in {"pull", "up"} for call in compose.calls)
    assert not any(call[0] == "restart" for call in compose.calls)
    assert store.saved[-1].backup == "backup_2026"
    assert store.saved[-1].snapshot is None
    assert store.saved[-1].snapshot_status == "failed"


def test_preferred_pre_deploy_snapshot_failure_is_recorded_but_deploy_continues(
    tmp_path: Path,
    monkeypatch,
):
    config = tmp_path / "odooctl.yml"
    config.write_text(
        CONFIG.replace(
            "/srv/filestore/prod",
            str(tmp_path / "srv/filestore/prod"),
        )
        + """
snapshots:
  provider: hetzner_cloud
  pre_deploy: preferred
  hetzner_cloud:
    server: odoo-prod
    recovery_server_type: cx23
    recovery_location: nbg1
    recovery_network: odoo-recovery
"""
    )
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "srv/filestore/prod").mkdir(parents=True)
    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")
    monkeypatch.delenv("HCLOUD_TOKEN", raising=False)

    events: list[tuple[str, tuple[object, ...]]] = []
    store = DummyStore()
    compose = DummyCompose("docker-compose.yml")
    _patch_happy_deploy(monkeypatch, events, store, compose)

    deploy_cmd.execute("production", "main", str(config))

    assert store.saved[-1].status == "success"
    assert store.saved[-1].snapshot_status == "failed"
    assert "Missing snapshot provider environment variables" in (
        store.saved[-1].message
    )


def test_preferred_pre_deploy_pending_snapshot_is_recorded_and_deploy_continues(
    tmp_path: Path,
    monkeypatch,
):
    config = tmp_path / "odooctl.yml"
    config.write_text(
        CONFIG.replace(
            "/srv/filestore/prod",
            str(tmp_path / "srv/filestore/prod"),
        )
        + """
snapshots:
  provider: hetzner_cloud
  pre_deploy: preferred
  hetzner_cloud:
    server: odoo-prod
    recovery_server_type: cx23
    recovery_location: nbg1
    recovery_network: odoo-recovery
"""
    )
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "srv/filestore/prod").mkdir(parents=True)
    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")

    events: list[tuple[str, tuple[object, ...]]] = []
    store = DummyStore()
    compose = DummyCompose("docker-compose.yml")
    _patch_happy_deploy(monkeypatch, events, store, compose)
    monkeypatch.setattr(
        "odooctl.services.snapshots.run_snapshot_create",
        lambda *args, **kwargs: SimpleNamespace(
            snapshot_id="production-snapshot-pending",
            status="pending",
        ),
    )

    deploy_cmd.execute("production", "main", str(config))

    assert store.saved[-1].status == "success"
    assert store.saved[-1].snapshot == "production-snapshot-pending"
    assert store.saved[-1].snapshot_status == "pending"
    assert "reconcile it before relying on it" in store.saved[-1].message


def test_pre_deploy_snapshot_failure_preserves_manifest_correlation(
    tmp_path: Path,
    monkeypatch,
):
    config = tmp_path / "odooctl.yml"
    config.write_text(
        CONFIG.replace(
            "/srv/filestore/prod",
            str(tmp_path / "srv/filestore/prod"),
        )
        + """
snapshots:
  provider: hetzner_cloud
  pre_deploy: preferred
  hetzner_cloud:
    server: odoo-prod
    recovery_server_type: cx23
    recovery_location: nbg1
    recovery_network: odoo-recovery
"""
    )
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "srv/filestore/prod").mkdir(parents=True)
    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")

    events: list[tuple[str, tuple[object, ...]]] = []
    store = DummyStore()
    compose = DummyCompose("docker-compose.yml")
    _patch_happy_deploy(monkeypatch, events, store, compose)
    failed_manifest = SnapshotManifest(
        snapshot_id="production-snapshot-ambiguous",
        project="demo",
        environment="production",
        provider="hetzner_cloud",
        source_resource_id="odoo-prod",
        status="pending",
        last_error="provider response was lost",
    )
    failure = SnapshotCreateFailed(
        failed_manifest,
        RuntimeError("provider response was lost"),
    )
    monkeypatch.setattr(
        "odooctl.services.snapshots.run_snapshot_create",
        lambda *args, **kwargs: (_ for _ in ()).throw(failure),
    )

    deploy_cmd.execute("production", "main", str(config))

    assert store.saved[-1].status == "success"
    assert store.saved[-1].snapshot == "production-snapshot-ambiguous"
    assert store.saved[-1].snapshot_status == "pending"
    assert "provider response was lost" in store.saved[-1].message


def test_pre_deploy_snapshot_only_applies_to_bound_protected_environment(
    tmp_path: Path,
    monkeypatch,
):
    config = tmp_path / "odooctl.yml"
    staging_config = CONFIG.replace(
        "  staging:\n    branch: staging",
        "  staging:\n    branch: staging\n    protected: true",
    ).replace(
        "/srv/filestore/staging",
        str(tmp_path / "srv/filestore/staging"),
    )
    config.write_text(
        staging_config
        + """
snapshots:
  provider: hetzner_cloud
  environment: production
  pre_deploy: required
  hetzner_cloud:
    server: odoo-prod
    recovery_server_type: cx23
    recovery_location: nbg1
    recovery_network: odoo-recovery
"""
    )
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "srv/filestore/staging").mkdir(parents=True)
    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")

    events: list[tuple[str, tuple[object, ...]]] = []
    store = DummyStore()
    compose = DummyCompose("docker-compose.yml")
    _patch_happy_deploy(monkeypatch, events, store, compose)

    deploy_cmd.execute("staging", "staging", str(config))

    assert events[0] == ("backup", ("staging",))
    assert not any(event[0] == "snapshot" for event in events)
    assert store.saved[-1].status == "success"
    assert store.saved[-1].snapshot is None
