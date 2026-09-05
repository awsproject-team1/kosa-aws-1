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
        self.add_group_error: Exception | None = None
        self.set_password_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.deleted: list[str] = []

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
        if self.add_group_error is not None:
            raise self.add_group_error
        self.groups[kwargs["Username"]] = kwargs["GroupName"]
        return {}

    def admin_set_user_password(self, **kwargs):
        self.calls.append(("admin_set_user_password", kwargs))
        if self.set_password_error is not None:
            raise self.set_password_error
        self.passwords[kwargs["Username"]] = (kwargs["Password"], kwargs["Permanent"])
        return {}

    def admin_update_user_attributes(self, **kwargs):
        self.calls.append(("admin_update_user_attributes", kwargs))
        attributes = {a["Name"]: a["Value"] for a in kwargs["UserAttributes"]}
        self.profiles[kwargs["Username"]] = attributes["profile"]
        return {}

    def admin_delete_user(self, **kwargs):
        self.calls.append(("admin_delete_user", kwargs))
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(kwargs["Username"])
        self.users.pop(kwargs["Username"], None)
        return {}

    def admin_get_user(self, **kwargs):
        self.calls.append(("admin_get_user", kwargs))
        if self.get_user_error is not None:
            raise self.get_user_error
        email = kwargs["Username"]
        if email not in self.users:
            raise ClientError("UserNotFoundException")
        attributes = [
            {"Name": "email", "Value": email},
            {"Name": "custom:customer_id", "Value": self.users[email]},
        ]
        if email in self.profiles:
            attributes.append({"Name": "profile", "Value": self.profiles[email]})
        return {"Username": email, "UserAttributes": attributes}

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
            CreateUserRequest(
                email="new@example.com", role="User", temporary_password="Tmp!2026ok"
            ),
        )
        self.assertEqual(client.users["new@example.com"], "cust-a")
        self.assertEqual(result["customer_id"], "cust-a")

    def test_the_caller_cannot_choose_another_customers_partition(self) -> None:
        """The request carries no customer field; the principal is the only source."""
        client = FakeCognito()
        _service(client).create_user(
            ADMIN,
            CreateUserRequest(
                email="new@example.com", role="User", temporary_password="Tmp!2026ok"
            ),
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
                    email="victim@example.com", role="Admin", temporary_password="Tmp!2026ok"
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
                    email="new@example.com", role="User", temporary_password="Tmp!2026ok"
                ),
            )
        self.assertEqual(client.calls, [])

    def test_the_request_rejects_an_unknown_role(self) -> None:
        with self.assertRaises(UserManagementError):
            CreateUserRequest(
                email="new@example.com", role="Superuser", temporary_password="Tmp!2026ok"
            )

    def test_the_request_rejects_a_short_password(self) -> None:
        with self.assertRaises(UserManagementError):
            CreateUserRequest(email="new@example.com", role="User", temporary_password="short")

    def test_no_response_field_carries_the_password(self) -> None:
        client = FakeCognito()
        result = _service(client).create_user(
            ADMIN,
            CreateUserRequest(
                email="new@example.com", role="User", temporary_password="Tmp!2026ok"
            ),
        )
        self.assertNotIn("Tmp!2026ok", str(result))


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
        self.assertEqual(
            result, {"email": "a@example.com", "profile": "profile-1", "profiles": ["profile-1"]}
        )

    def test_a_second_profile_is_added_without_dropping_the_first(self) -> None:
        """사내 정책 Profile과 ISMS-P 기준선 Profile을 따로 평가해 보려면 둘 다 가져야 한다."""
        client = FakeCognito({"a@example.com": "cust-a"})
        client.profiles["a@example.com"] = "profile-1"

        result = _service(client).assign_profile(
            ADMIN, email="a@example.com", policy_profile_id="profile-2"
        )

        self.assertEqual(client.profiles["a@example.com"], "profile-1,profile-2")
        self.assertEqual(result["profiles"], ["profile-1", "profile-2"])

    def test_assigning_an_already_assigned_profile_does_not_repeat_it(self) -> None:
        client = FakeCognito({"a@example.com": "cust-a"})
        client.profiles["a@example.com"] = "profile-1,profile-2"

        result = _service(client).assign_profile(
            ADMIN, email="a@example.com", policy_profile_id="profile-1"
        )

        self.assertEqual(result["profiles"], ["profile-1", "profile-2"])

    def test_a_profile_can_be_removed_leaving_the_others(self) -> None:
        client = FakeCognito({"a@example.com": "cust-a"})
        client.profiles["a@example.com"] = "profile-1,profile-2"

        result = _service(client).assign_profile(
            ADMIN, email="a@example.com", policy_profile_id="profile-1", action="remove"
        )

        self.assertEqual(client.profiles["a@example.com"], "profile-2")
        self.assertEqual(result["profiles"], ["profile-2"])

    def test_set_replaces_the_whole_list(self) -> None:
        client = FakeCognito({"a@example.com": "cust-a"})
        client.profiles["a@example.com"] = "profile-1,profile-2"

        result = _service(client).assign_profile(
            ADMIN, email="a@example.com", policy_profile_id="profile-3", action="set"
        )

        self.assertEqual(result["profiles"], ["profile-3"])

    def test_an_unknown_action_and_a_comma_are_client_errors(self) -> None:
        client = FakeCognito({"a@example.com": "cust-a"})
        with self.assertRaises(UserManagementError):
            _service(client).assign_profile(
                ADMIN, email="a@example.com", policy_profile_id="p", action="replace"
            )
        with self.assertRaises(UserManagementError):
            _service(client).assign_profile(ADMIN, email="a@example.com", policy_profile_id="a,b")
        self.assertEqual(client.profiles, {})

    def test_the_listing_splits_the_stored_profiles(self) -> None:
        client = FakeCognito({"a@example.com": "cust-a"})
        client.pages = [
            {
                "Users": [
                    {
                        "Username": "a@example.com",
                        "UserStatus": "CONFIRMED",
                        "Enabled": True,
                        "Attributes": [
                            {"Name": "email", "Value": "a@example.com"},
                            {"Name": "custom:customer_id", "Value": "cust-a"},
                            {"Name": "profile", "Value": "profile-1, profile-2,profile-1"},
                        ],
                    }
                ]
            }
        ]

        (user,) = _service(client).list_users(ADMIN)

        self.assertEqual(user["profiles"], ["profile-1", "profile-2"])

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


class DeleteUserTest(unittest.TestCase):
    def test_a_user_is_deleted_within_the_callers_customer(self) -> None:
        client = FakeCognito({"a@example.com": "cust-a"})
        result = _service(client).delete_user(ADMIN, email="a@example.com")
        self.assertNotIn("a@example.com", client.users)
        self.assertEqual(result, {"email": "a@example.com", "deleted": True})

    def test_another_customers_user_cannot_be_deleted(self) -> None:
        """The regression this pins: a delete aimed at a foreign account by address alone."""
        client = FakeCognito({"victim@example.com": "cust-b"})
        with self.assertRaises(AuthorizationDenied):
            _service(client).delete_user(ADMIN, email="victim@example.com")
        self.assertIn("victim@example.com", client.users)
        self.assertNotIn("admin_delete_user", [method for method, _ in client.calls])

    def test_an_unknown_user_fails_the_same_way_as_a_foreign_one(self) -> None:
        """Same denial for both, so the endpoint is not a cross-tenant existence oracle."""
        client = FakeCognito({"victim@example.com": "cust-b"})
        foreign = self._denial(client, "victim@example.com")
        missing = self._denial(client, "nobody@example.com")
        self.assertEqual(str(foreign), str(missing))

    @staticmethod
    def _denial(client: FakeCognito, email: str) -> BaseException:
        try:
            _service(client).delete_user(ADMIN, email=email)
        except AuthorizationDenied as error:
            return error
        raise AssertionError("expected AuthorizationDenied")

    def test_an_infrastructure_failure_is_not_reported_as_a_denial(self) -> None:
        """A missing IAM grant on the boundary read must surface as a server fault, not a 403."""
        client = FakeCognito({"a@example.com": "cust-a"})
        client.get_user_error = ClientError("AccessDeniedException")
        with self.assertRaises(ClientError):
            _service(client).delete_user(ADMIN, email="a@example.com")
        self.assertIn("a@example.com", client.users)
        self.assertNotIn("admin_delete_user", [method for method, _ in client.calls])

    def test_a_non_admin_cannot_delete_a_user(self) -> None:
        client = FakeCognito({"a@example.com": "cust-a"})
        with self.assertRaises(AuthorizationDenied):
            _service(client).delete_user(USER, email="a@example.com")
        self.assertEqual(client.calls, [])

    def test_an_invalid_email_is_a_client_error(self) -> None:
        client = FakeCognito({"a@example.com": "cust-a"})
        with self.assertRaises(UserManagementError):
            _service(client).delete_user(ADMIN, email="not-an-email")


class ServiceConstructionTest(unittest.TestCase):
    def test_a_blank_pool_id_is_refused(self) -> None:
        with self.assertRaises(TypeError):
            UserManagementService(client=FakeCognito(), user_pool_id="  ")

    def test_a_principal_is_required(self) -> None:
        with self.assertRaises(TypeError):
            _service(FakeCognito()).list_users("admin")  # type: ignore[arg-type]


class InitialPasswordPolicyTest(unittest.TestCase):
    """The pool rejects a weak password on the *last* of three Cognito writes.

    Checked up front, a weak password is a 400 and nothing is written. Left to Cognito, the user
    and the group already exist by the time it says no — a real account with no usable password,
    which the admin then cannot re-create ("already exists") and the user cannot sign in to.
    """

    def test_a_policy_compliant_password_is_accepted(self) -> None:
        CreateUserRequest(email="a@example.com", role="User", temporary_password="Tmp!2026ok")

    def test_each_missing_class_is_named_without_echoing_the_password(self) -> None:
        for password, missing in (
            ("tmp!2026ok", "uppercase"),
            ("TMP!2026OK", "lowercase"),
            ("Tmp!okokok", "number"),
            ("Tmp2026okok", "symbol"),
            ("T!2o", "at least 8 characters"),
        ):
            with self.subTest(missing=missing):
                with self.assertRaises(UserManagementError) as raised:
                    CreateUserRequest(
                        email="a@example.com", role="User", temporary_password=password
                    )
                self.assertIn(missing, str(raised.exception))
                self.assertNotIn(password, str(raised.exception))

    def test_the_backend_rule_matches_cognito_defaults(self) -> None:
        from apps.backend.api.users import PASSWORD_MIN_LENGTH, PASSWORD_REQUIRED_CLASSES

        self.assertEqual(PASSWORD_MIN_LENGTH, 8)
        self.assertEqual(
            set(PASSWORD_REQUIRED_CLASSES), {"uppercase", "lowercase", "number", "symbol"}
        )


class EmailNormalizationTest(unittest.TestCase):
    """Usernames in this pool are case-sensitive; an address must have one spelling everywhere."""

    def test_the_address_is_trimmed_and_lower_cased_on_create(self) -> None:
        client = FakeCognito()
        result = _service(client).create_user(
            ADMIN,
            CreateUserRequest(
                email="  Jin.Test@Example.COM ", role="User", temporary_password="Tmp!2026ok"
            ),
        )
        self.assertEqual(result["email"], "jin.test@example.com")
        self.assertIn("jin.test@example.com", client.users)
        for _method, kwargs in client.calls:
            self.assertEqual(kwargs.get("Username"), "jin.test@example.com")

    def test_profile_assignment_finds_the_user_regardless_of_typed_case(self) -> None:
        client = FakeCognito({"a@example.com": "cust-a"})
        _service(client).assign_profile(ADMIN, email="A@Example.com", policy_profile_id="p-1")
        self.assertEqual(client.profiles["a@example.com"], "p-1")


class HalfCreatedUserRollbackTest(unittest.TestCase):
    """If the group or password write fails, the account the first write made is removed."""

    def _create(self, client: FakeCognito):
        return _service(client).create_user(
            ADMIN,
            CreateUserRequest(
                email="new@example.com", role="User", temporary_password="Tmp!2026ok"
            ),
        )

    def test_a_password_rejected_by_the_pool_removes_the_user_and_is_a_client_error(self) -> None:
        client = FakeCognito()
        client.set_password_error = ClientError("InvalidPasswordException")
        with self.assertRaises(UserManagementError):
            self._create(client)
        self.assertEqual(client.deleted, ["new@example.com"])
        self.assertNotIn("new@example.com", client.users)

    def test_a_group_failure_removes_the_user_and_surfaces_the_real_error(self) -> None:
        client = FakeCognito()
        client.add_group_error = ClientError("ResourceNotFoundException")
        with self.assertRaises(ClientError):
            self._create(client)
        self.assertEqual(client.deleted, ["new@example.com"])
        self.assertNotIn("admin_set_user_password", [m for m, _ in client.calls])

    def test_a_failed_rollback_does_not_hide_the_original_failure(self) -> None:
        client = FakeCognito()
        client.set_password_error = ClientError("InvalidPasswordException")
        client.delete_error = ClientError("AccessDeniedException")
        with self.assertRaises(UserManagementError):
            self._create(client)

    def test_a_clean_create_deletes_nothing(self) -> None:
        client = FakeCognito()
        self._create(client)
        self.assertEqual(client.deleted, [])


if __name__ == "__main__":
    unittest.main()
