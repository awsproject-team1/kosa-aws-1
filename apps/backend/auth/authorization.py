"""Action-level Backend authorization for authenticated principals."""

from enum import StrEnum

from apps.backend.auth.principal import Principal, Role


class Action(StrEnum):
    """Currently approved Backend actions."""

    START_ASSESSMENT = "START_ASSESSMENT"
    READ_JOB = "READ_JOB"


class AuthorizationDenied(PermissionError):
    """Raised when a principal is not allowed to perform an action."""


_USER_ACTIONS = frozenset({Action.START_ASSESSMENT, Action.READ_JOB})
_ROLE_ACTIONS = {
    Role.USER: _USER_ACTIONS,
    Role.ADMIN: _USER_ACTIONS,
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
