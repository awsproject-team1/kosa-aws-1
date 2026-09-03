"""Admin user management: create/list users and assign a per-user Policy Profile.

Admin-only (Action.MANAGE_USERS). Every user is scoped to the caller's own `custom:customer_id`;
the caller never chooses another customer's partition. The per-user Policy Profile assignment is
stored on the Cognito standard `profile` attribute so the assigned user reads it at login.

The Cognito client is injected. The backend never returns passwords in a list; a create returns the
one-time temporary password decision to the admin exactly once (the admin supplies it).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from apps.backend.auth import Action, Principal, authorize

_ALLOWED_ROLES = ("Admin", "User")


class UserManagementError(ValueError):
    """Raised for invalid user-management requests."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateUserRequest:
    email: str
    role: str
    temporary_password: str

    def __post_init__(self) -> None:
        if not isinstance(self.email, str) or "@" not in self.email:
            raise UserManagementError("email is invalid")
        if self.role not in _ALLOWED_ROLES:
            raise UserManagementError("role must be Admin or User")
        if not isinstance(self.temporary_password, str) or len(self.temporary_password) < 8:
            raise UserManagementError("temporary_password must be at least 8 characters")


class CognitoUserClient(Protocol):
    def admin_create_user(self, **kwargs: object) -> dict: ...
    def admin_add_user_to_group(self, **kwargs: object) -> dict: ...
    def admin_set_user_password(self, **kwargs: object) -> dict: ...
    def admin_update_user_attributes(self, **kwargs: object) -> dict: ...
    def list_users(self, **kwargs: object) -> dict: ...


class UserManagementService:
    """Cognito-backed, customer-scoped user administration."""

    def __init__(self, *, client: CognitoUserClient, user_pool_id: str) -> None:
        if client is None or not isinstance(user_pool_id, str) or not user_pool_id.strip():
            raise TypeError("client and user_pool_id are required")
        self._client = client
        self._pool = user_pool_id

    def create_user(self, principal: Principal, request: CreateUserRequest) -> dict[str, object]:
        _require(principal)
        authorize(principal, Action.MANAGE_USERS)
        self._client.admin_create_user(
            UserPoolId=self._pool,
            Username=request.email,
            MessageAction="SUPPRESS",
            UserAttributes=[
                {"Name": "email", "Value": request.email},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "custom:customer_id", "Value": principal.customer_id},
            ],
        )
        self._client.admin_add_user_to_group(
            UserPoolId=self._pool, Username=request.email, GroupName=request.role
        )
        self._client.admin_set_user_password(
            UserPoolId=self._pool,
            Username=request.email,
            Password=request.temporary_password,
            Permanent=True,
        )
        return {"email": request.email, "role": request.role, "customer_id": principal.customer_id}

    def list_users(self, principal: Principal) -> tuple[dict[str, object], ...]:
        _require(principal)
        authorize(principal, Action.MANAGE_USERS)
        users: list[dict[str, object]] = []
        kwargs: dict[str, object] = {"UserPoolId": self._pool, "Limit": 60}
        while True:
            page = self._client.list_users(**kwargs)
            for user in page.get("Users", []):
                attrs = {a["Name"]: a["Value"] for a in user.get("Attributes", [])}
                if attrs.get("custom:customer_id") != principal.customer_id:
                    continue  # only the caller's own customer
                users.append({
                    "username": user.get("Username"),
                    "email": attrs.get("email"),
                    "customer_id": attrs.get("custom:customer_id"),
                    "profile": attrs.get("profile"),
                    "status": user.get("UserStatus"),
                    "enabled": user.get("Enabled"),
                })
            token = page.get("PaginationToken")
            if not token:
                break
            kwargs["PaginationToken"] = token
        return tuple(users)

    def assign_profile(
        self, principal: Principal, *, email: str, policy_profile_id: str
    ) -> dict[str, object]:
        _require(principal)
        authorize(principal, Action.MANAGE_USERS)
        if not isinstance(email, str) or "@" not in email:
            raise UserManagementError("email is invalid")
        if not isinstance(policy_profile_id, str) or not policy_profile_id.strip():
            raise UserManagementError("policy_profile_id is invalid")
        self._client.admin_update_user_attributes(
            UserPoolId=self._pool,
            Username=email,
            UserAttributes=[{"Name": "profile", "Value": policy_profile_id}],
        )
        return {"email": email, "profile": policy_profile_id}


def _require(principal: object) -> None:
    if not isinstance(principal, Principal):
        raise TypeError("principal must be a Principal")
