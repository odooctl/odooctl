"""Revocable browser sessions for the SPA.

A session id is a 256-bit random secret handed to the browser as an
``HttpOnly`` cookie. The server stores only its SHA-256 digest, so a leaked
``sessions.json`` does not yield usable credentials. Sessions are the
*revocable* credential (logout, user disable, password change); stateless
HMAC bearer tokens (``security.tokens``) remain the credential for CLI/CI.

Roles are deliberately NOT stored on the session: the API resolves the user
record on every request, so role changes and account disabling take effect
immediately instead of at next login.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

#: Filename of the session store inside the auth directory.
SESSIONS_FILENAME = "sessions.json"

#: Default browser-session lifetime: 12 hours.
DEFAULT_SESSION_TTL_SECONDS = 12 * 3600

#: Cookie name the API sets and reads.
SESSION_COOKIE = "odooctl_session"


def _digest(sid: str) -> str:
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SessionRecord:
    sid_hash: str
    user_id: str
    created_at: int
    expires_at: int

    def to_dict(self) -> dict:
        return {
            "sid_hash": self.sid_hash,
            "user_id": self.user_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SessionRecord":
        return cls(
            sid_hash=str(d["sid_hash"]),
            user_id=str(d["user_id"]),
            created_at=int(d.get("created_at", 0)),
            expires_at=int(d.get("expires_at", 0)),
        )


class SessionStore:
    """Locked, atomically-written JSON store of active sessions."""

    def __init__(self, auth_dir: Path) -> None:
        self.auth_dir = Path(auth_dir)
        self.path = self.auth_dir / SESSIONS_FILENAME
        self._lock_path = self.auth_dir / "sessions.lock"

    def _load(self) -> list[SessionRecord]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []
        records = []
        for raw in data.get("sessions", []):
            try:
                records.append(SessionRecord.from_dict(raw))
            except (KeyError, TypeError, ValueError):
                continue
        return records

    def _write(self, records: list[SessionRecord]) -> None:
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"sessions": [r.to_dict() for r in records]})
        fd, tmp = tempfile.mkstemp(dir=str(self.auth_dir), prefix=".sessions-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _locked(self):
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        lock_file = self._lock_path.open("w")
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        return lock_file

    def create(
        self,
        user_id: str,
        *,
        ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        now: float | None = None,
    ) -> str:
        """Create a session for *user_id* and return the secret session id."""
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        sid = secrets.token_urlsafe(32)
        issued = int(now if now is not None else time.time())
        record = SessionRecord(
            sid_hash=_digest(sid),
            user_id=user_id,
            created_at=issued,
            expires_at=issued + int(ttl_seconds),
        )
        with self._locked():
            records = [r for r in self._load() if r.expires_at > issued]
            records.append(record)
            self._write(records)
        return sid

    def get(self, sid: str, *, now: float | None = None) -> SessionRecord | None:
        """Return the live session for *sid*, or ``None`` (unknown/expired)."""
        current = int(now if now is not None else time.time())
        wanted = _digest(sid)
        for record in self._load():
            if record.sid_hash == wanted:
                return record if record.expires_at > current else None
        return None

    def revoke(self, sid: str) -> bool:
        """Delete the session for *sid*; True if one was removed."""
        wanted = _digest(sid)
        with self._locked():
            records = self._load()
            remaining = [r for r in records if r.sid_hash != wanted]
            if len(remaining) == len(records):
                return False
            self._write(remaining)
        return True

    def revoke_user(self, user_id: str, *, keep_sid: str | None = None) -> int:
        """Delete all of *user_id*'s sessions (except *keep_sid*, if given).

        Used on password change (keep the changing session) and on account
        disable/delete (revoke everything). Returns the number removed.
        """
        keep_hash = _digest(keep_sid) if keep_sid else None
        with self._locked():
            records = self._load()
            remaining = [
                r
                for r in records
                if r.user_id != user_id or (keep_hash is not None and r.sid_hash == keep_hash)
            ]
            removed = len(records) - len(remaining)
            if removed:
                self._write(remaining)
        return removed
