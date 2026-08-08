from odooctl.odoo import module_update

def test_update_modules_noops_without_modules(monkeypatch):
    called = False
    def fake_run(*args, **kwargs):
        nonlocal called
        called = True
    monkeypatch.setattr(module_update, "run", fake_run)
    module_update.update_modules_local("db", [])
    assert called is False

def test_update_modules_invokes_odoo_stop_after_init(monkeypatch):
    seen = {}
    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
    monkeypatch.setattr(module_update, "run", fake_run)
    module_update.update_modules_local("db", ["sale", "stock"])
    assert seen["args"] == ["odoo", "-d", "db", "-u", "sale,stock", "--stop-after-init"]
    assert seen["kwargs"]["stream"] is True


def test_update_modules_uses_configured_odoo_cli(monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args

    monkeypatch.setattr(module_update, "run", fake_run)
    module_update.update_modules_local(
        "db",
        ["sale"],
        cli_command="/opt/odoo/odoo-bin",
    )

    assert seen["args"][0] == "/opt/odoo/odoo-bin"


def test_update_modules_compose_passes_odoo_db_flags(monkeypatch):
    monkeypatch.setenv("ODOO_DB_PASSWORD", "secret")
    seen = {}

    class Compose:
        def exec(self, service, args, **kwargs):
            seen["service"] = service
            seen["args"] = args
            seen["kwargs"] = kwargs

    module_update.update_modules_compose(
        Compose(),
        "odoo",
        "odoo_prod",
        ["base"],
        db_host="db",
        db_user="odoo",
        db_password_env="ODOO_DB_PASSWORD",
        config_path="/etc/odoo/odoo.conf",
        cli_command="/opt/odoo/odoo-bin",
    )

    assert seen["service"] == "odoo"
    assert seen["args"] == [
        "/opt/odoo/odoo-bin",
        "-d",
        "odoo_prod",
        "-u",
        "base",
        "--stop-after-init",
        "-c",
        "/etc/odoo/odoo.conf",
        "--db_host",
        "db",
        "--db_user",
        "odoo",
    ]
    assert seen["kwargs"]["stream"] is True
    assert seen["kwargs"]["extra_env"] == {"PGPASSWORD": "secret"}


def test_update_modules_compose_requires_configured_password_env(monkeypatch):
    monkeypatch.delenv("ODOO_DB_PASSWORD", raising=False)

    class Compose:
        def exec(self, *args, **kwargs):  # pragma: no cover - should not be called
            raise AssertionError("exec should not be called")

    try:
        module_update.update_modules_compose(
            Compose(),
            "odoo",
            "odoo_prod",
            ["base"],
            db_host="db",
            db_user="odoo",
            db_password_env="ODOO_DB_PASSWORD",
        )
    except RuntimeError as exc:
        assert "ODOO_DB_PASSWORD" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected RuntimeError")


def test_update_modules_compose_password_never_on_argv(monkeypatch):
    secret = "compose-secret-value-1"
    monkeypatch.setenv("ODOO_DB_PASSWORD", secret)
    seen = {}

    class Compose:
        def exec(self, service, args, **kwargs):
            seen["args"] = args
            seen["kwargs"] = kwargs

    module_update.update_modules_compose(
        Compose(),
        "odoo",
        "odoo_prod",
        ["base"],
        db_host="db",
        db_user="odoo",
        db_password_env="ODOO_DB_PASSWORD",
    )

    assert all(secret not in arg for arg in seen["args"])
    assert "--db_password" not in seen["args"]
    assert seen["kwargs"]["extra_env"] == {"PGPASSWORD": secret}


def test_build_update_modules_args_never_contains_password():
    args = module_update.build_update_modules_args(
        "db", ["base"], db_host="db", db_user="odoo", config_path="/etc/odoo/odoo.conf"
    )
    assert "--db_password" not in args


def test_update_modules_local_password_via_env_not_argv(monkeypatch):
    secret = "local-secret-value-2"
    monkeypatch.setenv("ODOO_DB_PASSWORD", secret)
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs

    monkeypatch.setattr(module_update, "run", fake_run)
    module_update.update_modules_local(
        "db", ["sale"], db_host="db", db_user="odoo", db_password_env="ODOO_DB_PASSWORD"
    )

    assert all(secret not in arg for arg in seen["args"])
    assert "--db_password" not in seen["args"]
    assert seen["kwargs"]["env"] == {"PGPASSWORD": secret}
    assert seen["kwargs"]["stream"] is True


def test_update_modules_local_requires_configured_password_env(monkeypatch):
    monkeypatch.delenv("ODOO_DB_PASSWORD", raising=False)

    def fake_run(*args, **kwargs):  # pragma: no cover - should not be called
        raise AssertionError("run should not be called")

    monkeypatch.setattr(module_update, "run", fake_run)
    try:
        module_update.update_modules_local("db", ["sale"], db_password_env="ODOO_DB_PASSWORD")
    except RuntimeError as exc:
        assert "ODOO_DB_PASSWORD" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected RuntimeError")


def test_compose_exec_injects_env_flag_names_only(monkeypatch):
    """DockerComposeAdapter.exec passes -e NAME flags; values go via process env."""
    from odooctl.adapters import docker_compose as dc

    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs

    monkeypatch.setattr(dc, "run", fake_run)
    adapter = dc.DockerComposeAdapter("docker-compose.yml")
    adapter.exec("odoo", ["odoo", "--stop-after-init"], extra_env={"PGPASSWORD": "sekret-value"})

    assert seen["args"] == [
        "docker", "compose", "-f", "docker-compose.yml",
        "exec", "-T", "-e", "PGPASSWORD", "odoo", "odoo", "--stop-after-init",
    ]
    assert all("sekret-value" not in arg for arg in seen["args"])
    assert seen["kwargs"]["env"] == {"PGPASSWORD": "sekret-value"}
