"""Fail-closed identity normalization for verified Cognito access-token claims."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class InvalidIdentityClaims(ValueError):
    """Raised when verified claims cannot form an authorized product identity."""


class Role(StrEnum):
    """Product roles mapped from exact Cognito group names."""

    ADMIN = "Admin"
    USER = "User"


_GROUP_ROLES = {role.value: role for role in Role}


@dataclass(frozen=True, slots=True, kw_only=True)
class Principal:
    """Immutable identity used by Backend authorization decisions."""

    subject: str
    client_id: str
    roles: frozenset[Role]

    def __post_init__(self) -> None:
        _require_non_empty_string(self.subject, "subject")
        _require_non_empty_string(self.client_id, "client_id")
        if not isinstance(self.roles, frozenset):
            raise TypeError("roles must be a frozenset of Role values")
        if not self.roles:
            raise ValueError("roles must contain at least one Role")
        if any(not isinstance(role, Role) for role in self.roles):
            raise TypeError("roles must contain only Role values")

    @classmethod
    def from_verified_claims(cls, claims: Mapping[str, object]) -> Principal:
        """Create a principal from claims already verified by a trusted authorizer.

        Cryptographic JWT verification is intentionally outside this boundary. The
        claims still fail closed unless they describe a Cognito access token and at
        least one supported product role.
        """
        if not isinstance(claims, Mapping):
            raise InvalidIdentityClaims("verified claims must be a mapping")

        token_use = _required_claim(claims, "token_use")
        if token_use != "access":
            raise InvalidIdentityClaims("token_use must be 'access'")

        subject = _required_non_empty_claim(claims, "sub")
        client_id = _required_non_empty_claim(claims, "client_id")
        roles = _roles_from_groups(_required_claim(claims, "cognito:groups"))

        return cls(subject=subject, client_id=client_id, roles=roles)


def _required_claim(claims: Mapping[str, object], name: str) -> object:
    try:
        return claims[name]
    except KeyError as error:
        raise InvalidIdentityClaims(f"missing required claim: {name}") from error


def _required_non_empty_claim(claims: Mapping[str, object], name: str) -> str:
    value = _required_claim(claims, name)
    try:
        _require_non_empty_string(value, name)
    except (TypeError, ValueError) as error:
        raise InvalidIdentityClaims(str(error)) from error
    return value


def _roles_from_groups(value: object) -> frozenset[Role]:
    if not isinstance(value, list):
        raise InvalidIdentityClaims("cognito:groups must be an array of strings")

    roles: set[Role] = set()
    for group in value:
        if not isinstance(group, str) or not group.strip():
            raise InvalidIdentityClaims("cognito:groups must contain only non-empty strings")
        role = _GROUP_ROLES.get(group)
        if role is not None:
            roles.add(role)

    if not roles:
        raise InvalidIdentityClaims(
            "cognito:groups must include at least one supported product role"
        )
    return frozenset(roles)


def _require_non_empty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
