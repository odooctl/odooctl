"""The current documentation only names command paths exposed by the CLI."""
from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("documented_commands", ROOT / "scripts/check_documented_commands.py")
assert SPEC and SPEC.loader
documented_commands = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(documented_commands)


def test_documented_commands_match_current_cli_help() -> None:
    assert documented_commands.verify(ROOT / "docs", ROOT) == []
