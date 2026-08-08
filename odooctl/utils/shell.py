from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

SENSITIVE_MARKERS = ("PASSWORD", "SECRET", "TOKEN", "KEY", "PASSWD")
DEFAULT_REDACTION_MIN_SECRET_LENGTH = 6
DEFAULT_REDACTION_IGNORE_VALUES = frozenset({"odoo", "admin", "postgres", "password", "secret", "changeme"})

@dataclass(slots=True)
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

class CommandError(RuntimeError):
    def __init__(self, result: CommandResult, env: Mapping[str, str] | None = None):
        # Redact with the merged per-call environment (codex re-scan #4): a
        # secret supplied via the call's ``env=`` (e.g. PGPASSWORD) may not be
        # present in ``os.environ``, so ``redact(env=os.environ)`` alone would
        # miss it. ``result.args``/``result.stderr`` can echo such a value.
        message = f"Command failed ({result.returncode}): {' '.join(result.args)}\n{result.stderr}"
        super().__init__(redact(message, env))
        self.result = result

def redact(
    text: str,
    env: Mapping[str, str] | None = None,
    *,
    min_secret_length: int = DEFAULT_REDACTION_MIN_SECRET_LENGTH,
    ignore_values: Iterable[str] = DEFAULT_REDACTION_IGNORE_VALUES,
) -> str:
    env = env or os.environ
    ignored = {value for value in ignore_values if value}
    redacted = text
    for key, value in env.items():
        if not value or not any(marker in key.upper() for marker in SENSITIVE_MARKERS):
            continue
        if len(value) < min_secret_length or value in ignored:
            continue
        redacted = redacted.replace(value, "***REDACTED***")
    return redacted

def run(
    args: Sequence[str],
    *,
    check: bool = True,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    unset_env: Iterable[str] = (),
    stream: bool = False,
) -> CommandResult:
    merged_env = _merged_env(env, unset_env=unset_env)
    if stream:
        proc = subprocess.Popen(list(args), cwd=cwd, env=merged_env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output = []
        assert proc.stdout is not None
        for line in proc.stdout:
            clean = redact(line, merged_env)
            print(clean, end="")
            output.append(clean)
        rc = proc.wait()
        result = CommandResult(list(args), rc, "".join(output), "")
    else:
        proc = subprocess.run(list(args), cwd=cwd, env=merged_env, text=True, capture_output=True)
        result = CommandResult(list(args), proc.returncode, redact(proc.stdout, merged_env), redact(proc.stderr, merged_env))
    if check and result.returncode != 0:
        raise CommandError(result, merged_env)
    return result

def _merged_env(
    env: Mapping[str, str] | None = None,
    *,
    unset_env: Iterable[str] = (),
) -> dict[str, str]:
    merged_env = os.environ.copy()
    for name in unset_env:
        merged_env.pop(name, None)
    if env:
        merged_env.update(env)
    return merged_env


def run_capture_bytes(
    args: Sequence[str],
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    stdout_path: str | Path,
    check: bool = True,
) -> CommandResult:
    """Run a command and write raw stdout bytes to a file.

    This is binary-safe for PostgreSQL custom-format dumps: stdout never passes
    through Python text decoding or redaction.
    """
    merged_env = _merged_env(env)
    output_path = Path(stdout_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as stdout:
        proc = subprocess.run(list(args), cwd=cwd, env=merged_env, stdout=stdout, stderr=subprocess.PIPE)
    stderr = redact(proc.stderr.decode(errors="replace"), merged_env)
    result = CommandResult(list(args), proc.returncode, str(output_path), stderr)
    if check and result.returncode != 0:
        raise CommandError(result, merged_env)
    return result


def run_pipe_stdin(
    args: Sequence[str],
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    stdin_path: str | Path,
    check: bool = True,
) -> CommandResult:
    """Run a command with raw bytes from a file connected to stdin."""
    merged_env = _merged_env(env)
    with Path(stdin_path).open("rb") as stdin:
        proc = subprocess.run(list(args), cwd=cwd, env=merged_env, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout = redact(proc.stdout.decode(errors="replace"), merged_env)
    stderr = redact(proc.stderr.decode(errors="replace"), merged_env)
    result = CommandResult(list(args), proc.returncode, stdout, stderr)
    if check and result.returncode != 0:
        raise CommandError(result, merged_env)
    return result


def run_pipe(
    producer: Sequence[str],
    consumer: Sequence[str],
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
) -> CommandResult:
    """Pipe producer stdout into consumer stdin without invoking a shell.

    Replaces ``sh -c "producer | consumer"`` so that no operand is ever
    shell-interpreted. The byte stream never passes through Python text
    decoding, keeping it safe for PostgreSQL custom-format dumps.
    """
    merged_env = _merged_env(env)
    producer_proc = subprocess.Popen(
        list(producer), cwd=cwd, env=merged_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    assert producer_proc.stdout is not None
    consumer_proc = subprocess.Popen(
        list(consumer),
        cwd=cwd,
        env=merged_env,
        stdin=producer_proc.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Close our handle so the producer receives SIGPIPE if the consumer exits.
    producer_proc.stdout.close()
    consumer_stdout, consumer_stderr = consumer_proc.communicate()
    producer_stderr = producer_proc.stderr.read() if producer_proc.stderr else b""
    producer_rc = producer_proc.wait()
    display_args = [*producer, "|", *consumer]
    stderr = redact(
        (producer_stderr + consumer_stderr).decode(errors="replace"), merged_env
    )
    stdout = redact(consumer_stdout.decode(errors="replace"), merged_env)
    returncode = producer_rc if producer_rc != 0 else consumer_proc.returncode
    result = CommandResult(list(display_args), returncode, stdout, stderr)
    if check and returncode != 0:
        raise CommandError(result, merged_env)
    return result


def join_csv(values: Iterable[str]) -> str:
    return ",".join(v.strip() for v in values if v and v.strip())
