"""Cognito identity normalization and action-level Backend authorization."""

from apps.backend.auth.authorization import Action, AuthorizationDenied, authorize
from apps.backend.auth.principal import InvalidIdentityClaims, Principal, Role

__all__ = [
    "Action",
    "AuthorizationDenied",
    "InvalidIdentityClaims",
    "Principal",
    "Role",
    "authorize",
]
