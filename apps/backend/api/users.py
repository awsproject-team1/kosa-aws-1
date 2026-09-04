"""Admin user management: create/list users and assign a per-user Policy Profile.

Admin-only (Action.MANAGE_USERS). Every user is scoped to the caller's own `custom:customer_id`;
the caller never chooses another customer's partition. The per-user Policy Profile assignment is
stored on the Cognito standard `profile` attribute so the assigned user reads it at login.

One user pool holds every customer, so `authorize` is not the whole boundary: it proves the caller
may manage users, not that the *target* of a write is theirs. Reads filter on `custom:customer_id`
(`list_users`) and writes prove it before acting (`_require_same_customer`).

The Cognito client is injected. The backend never returns a password in any response; the admin
supplies the initial password and it is set as permanent, because the console has no
FORCE_CHANGE_PASSWORD flow to hand a first-login challenge to.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from apps.backend.auth import Action, AuthorizationDenied, Principal, authorize
from apps.backend.jobs.errors import RequestValidationError

_ALLOWED_ROLES = ("Admin", "User")

#: The user pool's password policy. The template declares none, so Cognito's default applies; the
#: security suite checks the template against these so the two cannot drift apart silently. They
#: are enforced *before* any Cognito write because `admin_set_user_password` is the last of three
#: calls — a rejection there used to leave a created, grouped user with no usable password.
PASSWORD_MIN_LENGTH = 8
PASSWORD_REQUIRED_CLASSES: Mapping[str, re.Pattern[str]] = {
    "uppercase": re.compile(r"[A-Z]"),
    "lowercase": re.compile(r"[a-z]"),
    "number": re.compile(r"[0-9]"),
    # Cognito's own symbol set.
    "symbol": re.compile(r"[\^$*.\[\]{}()?\"!@#%&/\\,><':;|_~`=+\-]"),
}

_LOGGER = logging.getLogger("governance.users")


def password_policy_violations(password: object) -> tuple[str, ...]:
    """Name what a candidate initial password is missing. Empty means it satisfies the pool."""
    if not isinstance(password, str):
        return ("string",)
    violations = []
    if len(password) < PASSWORD_MIN_LENGTH:
        violations.append(f"at least {PASSWORD_MIN_LENGTH} characters")
    violations.extend(
        name
        for name, pattern in PASSWORD_REQUIRED_CLASSES.items()
        if pattern.search(password) is None
    )
    return tuple(violations)


def normalize_email(value: object) -> str:
    """Trim and lower-case an address so creation and sign-in agree on the username.

    The pool was created without `UsernameConfiguration`, which leaves usernames case-sensitive:
    an account created as `Jin@…` cannot sign in as `jin@…`. Email is case-insensitive in practice,
    so the address is canonicalized once here and every Cognito call uses the same spelling.
    """
    if not isinstance(value, str) or "@" not in value or not value.strip():
        raise UserManagementError("email is invalid")
    return value.strip().lower()


class UserManagementError(RequestValidationError):
    """Raised for invalid user-management requests.

    A `RequestValidationError` so the public mapping answers 400 rather than falling through to
    500: these are malformed public fields, not server faults.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateUserRequest:
    email: str
    role: str
    #: The initial password the admin chose. Set as permanent (see the module docstring), so it
    #: is "temporary" only by convention — nothing forces a change at first login.
    temporary_password: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "email", normalize_email(self.email))
        if self.role not in _ALLOWED_ROLES:
            raise UserManagementError("role must be Admin or User")
        violations = password_policy_violations(self.temporary_password)
        if violations:
            # Names only — the message travels to the client and into logs, the password must not.
            raise UserManagementError("temporary_password must contain " + ", ".join(violations))


class CognitoUserClient(Protocol):
    def admin_create_user(self, **kwargs: object) -> dict: ...
    def admin_add_user_to_group(self, **kwargs: object) -> dict: ...
    def admin_set_user_password(self, **kwargs: object) -> dict: ...
    def admin_update_user_attributes(self, **kwargs: object) -> dict: ...
    def admin_get_user(self, **kwargs: object) -> dict: ...
    def admin_delete_user(self, **kwargs: object) -> dict: ...
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
        # The pool is shared across customers, so an address already taken by another customer
        # must not be re-created here: pool-wide username uniqueness is what stops this call from
        # reaching a foreign account, and the password write below must not run if it fires.
        # Surfaced as a client error rather than an opaque 500.
        try:
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
        except Exception as error:
            if _aws_error_code(error) == "UsernameExistsException":
                raise UserManagementError("a user with that email already exists") from None
            raise
        # Three writes, no transaction. If the group or password step fails, the user that the
        # first call created is real but cannot sign in — and the admin, seeing an error, retries
        # and gets "already exists". Undo the creation so the pool never holds that half-made
        # account; the original failure is what gets reported.
        try:
            self._client.admin_add_user_to_group(
                UserPoolId=self._pool, Username=request.email, GroupName=request.role
            )
            self._client.admin_set_user_password(
                UserPoolId=self._pool,
                Username=request.email,
                Password=request.temporary_password,
                Permanent=True,
            )
        except Exception as error:
            self._discard_half_created(request.email)
            if _aws_error_code(error) == "InvalidPasswordException":
                raise UserManagementError(
                    "temporary_password does not satisfy the user pool policy"
                ) from None
            raise
        return {"email": request.email, "role": request.role, "customer_id": principal.customer_id}

    def _discard_half_created(self, email: str) -> None:
        try:
            self._client.admin_delete_user(UserPoolId=self._pool, Username=email)
        except Exception as error:  # noqa: BLE001 - the original failure must surface, not this
            _LOGGER.warning(
                "could not discard half-created user: %s",
                _aws_error_code(error) or type(error).__name__,
            )

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
                users.append(
                    {
                        "username": user.get("Username"),
                        "email": attrs.get("email"),
                        "customer_id": attrs.get("custom:customer_id"),
                        "profile": attrs.get("profile"),
                        "status": user.get("UserStatus"),
                        "enabled": user.get("Enabled"),
                    }
                )
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
        email = normalize_email(email)
        if not isinstance(policy_profile_id, str) or not policy_profile_id.strip():
            raise UserManagementError("policy_profile_id is invalid")
        self._require_same_customer(principal, email)
        self._client.admin_update_user_attributes(
            UserPoolId=self._pool,
            Username=email,
            UserAttributes=[{"Name": "profile", "Value": policy_profile_id}],
        )
        return {"email": email, "profile": policy_profile_id}

    def delete_user(self, principal: Principal, *, email: str) -> dict[str, object]:
        """Permanently delete a user in the caller's own customer partition.

        The pool is shared across customers, so the target's `custom:customer_id` is read and
        matched before the delete (same boundary as `assign_profile`): a user that does not exist
        and a user owned by someone else raise the identical denial, so this never becomes a
        cross-tenant existence oracle. The delete removes the Cognito user and its group
        memberships; it does not touch that user's past audit events.
        """
        _require(principal)
        authorize(principal, Action.MANAGE_USERS)
        if not isinstance(email, str) or "@" not in email:
            raise UserManagementError("email is invalid")
        self._require_same_customer(principal, email)
        self._client.admin_delete_user(UserPoolId=self._pool, Username=email)
        return {"email": email, "deleted": True}

    def _require_same_customer(self, principal: Principal, email: str) -> None:
        """Refuse a write aimed at a user outside the caller's own customer partition.

        The username is a caller-supplied address into a pool shared by every customer, so the
        target's `custom:customer_id` must be read and matched before any write. A user that does
        not exist and a user owned by someone else raise the identical denial: answering them
        differently would turn this endpoint into a cross-tenant existence oracle.

        Only "no such user" is a denial. Every other read failure — a missing IAM grant above all —
        is a server fault and is re-raised as one: reporting it as 403 would make a broken
        deployment look like a working boundary refusing every legitimate request.
        """
        try:
            user = self._client.admin_get_user(UserPoolId=self._pool, Username=email)
        except Exception as error:
            if _aws_error_code(error) != "UserNotFoundException":
                raise
            raise AuthorizationDenied("the user is not in the caller's customer") from None
        attributes = user.get("UserAttributes", []) if isinstance(user, Mapping) else []
        owner = None
        for attribute in attributes if isinstance(attributes, list) else []:
            if isinstance(attribute, Mapping) and attribute.get("Name") == "custom:customer_id":
                owner = attribute.get("Value")
        if owner is None or owner != principal.customer_id:
            raise AuthorizationDenied("the user is not in the caller's customer")


def _require(principal: object) -> None:
    if not isinstance(principal, Principal):
        raise TypeError("principal must be a Principal")


def _aws_error_code(error: BaseException) -> str | None:
    """The service error code botocore attaches, or None.

    botocore builds its exception classes at runtime, so there is no class to import and compare
    against; the response code is the stable identity. Read defensively so a fake client raising a
    plain exception simply reads as "no code" instead of masking the real fault.
    """
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None
    detail = response.get("Error")
    code = detail.get("Code") if isinstance(detail, Mapping) else None
    return code if isinstance(code, str) else None
