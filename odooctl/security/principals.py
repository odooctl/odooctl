"""Organization / user / role / principal identity models.

These models are deliberately transport-agnostic dataclasses so the future
FastAPI service (M12) can construct a ``Principal`` from an authenticated
request, a capability token subject, or a service account without depending on
any web framework here.

A ``Principal`` is the single identity object that RBAC checks (``rbac``) and
audit records reason about. It carries the org it belongs to, the kind of
identity it represents (human user, service account, or token-derived), and
the set of roles granted within that org.
"""
from __future__ import annotations

import getpass
import os
from dataclasses import dataclass, field
from enum import Enum


class Role(str, Enum):
    """Platform roles, ordered from most to least privileged.

    - ``owner``: every action, including protected/production destructive ops.
    - ``admin``: manage projects/envs/secrets and promote to production.
    - ``operator``: deploy non-prod, backup, clone, restore staging.
    - ``viewer``: read-only (status/logs/backups/operations/audit).
    """

    OWNER = "owner"
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


# Privilege ordering used by RBAC elevation checks. Higher index == more power.
_ROLE_RANK: dict[Role, int] = {
    Role.VIEWER: 0,
    Role.OPERATOR: 1,
    Role.ADMIN: 2,
    Role.OWNER: 3,
}


def role_rank(role: Role) -> int:
    """Return the privilege rank for *role* (higher is more privileged)."""
    return _ROLE_RANK[Role(role)]


class PrincipalKind(str, Enum):
    """What kind of identity a principal represents."""

    USER = "user"
    SERVICE = "service"
    TOKEN = "token"


@dataclass(frozen=True)
class Org:
    """A tenant/organization boundary.

    V1 is single-tenant in practice, but principals always carry an org id so
    multi-tenant scoping can be added without reshaping identities later.
    """

    id: str
    name: str = ""


@dataclass(frozen=True)
class User:
    """A human account within an org."""

    id: str
    org_id: str
    name: str = ""
    email: str = ""


@dataclass(frozen=True)
class Principal:
    """An authenticated identity that RBAC and audit reason about.

    ``roles`` is the set of roles granted to this identity within ``org_id``.
    The principal is intentionally immutable; grant changes produce a new
    principal rather than mutating one in place.
    """

    id: str
    org_id: str
    kind: PrincipalKind = PrincipalKind.USER
    roles: frozenset[Role] = field(default_factory=frozenset)
    display: str = ""

    def __post_init__(self) -> None:
        # Normalise roles to a frozenset[Role] regardless of input iterable type.
        object.__setattr__(self, "roles", frozenset(Role(r) for r in self.roles))

    @property
    def identity(self) -> str:
        """Stable identity string for audit records, e.g. ``user:alice@acme``."""
        return f"{self.kind.value}:{self.id}@{self.org_id}"

    def has_role(self, role: Role) -> bool:
        return Role(role) in self.roles

    def max_role(self) -> Role | None:
        """Return the most privileged role held, or ``None`` if no roles."""
        if not self.roles:
            return None
        return max(self.roles, key=role_rank)

    def has_at_least(self, role: Role) -> bool:
        """True if any held role is at least as privileged as *role*."""
        floor = role_rank(role)
        return any(role_rank(r) >= floor for r in self.roles)

    @classmethod
    def for_user(cls, user: User, roles: frozenset[Role] | set[Role] | list[Role]) -> "Principal":
        return cls(
            id=user.id,
            org_id=user.org_id,
            kind=PrincipalKind.USER,
            roles=frozenset(roles),
            display=user.name or user.email or user.id,
        )

    @classmethod
    def service(cls, id: str, org_id: str, roles: frozenset[Role] | set[Role] | list[Role]) -> "Principal":
        return cls(id=id, org_id=org_id, kind=PrincipalKind.SERVICE, roles=frozenset(roles), display=id)


#: Env var that overrides CLI actor attribution (e.g. a CI job sets
#: ``ODOOCTL_ACTOR=ci:release-pipeline`` so audit records name the pipeline).
ACTOR_ENV_VAR = "ODOOCTL_ACTOR"


def local_actor() -> str:
    """Attribution string for operations invoked from the local CLI.

    A shell on the server is the local-admin principal; what audit needs is
    *which* OS account acted, not a ``"cli"`` literal. Honors
    :data:`ACTOR_ENV_VAR` so wrappers (CI, cron) can attribute themselves.
    """
    override = os.environ.get(ACTOR_ENV_VAR, "").strip()
    if override:
        return override
    try:
        username = getpass.getuser()
    except OSError:
        username = "unknown"
    return f"local:{username}"
