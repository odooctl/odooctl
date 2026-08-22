"""Ensure every fenced ``odooctl`` command starts with a real CLI command path.

Arguments are intentionally not executed: this is a documentation parser, not
an operator-action test. Each discovered command path is checked with
``--help`` from the matching source checkout.
"""
from __future__ import annotations

import argparse
import re
import shlex
import sys
from pathlib import Path


COMMAND_LINE = re.compile(r"^\s*(?:\$\s*)?(odooctl(?:\s+.*)?)$")
GLOBAL_OPTIONS = {"--project", "-p", "--project-dir", "-C"}


def documented_commands(docs_root: Path) -> list[tuple[Path, int, list[str]]]:
    commands: list[tuple[Path, int, list[str]]] = []
    for path in sorted(docs_root.rglob("*.md")):
        in_fence = False
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if line.strip().startswith("```"):
                in_fence = not in_fence
                continue
            if not in_fence:
                continue
            match = COMMAND_LINE.match(line.rstrip("\\\n"))
            if match:
                commands.append((path, number, shlex.split(match.group(1))))
    return commands


def _command_path(tokens: list[str], root) -> list[str]:
    remaining = tokens[1:]
    while remaining and remaining[0] in GLOBAL_OPTIONS:
        if len(remaining) < 2:
            return []
        remaining = remaining[2:]
    command = root
    path: list[str] = []
    while remaining and hasattr(command, "commands") and remaining[0] in command.commands:
        name = remaining.pop(0)
        path.append(name)
        command = command.commands[name]
    return path


def verify(docs_root: Path, package_root: Path) -> list[str]:
    sys.path.insert(0, str(package_root))
    from typer.main import get_command
    from typer.testing import CliRunner

    from odooctl.main import app

    root = get_command(app)
    runner = CliRunner()
    errors: list[str] = []
    for path, line, tokens in documented_commands(docs_root):
        if tokens in (["odooctl", "--version"], ["odooctl", "--help"]):
            result = runner.invoke(app, tokens[1:])
            if result.exit_code != 0:
                errors.append(f"{path}:{line}: root option failed: {result.output}")
            continue
        command_path = _command_path(tokens, root)
        if not command_path:
            errors.append(f"{path}:{line}: could not parse command path: {' '.join(tokens)}")
            continue
        result = runner.invoke(app, [*command_path, "--help"])
        if result.exit_code != 0:
            errors.append(f"{path}:{line}: help failed for {' '.join(command_path)}: {result.output}")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs-root", type=Path, required=True)
    parser.add_argument("--package-root", type=Path, required=True)
    args = parser.parse_args()
    errors = verify(args.docs_root.resolve(), args.package_root.resolve())
    if errors:
        print("\n".join(errors), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
