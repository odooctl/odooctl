"""Shared pytest configuration for odooctl tests.

The test suite should not depend on secrets exported in the developer or CI
environment. Individual tests that need a secret can opt in by setting it with
``monkeypatch.setenv`` inside the test body.
"""

from __future__ import annotations

import os
import re

import pytest
from pytest import ExitCode


_ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """Return ``text`` with SGR colour escapes removed.

    Typer sets ``FORCE_TERMINAL`` whenever ``GITHUB_ACTIONS`` is present, so
    Rich renders CLI error panels in colour on Actions but in plain text on a
    developer machine or in a bare container. Rich's highlighter then *splits*
    option tokens while styling them — ``--yes`` is emitted as ``-``, a reset,
    then ``-yes`` — so the literal substring ``"--yes"`` does not appear in the
    coloured bytes at all.

    Assertions on CLI output must therefore strip first, or they pass
    everywhere except CI. Setting ``NO_COLOR`` or ``TERM=dumb`` does not help:
    Typer resolves its colour settings at import time and force-terminal wins.
    """

    return _ANSI_SGR.sub("", text)


# Environment variable names used by example configs and remote-backup tests.
# Keep this intentionally broader than the current suite so new tests do not
# accidentally inherit real operator credentials from the ambient environment.
ISOLATED_ENV_VARS = (
    "ODOO_DB_PASSWORD",
    "S3_ENDPOINT",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "ODOOCTL_API_KEY",
    "ODOOCTL_RUNNER_KEY",
)
ORIGINAL_ENV = {name: os.environ[name] for name in ISOLATED_ENV_VARS if name in os.environ}


@pytest.fixture(autouse=True)
def isolate_operator_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove operator secrets from every test unless the test opts in.

    Tests can still call ``monkeypatch.setenv`` after this fixture has run.
    This prevents an exported ODOO_DB_PASSWORD from bypassing missing-env
    assertions and reaching real host adapters such as psql/pg_dump.
    """

    for name in ISOLATED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def isolate_global_registry(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Point the global project registry at a per-test empty location.

    The registry default is ``$XDG_CONFIG_HOME/odooctl/config.toml`` (falling
    back to ``~/.config``). Without this fixture the suite's outcome depends on
    whatever projects the operator has registered on the machine running the
    tests — the suspected cause of historical environment-dependent failures.
    Tests that need a populated registry write into the isolated location.
    """

    monkeypatch.setenv(
        "XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg-config"))
    )


@pytest.fixture
def preserved_env(monkeypatch: pytest.MonkeyPatch):
    """Temporarily restore selected real environment variables for opt-in tests.

    Usage: ``preserved_env("ODOO_DB_PASSWORD")``. This is intentionally explicit
    and currently unused by unit tests, but it gives future integration tests a
    clean escape hatch without disabling the autouse isolation globally.
    """

    def restore(*names: str) -> None:
        for name in names:
            if name in ORIGINAL_ENV:
                monkeypatch.setenv(name, ORIGINAL_ENV[name])

    return restore


def pytest_sessionfinish(session: pytest.Session, exitstatus: int | ExitCode) -> None:
    """Treat an empty integration suite as a clean collection result.

    M0 registers the integration marker before any integration tests exist.
    ``pytest -m integration`` should therefore be usable in CI/sprints as a
    smoke collection command without failing solely because no integration tests
    have landed yet.
    """

    if session.config.option.markexpr == "integration" and exitstatus == ExitCode.NO_TESTS_COLLECTED:
        session.exitstatus = ExitCode.OK
