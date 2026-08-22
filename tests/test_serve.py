"""Tests for the deliberately narrow remote-UI host opt-in."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from odooctl.commands.serve import validate_allowed_hosts
from odooctl.main import app


runner = CliRunner()


def test_allowed_hosts_accept_exact_hostnames_and_ips() -> None:
    assert validate_allowed_hosts(["ui.example.com", "100.88.1.2"]) == [
        "ui.example.com",
        "100.88.1.2",
    ]


@pytest.mark.parametrize("host", ["*", "*.example.com", "ui..example.com", "https://ui.example.com"])
def test_allowed_hosts_reject_wildcards_and_urls(host: str) -> None:
    with pytest.raises(ValueError, match="exact hostname or IP address"):
        validate_allowed_hosts([host])


def test_serve_passes_explicit_allowed_hosts(monkeypatch) -> None:
    recorded: dict[str, object] = {}
    monkeypatch.setattr("odooctl.main.serve_cmd.run", lambda **kwargs: recorded.update(kwargs))

    result = runner.invoke(
        app,
        [
            "serve",
            "--api-key",
            "test-api-key-123456789012345678901234567890",
            "--allowed-host",
            "ui.example.test",
            "--allowed-host",
            "100.88.1.2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert recorded["allowed_hosts"] == ["ui.example.test", "100.88.1.2"]
