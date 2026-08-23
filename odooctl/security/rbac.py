"""Role-based access control matrix and check/require helpers.

:func:`require` is the primitive a caller uses to gate an action; read actions
are modelled too so the future API can return 403 instead of leaking data.
These helpers define the *policy* — they do not auto-apply to anything. The
existing CLI does not yet call them; wiring them into the API/runner and
service paths is future work (M12+).

Two dimensions decide an outcome:

1. The role → action matrix (:data:`ROLE_ACTIONS`).
2. Environment protection: destructive actions targeting a protected /
   production environment require admin-or-higher even when an operator is
   otherwise allowed to run them on non-prod.
"""
from __future__ import annotations

from enum import Enum

from odooctl.security.principals import Principal, Role, role_rank


class Action(str, Enum):
    """Actions the platform gates.

    Read-family: observe state without mutation.
    Write-family: mutate environments, data, or configuration.
    """

    # read-family
    READ = "read"
    STATUS = "status"
    LOGS = "logs"
    BACKUPS = "backups"          # view/list backups
    OPERATIONS = "operations"    # view operation history
    AUDIT = "audit"              # read the audit trail

    # write-family
    BACKUP = "backup"            # create a backup
    DEPLOY = "deploy"
    CLONE = "clone"
    RESTORE = "restore"
    PROMOTE = "promote"
    ENV = "env"                  # create/destroy environments
    SECRETS = "secrets"          # manage secret store
    CANCEL = "cancel"            # cancel a queued/running operation
    USERS = "users"              # manage user accounts and ownership


READ_ACTIONS: frozenset[Action] = frozenset(
    {Action.READ, Action.STATUS, Action.LOGS, Action.BACKUPS, Action.OPERATIONS, Action.AUDIT}
)

WRITE_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.BACKUP,
        Action.DEPLOY,
        Action.CLONE,
        Action.RESTORE,
        Action.PROMOTE,
        Action.ENV,
        Action.SECRETS,
        Action.CANCEL,
        Action.USERS,
    }
)

# Destructive actions that, when aimed at a protected/production environment,
# require admin-or-higher regardless of the base matrix.
DESTRUCTIVE_ON_PROTECTED: frozenset[Action] = frozenset(
    {Action.DEPLOY, Action.CLONE, Action.RESTORE, Action.PROMOTE, Action.ENV, Action.SECRETS}
)

# Operation kinds whose blast radius is the whole compose project rather than
# the target environment alone: environments share containers in the single
# compose-file model, so e.g. restarting the odoo service for "staging" also
# disrupts production. For these kinds, protection of ANY environment in the
# project applies.
PROJECT_WIDE_DISRUPTION_KINDS: frozenset[str] = frozenset({"service_restart"})


def kind_protected(cfg, kind: str, environment: str) -> bool:
    """Effective protected flag for an operation *kind* on *environment*.

    ``cfg`` is duck-typed (needs ``is_protected`` and ``environments``) so this
    stays importable from the unprivileged API layer.
    """
    if kind in PROJECT_WIDE_DISRUPTION_KINDS:
        return any(cfg.is_protected(name) for name in cfg.environments)
    return bool(cfg.is_protected(environment))

# Minimum role required to act on a protected/production environment for a
# destructive action.
_PROTECTED_FLOOR: Role = Role.ADMIN

# Base role → allowed actions matrix.
ROLE_ACTIONS: dict[Role, frozenset[Action]] = {
    Role.VIEWER: READ_ACTIONS,
    Role.OPERATOR: READ_ACTIONS
    | frozenset({Action.BACKUP, Action.DEPLOY, Action.CLONE, Action.RESTORE, Action.CANCEL}),
    Role.ADMIN: READ_ACTIONS | WRITE_ACTIONS,
    Role.OWNER: READ_ACTIONS | WRITE_ACTIONS,
}


class AccessDenied(PermissionError):
    """Raised when a principal is not permitted to perform an action."""

    def __init__(self, principal: Principal, action: Action, reason: str = "") -> None:
        self.principal = principal
        self.action = Action(action)
        self.reason = reason or "insufficient role"
        super().__init__(
            f"Access denied: {principal.identity} cannot perform "
            f"'{self.action.value}' ({self.reason})"
        )


def _roles_allow(principal: Principal, action: Action) -> bool:
    return any(action in ROLE_ACTIONS.get(role, frozenset()) for role in principal.roles)


def is_allowed(principal: Principal, action: Action, *, protected: bool = False) -> bool:
    """Return True if *principal* may perform *action*.

    When *protected* is True and the action is destructive, the principal must
    hold admin-or-higher in addition to the base matrix allowance.
    """
    action = Action(action)
    if not _roles_allow(principal, action):
        return False
    if protected and action in DESTRUCTIVE_ON_PROTECTED:
        return principal.has_at_least(_PROTECTED_FLOOR)
    return True


def require(principal: Principal, action: Action, *, protected: bool = False) -> None:
    """Raise :class:`AccessDenied` if *principal* may not perform *action*."""
    action = Action(action)
    if not _roles_allow(principal, action):
        raise AccessDenied(principal, action, "role lacks this action")
    if protected and action in DESTRUCTIVE_ON_PROTECTED and not principal.has_at_least(_PROTECTED_FLOOR):
        raise AccessDenied(
            principal,
            action,
            f"protected environment requires {_PROTECTED_FLOOR.value} or higher",
        )


def allowed_actions(role: Role, *, protected: bool = False) -> frozenset[Action]:
    """Return the set of actions *role* may perform (optionally on protected)."""
    base = ROLE_ACTIONS.get(Role(role), frozenset())
    if not protected:
        return base
    if role_rank(role) >= role_rank(_PROTECTED_FLOOR):
        return base
    return frozenset(a for a in base if a not in DESTRUCTIVE_ON_PROTECTED)


def role_matrix() -> dict[str, dict[str, bool]]:
    """Return a serialisable matrix: ``{role: {action: allowed}}``.

    Used by ``odooctl security rbac`` to render the matrix and by tests to
    assert full coverage of every role/action pair.
    """
    matrix: dict[str, dict[str, bool]] = {}
    for role in Role:
        base = ROLE_ACTIONS.get(role, frozenset())
        matrix[role.value] = {action.value: action in base for action in Action}
    return matrix
