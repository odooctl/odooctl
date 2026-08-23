"""User store, password hashing, session store, and CLI actor attribution."""
from __future__ import annotations

import json
import stat

import pytest

from odooctl.security.principals import PrincipalKind, Role, local_actor
from odooctl.security.sessions import SessionStore
from odooctl.security.users import (
    UserExists,
    UserNotFound,
    UserStore,
    hash_password,
    normalize_email,
    verify_password,
)


# --------------------------------------------------------------- hashing
def test_hash_and_verify_roundtrip():
    stored = hash_password("correct horse battery")
    assert stored.startswith("scrypt$")
    assert verify_password(stored, "correct horse battery")
    assert not verify_password(stored, "wrong password")


def test_hashes_are_salted():
    assert hash_password("same-password") != hash_password("same-password")


def test_verify_rejects_garbage_and_unknown_schemes():
    assert not verify_password("", "x")
    assert not verify_password("not-a-hash", "x")
    assert not verify_password("argon2id$whatever", "x")
    assert not verify_password("scrypt$bad$fields", "x")


def test_normalize_email():
    assert normalize_email("  Alice@Example.COM ") == "alice@example.com"
    with pytest.raises(ValueError):
        normalize_email("not-an-email")
    with pytest.raises(ValueError):
        normalize_email("spaces in@example.com")


# ----------------------------------------------------------------- store
@pytest.fixture
def store(tmp_path):
    return UserStore(tmp_path / "auth")


def test_create_and_lookup(store):
    user = store.create("Alice@Example.com", "password123", roles=[Role.ADMIN], name="Alice")
    assert user.email == "alice@example.com"
    assert user.roles == ("admin",)
    assert store.get(user.id).email == "alice@example.com"
    assert store.get_by_email("ALICE@example.com").id == user.id
    assert [u.email for u in store.list_users()] == ["alice@example.com"]


def test_create_rejects_duplicate_email(store):
    store.create("alice@example.com", "password123")
    with pytest.raises(UserExists):
        store.create("Alice@example.com", "password456")


def test_create_rejects_short_password_and_bad_email(store):
    with pytest.raises(ValueError):
        store.create("alice@example.com", "short")
    with pytest.raises(ValueError):
        store.create("nope", "password123")


def test_password_hash_never_in_public_dict(store):
    user = store.create("alice@example.com", "password123")
    public = user.to_public_dict()
    assert "password_hash" not in public
    assert "scrypt$" not in json.dumps(public)


def test_users_file_is_owner_only(store):
    store.create("alice@example.com", "password123")
    mode = stat.S_IMODE(store.path.stat().st_mode)
    assert mode == 0o600


def test_authenticate_paths(store):
    user = store.create("alice@example.com", "password123", roles=[Role.OPERATOR])
    assert store.authenticate("alice@example.com", "password123").id == user.id
    assert store.authenticate("alice@example.com", "wrong-password") is None
    assert store.authenticate("nobody@example.com", "password123") is None
    store.set_disabled(user.id, True)
    assert store.authenticate("alice@example.com", "password123") is None
    store.set_disabled(user.id, False)
    assert store.authenticate("alice@example.com", "password123").id == user.id


def test_set_password_and_roles(store):
    user = store.create("alice@example.com", "password123")
    store.set_password(user.id, "new-password-1")
    assert store.authenticate("alice@example.com", "new-password-1") is not None
    assert store.authenticate("alice@example.com", "password123") is None
    with pytest.raises(ValueError):
        store.set_password(user.id, "short")
    updated = store.set_roles(user.id, [Role.ADMIN, Role.VIEWER])
    assert set(updated.roles) == {"admin", "viewer"}


def test_delete_user(store):
    user = store.create("alice@example.com", "password123")
    store.delete(user.id)
    assert store.list_users() == []
    with pytest.raises(UserNotFound):
        store.delete(user.id)


def test_mutate_unknown_user_raises(store):
    with pytest.raises(UserNotFound):
        store.set_roles("u-missing", [Role.VIEWER])


def test_to_principal(store):
    user = store.create("alice@example.com", "password123", roles=[Role.ADMIN], name="Alice")
    principal = user.to_principal()
    assert principal.kind == PrincipalKind.USER
    assert principal.id == "alice@example.com"
    assert principal.has_role(Role.ADMIN)
    assert principal.display == "Alice"


def test_unknown_role_strings_are_ignored_in_role_set(store):
    user = store.create("alice@example.com", "password123", roles=[Role.VIEWER])
    hacked = user.__class__.from_dict({**user.to_dict(), "roles": ["viewer", "superroot"]})
    assert hacked.role_set() == frozenset({Role.VIEWER})


# -------------------------------------------------------------- sessions
@pytest.fixture
def sessions(tmp_path):
    return SessionStore(tmp_path / "auth")


def test_session_lifecycle(sessions):
    sid = sessions.create("u-1", ttl_seconds=100, now=1000)
    record = sessions.get(sid, now=1050)
    assert record is not None and record.user_id == "u-1"
    assert sessions.get(sid, now=1100) is None  # expired
    assert sessions.get("bogus", now=1050) is None


def test_session_file_stores_hash_not_sid(sessions):
    sid = sessions.create("u-1", ttl_seconds=100, now=1000)
    raw = sessions.path.read_text()
    assert sid not in raw
    mode = stat.S_IMODE(sessions.path.stat().st_mode)
    assert mode == 0o600


def test_session_revoke(sessions):
    sid = sessions.create("u-1", ttl_seconds=100, now=1000)
    assert sessions.revoke(sid) is True
    assert sessions.get(sid, now=1001) is None
    assert sessions.revoke(sid) is False


def test_revoke_user_with_keep(sessions):
    keep = sessions.create("u-1", ttl_seconds=100, now=1000)
    other = sessions.create("u-1", ttl_seconds=100, now=1000)
    unrelated = sessions.create("u-2", ttl_seconds=100, now=1000)
    removed = sessions.revoke_user("u-1", keep_sid=keep)
    assert removed == 1
    assert sessions.get(keep, now=1001) is not None
    assert sessions.get(other, now=1001) is None
    assert sessions.get(unrelated, now=1001) is not None


def test_create_prunes_expired_sessions(sessions):
    sessions.create("u-1", ttl_seconds=10, now=1000)
    sessions.create("u-2", ttl_seconds=100, now=2000)
    data = json.loads(sessions.path.read_text())
    assert len(data["sessions"]) == 1


# --------------------------------------------------------------- actor
def test_local_actor_names_os_user(monkeypatch):
    monkeypatch.delenv("ODOOCTL_ACTOR", raising=False)
    actor = local_actor()
    assert actor.startswith("local:") and len(actor) > len("local:")


def test_local_actor_env_override(monkeypatch):
    monkeypatch.setenv("ODOOCTL_ACTOR", "ci:release-pipeline")
    assert local_actor() == "ci:release-pipeline"
