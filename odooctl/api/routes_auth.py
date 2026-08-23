"""Login, session, and user-management routes.

POST /auth/login     — email/password login; sets the session cookie.
POST /auth/logout    — revoke the current browser session.
GET  /auth/me        — the authenticated principal (cookie or bearer).
POST /auth/password  — self-service password change (session login only).

GET    /users               — list accounts (admin+).
POST   /users               — create an account (admin+, role ceiling).
PATCH  /users/{user_id}     — roles / disabled / name (admin+, guards).
POST   /users/{user_id}/password — admin password reset (revokes sessions).
DELETE /users/{user_id}     — delete an account (admin+, not yourself).

Guardrails mirror token minting: nobody can grant, edit, or reset an account
whose role outranks their own. Failed logins back off per email address.

No privileged imports — satisfies the runner contract.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from odooctl.api.auth import auth_dir, get_principal, require_action
from odooctl.security.principals import Principal, PrincipalKind, Role, role_rank
from odooctl.security.rbac import Action
from odooctl.security.sessions import (
    DEFAULT_SESSION_TTL_SECONDS,
    SESSION_COOKIE,
    SessionStore,
)
from odooctl.security.users import (
    UserExists,
    UserNotFound,
    UserRecord,
    UserStore,
    verify_password,
)

router = APIRouter()

# Failed-login backoff: after LOGIN_MAX_FAILURES failures for one email within
# LOGIN_WINDOW_SECONDS, further attempts get 429 until the window expires.
# In-memory (per server process) — this bounds online guessing, not offline.
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60


class LoginRequest(BaseModel):
    email: str
    password: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class UserCreateRequest(BaseModel):
    email: str
    password: str
    roles: list[str] = ["viewer"]
    name: str = ""


class UserPatchRequest(BaseModel):
    roles: list[str] | None = None
    disabled: bool | None = None
    name: str | None = None


class PasswordResetRequest(BaseModel):
    new_password: str


def _failures(request: Request) -> dict:
    state = request.app.state
    if not hasattr(state, "login_failures"):
        state.login_failures = {}
    return state.login_failures


def _check_backoff(request: Request, email: str) -> None:
    now = time.time()
    attempts = [t for t in _failures(request).get(email, []) if now - t < LOGIN_WINDOW_SECONDS]
    _failures(request)[email] = attempts
    if len(attempts) >= LOGIN_MAX_FAILURES:
        raise HTTPException(
            status_code=429,
            detail="Too many failed login attempts; try again later",
        )


def _record_failure(request: Request, email: str) -> None:
    _failures(request).setdefault(email, []).append(time.time())


def _clear_failures(request: Request, email: str) -> None:
    _failures(request).pop(email, None)


def _parse_roles(raw: list[str]) -> list[Role]:
    roles: list[Role] = []
    for value in raw:
        try:
            roles.append(Role(value))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unknown role: {value!r}")
    return roles


def _principal_rank(principal: Principal) -> int:
    return max((role_rank(r) for r in principal.roles), default=-1)


def _require_rank_ceiling(principal: Principal, roles: list[Role], *, verb: str) -> None:
    ceiling = _principal_rank(principal)
    for role in roles:
        if role_rank(role) > ceiling:
            raise HTTPException(
                status_code=403,
                detail=f"Cannot {verb} a {role.value!r} account above your own role",
            )


def _require_outranks_target(principal: Principal, target: UserRecord, *, verb: str) -> None:
    target_role = target.max_role()
    if target_role is not None and role_rank(target_role) > _principal_rank(principal):
        raise HTTPException(
            status_code=403,
            detail=f"Cannot {verb} an account whose role outranks your own",
        )


def _is_self(request: Request, principal: Principal, target: UserRecord) -> bool:
    if getattr(request.state, "session_user_id", None) == target.id:
        return True
    return principal.kind == PrincipalKind.USER and principal.id == target.email


# --------------------------------------------------------------------- auth
@router.post("/auth/login")
def login(body: LoginRequest, request: Request, response: Response):
    """Email/password login. On success, sets the ``odooctl_session`` cookie.

    The cookie is ``HttpOnly`` + ``SameSite=Lax``; combined with JSON-only
    request bodies and no CORS allowance this keeps cross-site request forgery
    out without a token round-trip.
    """
    email = body.email.strip().lower()
    _check_backoff(request, email)

    directory = auth_dir(request)
    user = UserStore(directory).authenticate(email, body.password)
    if user is None:
        _record_failure(request, email)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    _clear_failures(request, email)

    sid = SessionStore(directory).create(user.id, ttl_seconds=DEFAULT_SESSION_TTL_SECONDS)
    response.set_cookie(
        SESSION_COOKIE,
        sid,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=DEFAULT_SESSION_TTL_SECONDS,
        secure=request.url.scheme == "https",
    )
    return {"user": user.to_public_dict(), "session_ttl_seconds": DEFAULT_SESSION_TTL_SECONDS}


@router.post("/auth/logout")
def logout(request: Request, response: Response):
    """Revoke the current browser session (idempotent)."""
    sid = request.cookies.get(SESSION_COOKIE)
    revoked = False
    if sid:
        revoked = SessionStore(auth_dir(request)).revoke(sid)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"revoked": revoked}


@router.get("/auth/me")
def me(request: Request, principal: Principal = Depends(get_principal)):
    """The authenticated principal, for the SPA header and CLI whoami."""
    return {
        "id": principal.id,
        "org_id": principal.org_id,
        "kind": principal.kind.value,
        "display": principal.display,
        "roles": sorted(r.value for r in principal.roles),
        "session": bool(getattr(request.state, "session_sid", None)),
    }


@router.post("/auth/password")
def change_password(
    body: PasswordChangeRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
):
    """Self-service password change. Requires a session (cookie) login.

    Verifies the current password, then revokes every *other* session of the
    account so a hijacked session cannot survive a password rotation.
    """
    user_id = getattr(request.state, "session_user_id", None)
    if user_id is None:
        raise HTTPException(
            status_code=400,
            detail="Password change requires a browser session login",
        )
    directory = auth_dir(request)
    store = UserStore(directory)
    user = store.get(user_id)
    if not verify_password(user.password_hash, body.current_password):
        raise HTTPException(status_code=403, detail="Current password is incorrect")
    try:
        store.set_password(user.id, body.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    SessionStore(directory).revoke_user(
        user.id, keep_sid=getattr(request.state, "session_sid", None)
    )
    return {"changed": True}


# -------------------------------------------------------------------- users
@router.get("/users")
def list_users(
    request: Request,
    principal: Principal = Depends(require_action(Action.USERS)),
):
    store = UserStore(auth_dir(request))
    return {"users": [u.to_public_dict() for u in store.list_users()]}


@router.post("/users", status_code=201)
def create_user(
    body: UserCreateRequest,
    request: Request,
    principal: Principal = Depends(require_action(Action.USERS)),
):
    roles = _parse_roles(body.roles)
    _require_rank_ceiling(principal, roles, verb="create")
    store = UserStore(auth_dir(request))
    try:
        user = store.create(body.email, body.password, roles=roles, name=body.name)
    except UserExists as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"user": user.to_public_dict()}


def _load_target(request: Request, user_id: str) -> tuple[UserStore, UserRecord]:
    store = UserStore(auth_dir(request))
    try:
        return store, store.get(user_id)
    except UserNotFound:
        raise HTTPException(status_code=404, detail=f"No user with id {user_id!r}")


@router.patch("/users/{user_id}")
def patch_user(
    user_id: str,
    body: UserPatchRequest,
    request: Request,
    principal: Principal = Depends(require_action(Action.USERS)),
):
    store, target = _load_target(request, user_id)
    _require_outranks_target(principal, target, verb="modify")

    if body.disabled is True and _is_self(request, principal, target):
        raise HTTPException(status_code=400, detail="You cannot disable your own account")

    user = target
    if body.roles is not None:
        roles = _parse_roles(body.roles)
        _require_rank_ceiling(principal, roles, verb="grant")
        user = store.set_roles(user.id, roles)
    if body.name is not None:
        user = store.set_name(user.id, body.name)
    if body.disabled is not None:
        user = store.set_disabled(user.id, body.disabled)
        if body.disabled:
            SessionStore(auth_dir(request)).revoke_user(user.id)
    return {"user": user.to_public_dict()}


@router.post("/users/{user_id}/password")
def reset_password(
    user_id: str,
    body: PasswordResetRequest,
    request: Request,
    principal: Principal = Depends(require_action(Action.USERS)),
):
    """Admin password reset. Revokes all of the target account's sessions."""
    store, target = _load_target(request, user_id)
    _require_outranks_target(principal, target, verb="reset the password of")
    try:
        store.set_password(target.id, body.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    SessionStore(auth_dir(request)).revoke_user(target.id)
    return {"reset": True}


@router.delete("/users/{user_id}")
def delete_user(
    user_id: str,
    request: Request,
    principal: Principal = Depends(require_action(Action.USERS)),
):
    store, target = _load_target(request, user_id)
    _require_outranks_target(principal, target, verb="delete")
    if _is_self(request, principal, target):
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    store.delete(target.id)
    SessionStore(auth_dir(request)).revoke_user(target.id)
    return {"deleted": True}
