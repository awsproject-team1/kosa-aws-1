"""Action-level Backend authorization for authenticated principals."""

from enum import StrEnum

from apps.backend.auth.principal import Principal, Role


class Action(StrEnum):
    """Currently approved Backend actions."""

    START_ASSESSMENT = "START_ASSESSMENT"
    READ_JOB = "READ_JOB"
    START_REMEDIATION = "START_REMEDIATION"
    START_DEPLOYMENT = "START_DEPLOYMENT"
    APPROVE_DEPLOYMENT = "APPROVE_DEPLOYMENT"
    REJECT_DEPLOYMENT = "REJECT_DEPLOYMENT"
    MANAGE_REMEDIATION_EXCEPTIONS = "MANAGE_REMEDIATION_EXCEPTIONS"
    MANAGE_POLICY_SOURCES = "MANAGE_POLICY_SOURCES"
    PUBLISH_POLICY_PROFILE = "PUBLISH_POLICY_PROFILE"
    READ_OBSERVABILITY = "READ_OBSERVABILITY"
    READ_AUDIT_EVENTS = "READ_AUDIT_EVENTS"
    MANAGE_USERS = "MANAGE_USERS"
    # Natural-language routing (ADR-0012). The Parent only classifies and proposes; it
    # cannot start work, so this is granted to any authenticated user.
    ORCHESTRATE = "ORCHESTRATE"


class AuthorizationDenied(PermissionError):
    """Raised when a principal is not allowed to perform an action."""


_USER_ACTIONS = frozenset(
    {
        Action.START_ASSESSMENT,
        Action.START_REMEDIATION,
        Action.START_DEPLOYMENT,
        Action.READ_JOB,
        Action.ORCHESTRATE,
    }
)
_ROLE_ACTIONS = {
    Role.USER: _USER_ACTIONS,
    Role.ADMIN: _USER_ACTIONS
    | frozenset(
        {
            Action.APPROVE_DEPLOYMENT,
            Action.REJECT_DEPLOYMENT,
            Action.MANAGE_REMEDIATION_EXCEPTIONS,
            Action.MANAGE_POLICY_SOURCES,
            Action.PUBLISH_POLICY_PROFILE,
            Action.READ_OBSERVABILITY,
            Action.READ_AUDIT_EVENTS,
            Action.MANAGE_USERS,
        }
    ),
}


def authorize(principal: Principal, action: Action) -> None:
    """Require an exact approved action for one normalized principal."""
    if not isinstance(principal, Principal):
        raise TypeError("principal must be a Principal")
    if not isinstance(action, Action):
        raise AuthorizationDenied("unsupported action")

    if any(action in _ROLE_ACTIONS.get(role, frozenset()) for role in principal.roles):
        return

    raise AuthorizationDenied(f"principal is not authorized for {action.value}")
