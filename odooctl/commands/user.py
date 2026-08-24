"""odooctl user — manage server-level user accounts.

Accounts live next to the project registry (``~/.config/odooctl/users.json``)
and span every registered project; the API (``odooctl serve``) authenticates
browser logins against them. The CLI acts as the local-admin principal — a
shell on the server outranks any API role, so these commands take no login.

Secret-safety: passwords are never accepted as command-line arguments (shell
history / ``ps`` leak). They arrive via ``--password-env`` or ``--stdin``, or
an interactive hidden prompt when neither is given on a TTY.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from odooctl.security.principals import Role
from odooctl.security.sessions import SessionStore
from odooctl.security.users import UserError, UserStore

app = typer.Typer(help="Manage user accounts for the odooctl API/UI.", add_completion=False)
console = Console()


def _auth_dir(auth_dir: Path | None) -> Path:
    if auth_dir is not None:
        return auth_dir
    from odooctl.registry import default_registry_path

    return default_registry_path().parent


def _read_password(password_env: str | None, stdin: bool, *, confirm: bool) -> str:
    if password_env is not None:
        if password_env not in os.environ:
            typer.echo(f"Environment variable {password_env} is not set", err=True)
            raise typer.Exit(1)
        return os.environ[password_env]
    if stdin:
        return sys.stdin.readline().rstrip("\n")
    if sys.stdin.isatty():
        password = typer.prompt("Password", hide_input=True)
        if confirm:
            again = typer.prompt("Repeat password", hide_input=True)
            if password != again:
                typer.echo("Passwords do not match", err=True)
                raise typer.Exit(1)
        return password
    typer.echo("Provide the password via --password-env NAME or --stdin", err=True)
    raise typer.Exit(1)


def _parse_roles(roles: list[str]) -> list[Role]:
    parsed: list[Role] = []
    for raw in roles:
        try:
            parsed.append(Role(raw))
        except ValueError:
            typer.echo(
                f"Unknown role {raw!r}; valid roles: {', '.join(r.value for r in Role)}",
                err=True,
            )
            raise typer.Exit(1)
    return parsed


def _get_by_email(store: UserStore, email: str):
    try:
        return store.get_by_email(email)
    except UserError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)


@app.command("add")
def add(
    email: str = typer.Argument(..., help="Login email for the new account."),
    role: list[str] = typer.Option(["viewer"], "--role", help="Role to grant (repeatable)."),
    name: str = typer.Option("", "--name", help="Display name."),
    password_env: str | None = typer.Option(None, "--password-env", help="Read the password from this env var."),
    stdin: bool = typer.Option(False, "--stdin", help="Read the password from stdin."),
    auth_dir: Path | None = typer.Option(None, "--auth-dir", hidden=True, help="Store directory override (testing)."),
) -> None:
    """Create a user account (e.g. the first admin after install)."""
    roles = _parse_roles(role)
    password = _read_password(password_env, stdin, confirm=True)
    store = UserStore(_auth_dir(auth_dir))
    try:
        user = store.create(email, password, roles=roles, name=name)
    except (UserError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    typer.echo(
        f"Created user {user.email} ({', '.join(user.roles) or 'no roles'}). "
        "They can log in at the odooctl web UI."
    )


@app.command("list")
def list_users(
    json_output: bool = typer.Option(False, "--json", "--json-output"),
    auth_dir: Path | None = typer.Option(None, "--auth-dir", hidden=True),
) -> None:
    """List accounts (never shows password hashes)."""
    users = UserStore(_auth_dir(auth_dir)).list_users()
    if json_output:
        typer.echo(json.dumps([u.to_public_dict() for u in users], indent=2))
        return
    table = Table(title="odooctl users")
    table.add_column("Email", style="bold")
    table.add_column("Name")
    table.add_column("Roles")
    table.add_column("Status")
    table.add_column("Created")
    for u in users:
        table.add_row(
            u.email,
            u.name or "-",
            ", ".join(u.roles) or "-",
            "disabled" if u.disabled else "active",
            u.created_at or "-",
        )
    console.print(table)


@app.command("role")
def set_roles(
    email: str = typer.Argument(...),
    role: list[str] = typer.Option(..., "--role", help="Role to grant (repeatable); replaces current roles."),
    auth_dir: Path | None = typer.Option(None, "--auth-dir", hidden=True),
) -> None:
    """Replace an account's roles."""
    roles = _parse_roles(role)
    store = UserStore(_auth_dir(auth_dir))
    user = _get_by_email(store, email)
    user = store.set_roles(user.id, roles)
    typer.echo(f"Roles for {user.email}: {', '.join(user.roles) or 'none'}")


@app.command("passwd")
def passwd(
    email: str = typer.Argument(...),
    password_env: str | None = typer.Option(None, "--password-env"),
    stdin: bool = typer.Option(False, "--stdin"),
    auth_dir: Path | None = typer.Option(None, "--auth-dir", hidden=True),
) -> None:
    """Reset an account's password and revoke its active sessions."""
    directory = _auth_dir(auth_dir)
    store = UserStore(directory)
    user = _get_by_email(store, email)
    password = _read_password(password_env, stdin, confirm=True)
    try:
        store.set_password(user.id, password)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    revoked = SessionStore(directory).revoke_user(user.id)
    typer.echo(f"Password updated for {user.email} ({revoked} session(s) revoked).")


@app.command("disable")
def disable(
    email: str = typer.Argument(...),
    auth_dir: Path | None = typer.Option(None, "--auth-dir", hidden=True),
) -> None:
    """Disable an account and revoke its active sessions."""
    directory = _auth_dir(auth_dir)
    store = UserStore(directory)
    user = _get_by_email(store, email)
    store.set_disabled(user.id, True)
    revoked = SessionStore(directory).revoke_user(user.id)
    typer.echo(f"Disabled {user.email} ({revoked} session(s) revoked).")


@app.command("enable")
def enable(
    email: str = typer.Argument(...),
    auth_dir: Path | None = typer.Option(None, "--auth-dir", hidden=True),
) -> None:
    """Re-enable a disabled account."""
    store = UserStore(_auth_dir(auth_dir))
    user = _get_by_email(store, email)
    store.set_disabled(user.id, False)
    typer.echo(f"Enabled {user.email}.")


@app.command("remove")
def remove(
    email: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    auth_dir: Path | None = typer.Option(None, "--auth-dir", hidden=True),
) -> None:
    """Delete an account and revoke its active sessions."""
    directory = _auth_dir(auth_dir)
    store = UserStore(directory)
    user = _get_by_email(store, email)
    if not yes:
        typer.confirm(f"Delete user {user.email}?", abort=True)
    store.delete(user.id)
    SessionStore(directory).revoke_user(user.id)
    typer.echo(f"Removed {user.email}.")
