"""Authentication and RBAC dependencies for the FastAPI service.

Two credential kinds resolve to the same :class:`Principal` contract:

- **Bearer tokens** (``Authorization: Bearer ...``): stateless HMAC tokens
  from ``odooctl.security.tokens`` with roles embedded in the payload. The
  credential for CLI/CI — verifiable offline, not revocable, short-lived.
- **Session cookies** (``odooctl_session``): revocable server-side sessions
  from ``odooctl.security.sessions``, created by ``POST /auth/login``. Roles
  are resolved from the user store on *every* request, so role changes and
  account disabling take effect immediately.

Roles drive RBAC checks via ``odooctl.security.rbac``.

No privileged imports — satisfies the runner contract.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, Header, HTTPException, Request

from odooctl.security import rbac, tokens
from odooctl.security.principals import Principal, PrincipalKind, Role
from odooctl.security.sessions import SESSION_COOKIE, SessionStore
from odooctl.security.users import UserNotFound, UserStore


def auth_dir(request: Request) -> Path:
    """Directory holding the server-level user and session stores.

    Defaults to the registry's directory (``~/.config/odooctl``) so accounts
    span every registered project; ``create_app(auth_dir=...)`` overrides for
    tests.
    """
    configured = getattr(request.app.state, "auth_dir", None)
    if configured is not None:
        return Path(configured)
    return Path(request.app.state.registry_loader().path).parent


def _principal_from_token(request: Request, token_str: str) -> Principal:
    api_key: str = request.app.state.api_key
    try:
        payload = tokens.verify(api_key, token_str, action="api")
    except tokens.TokenExpired:
        raise HTTPException(status_code=401, detail="Token expired")
    except tokens.TokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Expose the token's project scope claim so routes that are not
    # project-scoped in the path (/operations/{id}...) can enforce that the
    # operation belongs to the project the token was minted for. "*" (the
    # session-token default) means all projects.
    request.state.token_project = str(payload.get("proj") or "*")

    roles_raw = payload.get("roles", ["viewer"])
    role_set: list[Role] = []
    for r in roles_raw:
        try:
            role_set.append(Role(r))
        except ValueError:
            pass
    if not role_set:
        role_set = [Role.VIEWER]

    sub = payload.get("sub", "api-client")
    org_id = payload.get("org", "default")
    return Principal(
        id=sub,
        org_id=org_id,
        kind=PrincipalKind.TOKEN,
        roles=frozenset(role_set),
        display=sub,
    )


def _principal_from_session(request: Request, sid: str) -> Principal:
    directory = auth_dir(request)
    session = SessionStore(directory).get(sid)
    if session is None:
        raise HTTPException(status_code=401, detail="Session expired or revoked")
    try:
        user = UserStore(directory).get(session.user_id)
    except UserNotFound:
        raise HTTPException(status_code=401, detail="Session user no longer exists")
    if user.disabled:
        raise HTTPException(status_code=401, detail="Account is disabled")

    # Session principals act across all projects (role-limited, org-scoped);
    # per-project confinement is a bearer-token feature.
    request.state.token_project = "*"
    # Stashed so /auth/logout and /auth/password can operate on *this* session.
    request.state.session_sid = sid
    request.state.session_user_id = user.id
    return user.to_principal()


def get_principal(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Principal:
    # Key-strength floor (F24): refuse to authenticate against any weak server
    # key. ``create_app`` already rejects a weak key at startup; this is a
    # backstop for apps constructed another way, and applies regardless of how
    # the key was supplied.
    if len(request.app.state.api_key) < tokens.MIN_API_KEY_LENGTH:
        raise HTTPException(status_code=500, detail="Server API key is too weak")

    if authorization:
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Missing bearer token")
        return _principal_from_token(request, authorization.split(" ", 1)[1])

    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        return _principal_from_session(request, sid)

    raise HTTPException(status_code=401, detail="Missing bearer token or session cookie")


def enforce_project_scope(request: Request, project: str) -> None:
    """Reject access when the token's project claim does not cover *project*.

    A token minted with a concrete ``proj`` claim (not ``"*"``) is confined to
    that one project: it must not read from or enqueue against any other
    project. Session tokens use ``proj="*"`` and are unaffected. Responds 404
    (not 403) so a scoped token cannot enumerate which other projects exist.
    """
    claim = str(getattr(request.state, "token_project", None) or "")
    if claim != "*" and claim != project:
        raise HTTPException(status_code=404, detail=f"Project {project!r} not found")


def require_action(action: rbac.Action):
    """Dependency factory: authenticate and require *action* via RBAC."""

    def _dep(principal: Principal = Depends(get_principal)) -> Principal:
        try:
            rbac.require(principal, action)
        except rbac.AccessDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        return principal

    return _dep
