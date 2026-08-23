"""Persistent user accounts with hashed passwords.

Users are a *server-level* concept (like the project registry), not a
per-project one: one odooctl host has one set of accounts that spans every
registered project. The store lives next to the registry
(``~/.config/odooctl/users.json`` by default) as a locked, atomically-written
JSON file with owner-only permissions.

Password hashing is scheme-prefixed (``scrypt$...``) so the algorithm can be
upgraded (e.g. to argon2id) without invalidating stored hashes: verification
dispatches on the prefix, and callers may rehash on successful login when the
scheme is outdated. The default is stdlib ``hashlib.scrypt`` (N=2^14, r=8,
p=1) — no third-party crypto dependency, same policy as ``tokens``.

Auth-provider pluggability: :meth:`UserStore.authenticate` is the *password*
provider. Records carry ``provider``/``provider_subject`` fields so an OIDC
provider (post-1.0) can attach external identities to the same accounts
without a schema migration.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from odooctl.security.principals import Principal, PrincipalKind, Role

#: Filename of the user store inside the auth directory.
USERS_FILENAME = "users.json"

#: Minimum accepted password length. A floor, not a policy engine.
MIN_PASSWORD_LENGTH = 8

# scrypt cost parameters (interactive-login grade): 16 MiB memory, ~50 ms.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UserError(Exception):
    """Base class for user-store failures."""


class UserExists(UserError):
    """An account with this email already exists."""


class UserNotFound(UserError):
    """No account matches the given id or email."""


def normalize_email(email: str) -> str:
    """Lowercase/trim an email; raise ``ValueError`` if it is not plausible."""
    email = email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise ValueError(f"Not a valid email address: {email!r}")
    return email


def validate_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    return password


def hash_password(password: str) -> str:
    """Hash *password* into the scheme-prefixed storage format."""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(stored: str, password: str) -> bool:
    """Check *password* against a stored hash; unknown schemes verify false."""
    try:
        scheme, rest = stored.split("$", 1)
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    try:
        n_s, r_s, p_s, salt_hex, dk_hex = rest.split("$")
        expected = bytes.fromhex(dk_hex)
        dk = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n_s),
            r=int(r_s),
            p=int(p_s),
            dklen=len(expected),
            maxmem=_SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


# A throwaway hash verified for unknown emails so authenticate() costs the
# same whether or not the account exists (login-oracle hardening).
_DUMMY_HASH = hash_password(secrets.token_hex(16))


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class UserRecord:
    """A stored account. ``password_hash`` never leaves ``to_dict``."""

    id: str
    email: str
    name: str = ""
    org_id: str = "default"
    roles: tuple[str, ...] = field(default_factory=tuple)
    password_hash: str = ""
    provider: str = "password"
    provider_subject: str = ""
    disabled: bool = False
    created_at: str = ""
    password_updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "org_id": self.org_id,
            "roles": list(self.roles),
            "password_hash": self.password_hash,
            "provider": self.provider,
            "provider_subject": self.provider_subject,
            "disabled": self.disabled,
            "created_at": self.created_at,
            "password_updated_at": self.password_updated_at,
        }

    def to_public_dict(self) -> dict:
        """Value-free view for API responses and CLI listings."""
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "org_id": self.org_id,
            "roles": list(self.roles),
            "provider": self.provider,
            "disabled": self.disabled,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "UserRecord":
        return cls(
            id=str(d["id"]),
            email=str(d["email"]),
            name=str(d.get("name", "")),
            org_id=str(d.get("org_id", "default")),
            roles=tuple(str(r) for r in d.get("roles", [])),
            password_hash=str(d.get("password_hash", "")),
            provider=str(d.get("provider", "password")),
            provider_subject=str(d.get("provider_subject", "")),
            disabled=bool(d.get("disabled", False)),
            created_at=str(d.get("created_at", "")),
            password_updated_at=str(d.get("password_updated_at", "")),
        )

    def role_set(self) -> frozenset[Role]:
        roles: set[Role] = set()
        for raw in self.roles:
            try:
                roles.add(Role(raw))
            except ValueError:
                continue
        return frozenset(roles)

    def max_role(self) -> Role | None:
        from odooctl.security.principals import role_rank

        roles = self.role_set()
        return max(roles, key=role_rank) if roles else None

    def to_principal(self) -> Principal:
        """The identity object RBAC and audit reason about for this account."""
        return Principal(
            id=self.email,
            org_id=self.org_id,
            kind=PrincipalKind.USER,
            roles=self.role_set(),
            display=self.name or self.email,
        )


class UserStore:
    """Locked, atomically-written JSON store of :class:`UserRecord` accounts."""

    def __init__(self, auth_dir: Path) -> None:
        self.auth_dir = Path(auth_dir)
        self.path = self.auth_dir / USERS_FILENAME
        self._lock_path = self.auth_dir / "users.lock"

    # ------------------------------------------------------------------ io
    def _load(self) -> list[UserRecord]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []
        users = []
        for raw in data.get("users", []):
            try:
                users.append(UserRecord.from_dict(raw))
            except (KeyError, TypeError, ValueError):
                continue
        return users

    def _write(self, users: list[UserRecord]) -> None:
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"users": [u.to_dict() for u in users]}, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(self.auth_dir), prefix=".users-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def _locked(self):
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        lock_file = self._lock_path.open("w")
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        return lock_file

    # ---------------------------------------------------------------- reads
    def list_users(self) -> list[UserRecord]:
        return sorted(self._load(), key=lambda u: u.email)

    def get(self, user_id: str) -> UserRecord:
        for user in self._load():
            if user.id == user_id:
                return user
        raise UserNotFound(f"No user with id {user_id!r}")

    def get_by_email(self, email: str) -> UserRecord:
        email = email.strip().lower()
        for user in self._load():
            if user.email == email:
                return user
        raise UserNotFound(f"No user with email {email!r}")

    # --------------------------------------------------------------- writes
    def create(
        self,
        email: str,
        password: str,
        *,
        roles: list[Role] | list[str] | tuple = (),
        name: str = "",
        org_id: str = "default",
    ) -> UserRecord:
        email = normalize_email(email)
        validate_password(password)
        role_values = tuple(Role(r).value for r in roles)
        with self._locked():
            users = self._load()
            if any(u.email == email for u in users):
                raise UserExists(f"A user with email {email!r} already exists")
            record = UserRecord(
                id=f"u-{secrets.token_hex(8)}",
                email=email,
                name=name,
                org_id=org_id,
                roles=role_values,
                password_hash=hash_password(password),
                created_at=_utcnow(),
                password_updated_at=_utcnow(),
            )
            users.append(record)
            self._write(users)
        return record

    def _mutate(self, user_id: str, **changes) -> UserRecord:
        with self._locked():
            users = self._load()
            for i, user in enumerate(users):
                if user.id == user_id:
                    updated = replace(user, **changes)
                    users[i] = updated
                    self._write(users)
                    return updated
        raise UserNotFound(f"No user with id {user_id!r}")

    def set_password(self, user_id: str, password: str) -> UserRecord:
        validate_password(password)
        return self._mutate(
            user_id,
            password_hash=hash_password(password),
            password_updated_at=_utcnow(),
        )

    def set_roles(self, user_id: str, roles: list[Role] | list[str] | tuple) -> UserRecord:
        return self._mutate(user_id, roles=tuple(Role(r).value for r in roles))

    def set_disabled(self, user_id: str, disabled: bool) -> UserRecord:
        return self._mutate(user_id, disabled=bool(disabled))

    def set_name(self, user_id: str, name: str) -> UserRecord:
        return self._mutate(user_id, name=str(name))

    def delete(self, user_id: str) -> None:
        with self._locked():
            users = self._load()
            remaining = [u for u in users if u.id != user_id]
            if len(remaining) == len(users):
                raise UserNotFound(f"No user with id {user_id!r}")
            self._write(remaining)

    # ----------------------------------------------------------------- auth
    def authenticate(self, email: str, password: str) -> UserRecord | None:
        """Password login: return the account, or ``None`` on any failure.

        Unknown email, disabled account, and wrong password are deliberately
        indistinguishable to the caller, and the unknown-email path still runs
        one scrypt verification so timing does not disclose which emails exist.
        """
        try:
            user = self.get_by_email(email)
        except (UserNotFound, ValueError):
            verify_password(_DUMMY_HASH, password)
            return None
        if not user.password_hash or not verify_password(user.password_hash, password):
            return None
        if user.disabled:
            return None
        return user
