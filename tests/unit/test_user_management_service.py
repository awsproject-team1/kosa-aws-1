"""Customer-scope tests for Admin user management.

One Cognito user pool holds every customer, so the username an admin supplies is an address into
a shared namespace. `authorize` proves the caller may manage users; it says nothing about who the
*target* belongs to. These tests pin that second boundary on every path that reads or writes a
user, because a regression there is silent: the call succeeds and touches someone else's account.
"""

import unittest

from apps.backend.api.users import (
    CreateUserRequest,
    UserManagementError,
    UserManagementService,
)
from apps.backend.auth import AuthorizationDenied, Principal, Role

ADMIN = Principal(
    subject="admin-001",
    client_id="client-001",
    customer_id="cust-a",
    roles=frozenset({Role.ADMIN}),
)
USER = Principal(
    subject="user-001",
    client_id="client-001",
    customer_id="cust-a",
    roles=frozenset({Role.USER}),
)


class ClientError(Exception):
    """The shape botocore raises: a service error code on a `response` mapping."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeCognito:
    def __init__(self, users: dict[str, str] | None = None) -> None:
        #: email -> owning customer_id
        self.users = dict(users or {})
        self.calls: list[tuple[str, dict]] = []
        self.profiles: dict[str, str] = {}
        self.groups: dict[str, str] = {}
        self.passwords: dict[str, tuple[str, bool]] = {}
        self.pages: list[dict] | None = None
        self.get_user_error: Exception | None = None

    def admin_create_user(self, **kwargs):
        self.calls.append(("admin_create_user", kwargs))
        email = kwargs["Username"]
        if email in self.users:
            raise ClientError("UsernameExistsException")
        attributes = {a["Name"]: a["Value"] for a in kwargs["UserAttributes"]}
        self.users[email] = attributes["custom:customer_id"]
        return {}

    def admin_add_user_to_group(self, **kwargs):
        self.calls.append(("admin_add_user_to_group", kwargs))
        self.groups[kwargs["Username"]] = kwargs["GroupName"]
        return {}

    def admin_set_user_password(self, **kwargs):
        self.calls.append(("admin_set_user_password", kwargs))
        self.passwords[kwargs["Username"]] = (kwargs["Password"], kwargs["Permanent"])
        return {}

    def admin_update_user_attributes(self, **kwargs):
        self.calls.append(("admin_update_user_attributes", kwargs))
        attributes = {a["Name"]: a["Value"] for a in kwargs["UserAttributes"]}
        self.profiles[kwargs["Username"]] = attributes["profile"]
        return {}

    def admin_get_user(self, **kwargs):
        self.calls.append(("admin_get_user", kwargs))
        if self.get_user_error is not None:
            raise self.get_user_error
        email = kwargs["Username"]
        if email not in self.users:
            raise ClientError("UserNotFoundException")
        return {
            "Username": email,
            "UserAttributes": [
                {"Name": "email", "Value": email},
                {"Name": "custom:customer_id", "Value": self.users[email]},
            ],
        }

    def list_users(self, **kwargs):
        self.calls.append(("list_users", kwargs))
        if self.pages is not None:
            index = 0 if "PaginationToken" not in kwargs else int(kwargs["PaginationToken"])
            return self.pages[index]
        return {
            "Users": [
                {
                    "Username": email,
                    "UserStatus": "CONFIRMED",
                    "Enabled": True,
                    "Attributes": [
                        {"Name": "email", "Value": email},
                        {"Name": "custom:customer_id", "Value": customer},
                    ],
                }
                for email, customer in self.users.items()
            ]
        }


def _service(client: FakeCognito) -> UserManagementService:
    return UserManagementService(client=client, user_pool_id="pool-1")


class CreateUserTest(unittest.TestCase):
    def test_the_new_user_is_stamped_with_the_callers_customer(self) -> None:
        client = FakeCognito()
        result = _service(client).create_user(
            ADMIN,
            CreateUserRequest(email="new@example.com", role="User", temporary_password="pw-123456"),
        )
        self.assertEqual(client.users["new@example.com"], "cust-a")
        self.assertEqual(result["customer_id"], "cust-a")

    def test_the_caller_cannot_choose_another_customers_partition(self) -> None:
        """The request carries no customer field; the principal is the only source."""
        client = FakeCognito()
        _service(client).create_user(
            ADMIN,
            CreateUserRequest(email="new@example.com", role="User", temporary_password="pw-123456"),
        )
        _method, kwargs = client.calls[0]
        attributes = {a["Name"]: a["Value"] for a in kwargs["UserAttributes"]}
        self.assertEqual(attributes["custom:customer_id"], ADMIN.customer_id)

    def test_an_address_owned_by_another_customer_is_refused_before_the_password_write(
        self,
    ) -> None:
        """Pool-wide username uniqueness is what keeps this call off a foreign account.

        If the create is ever allowed to fall through, the `admin_set_user_password` below it
        would set a password the caller chose on a user they do not own.
        """
        client = FakeCognito({"victim@example.com": "cust-b"})
        with self.assertRaises(UserManagementError):
            _service(client).create_user(
                ADMIN,
                CreateUserRequest(
                    email="victim@example.com", role="Admin", temporary_password="pw-123456"
                ),
            )
        self.assertEqual(client.passwords, {})
        self.assertEqual(client.groups, {})

    def test_a_non_admin_cannot_create_a_user(self) -> None:
        client = FakeCognito()
        with self.assertRaises(AuthorizationDenied):
            _service(client).create_user(
                USER,
                CreateUserRequest(
                    email="new@example.com", role="User", temporary_password="pw-123456"
                ),
            )
        self.assertEqual(client.calls, [])

    def test_the_request_rejects_an_unknown_role(self) -> None:
        with self.assertRaises(UserManagementError):
            CreateUserRequest(
                email="new@example.com", role="Superuser", temporary_password="pw-123456"
            )

    def test_the_request_rejects_a_short_password(self) -> None:
        with self.assertRaises(UserManagementError):
            CreateUserRequest(email="new@example.com", role="User", temporary_password="short")

    def test_no_response_field_carries_the_password(self) -> None:
        client = FakeCognito()
        result = _service(client).create_user(
            ADMIN,
            CreateUserRequest(email="new@example.com", role="User", temporary_password="pw-123456"),
        )
        self.assertNotIn("pw-123456", str(result))


class ListUsersTest(unittest.TestCase):
    def test_only_the_callers_customer_is_returned(self) -> None:
        client = FakeCognito({"a@example.com": "cust-a", "b@example.com": "cust-b"})
        users = _service(client).list_users(ADMIN)
        self.assertEqual([u["email"] for u in users], ["a@example.com"])

    def test_every_page_is_filtered_not_just_the_first(self) -> None:
        client = FakeCognito()
        client.pages = [
            {
                "Users": [
                    {
                        "Username": "a@example.com",
                        "Attributes": [
                            {"Name": "email", "Value": "a@example.com"},
                            {"Name": "custom:customer_id", "Value": "cust-a"},
                        ],
                    }
                ],
                "PaginationToken": "1",
            },
            {
                "Users": [
                    {
                        "Username": "b@example.com",
                        "Attributes": [
                            {"Name": "email", "Value": "b@example.com"},
                            {"Name": "custom:customer_id", "Value": "cust-b"},
                        ],
                    }
                ]
            },
        ]
        users = _service(client).list_users(ADMIN)
        self.assertEqual([u["email"] for u in users], ["a@example.com"])

    def test_a_non_admin_cannot_list_users(self) -> None:
        client = FakeCognito({"a@example.com": "cust-a"})
        with self.assertRaises(AuthorizationDenied):
            _service(client).list_users(USER)
        self.assertEqual(client.calls, [])


class AssignProfileTest(unittest.TestCase):
    def test_a_profile_is_assigned_within_the_callers_customer(self) -> None:
        client = FakeCognito({"a@example.com": "cust-a"})
        result = _service(client).assign_profile(
            ADMIN, email="a@example.com", policy_profile_id="profile-1"
        )
        self.assertEqual(client.profiles["a@example.com"], "profile-1")
        self.assertEqual(result, {"email": "a@example.com", "profile": "profile-1"})

    def test_another_customers_user_cannot_be_reassigned(self) -> None:
        """The regression this pins: a write aimed at a foreign account by address alone."""
        client = FakeCognito({"victim@example.com": "cust-b"})
        with self.assertRaises(AuthorizationDenied):
            _service(client).assign_profile(
                ADMIN, email="victim@example.com", policy_profile_id="profile-1"
            )
        self.assertEqual(client.profiles, {})
        self.assertNotIn("admin_update_user_attributes", [method for method, _ in client.calls])

    def test_an_unknown_user_fails_the_same_way_as_a_foreign_one(self) -> None:
        """Same denial for both, so the endpoint is not a cross-tenant existence oracle."""
        client = FakeCognito({"victim@example.com": "cust-b"})
        foreign = self._denial(client, "victim@example.com")
        missing = self._denial(client, "nobody@example.com")
        self.assertEqual(str(foreign), str(missing))

    @staticmethod
    def _denial(client: FakeCognito, email: str) -> BaseException:
        try:
            _service(client).assign_profile(ADMIN, email=email, policy_profile_id="profile-1")
        except AuthorizationDenied as error:
            return error
        raise AssertionError("expected AuthorizationDenied")

    def test_an_infrastructure_failure_is_not_reported_as_a_denial(self) -> None:
        """A missing IAM grant must surface as a server fault, not as the boundary refusing.

        Reporting it as 403 would make a broken deployment indistinguishable from a working one,
        and every legitimate assignment would fail with an answer that says the caller is at fault.
        """
        client = FakeCognito({"a@example.com": "cust-a"})
        client.get_user_error = ClientError("AccessDeniedException")
        with self.assertRaises(ClientError):
            _service(client).assign_profile(
                ADMIN, email="a@example.com", policy_profile_id="profile-1"
            )
        self.assertEqual(client.profiles, {})

    def test_a_non_admin_cannot_assign_a_profile(self) -> None:
        client = FakeCognito({"a@example.com": "cust-a"})
        with self.assertRaises(AuthorizationDenied):
            _service(client).assign_profile(
                USER, email="a@example.com", policy_profile_id="profile-1"
            )
        self.assertEqual(client.calls, [])

    def test_an_invalid_profile_id_is_a_client_error(self) -> None:
        client = FakeCognito({"a@example.com": "cust-a"})
        with self.assertRaises(UserManagementError):
            _service(client).assign_profile(ADMIN, email="a@example.com", policy_profile_id="  ")

    def test_an_invalid_email_is_a_client_error(self) -> None:
        client = FakeCognito({"a@example.com": "cust-a"})
        with self.assertRaises(UserManagementError):
            _service(client).assign_profile(
                ADMIN, email="not-an-email", policy_profile_id="profile-1"
            )


class ServiceConstructionTest(unittest.TestCase):
    def test_a_blank_pool_id_is_refused(self) -> None:
        with self.assertRaises(TypeError):
            UserManagementService(client=FakeCognito(), user_pool_id="  ")

    def test_a_principal_is_required(self) -> None:
        with self.assertRaises(TypeError):
            _service(FakeCognito()).list_users("admin")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
