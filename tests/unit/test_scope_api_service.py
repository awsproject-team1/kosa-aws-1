"""ScopeApiService returns only non-secret connection facts for the caller's customer."""

import json
import unittest

from apps.backend.api.scope import ScopeApiService
from apps.backend.auth import Principal, Role

PRINCIPAL = Principal(
    subject="user-001",
    client_id="client-001",
    customer_id="kosa-sandbox",
    roles=frozenset({Role.ADMIN}),
)


class ScopeApiServiceTest(unittest.TestCase):
    def test_returns_only_the_callers_customer_scope(self) -> None:
        service = ScopeApiService(
            scope_json=json.dumps(
                {
                    "kosa-sandbox": [{"repository_id": "test-s3-sandbox"}],
                    "other-customer": [{"repository_id": "not-mine"}],
                }
            )
        )

        result = service.get_scope(PRINCIPAL)

        self.assertEqual(result["customer_id"], "kosa-sandbox")
        self.assertEqual(result["repositories"], [{"repository_id": "test-s3-sandbox"}])

    def test_surfaces_github_and_account_when_present(self) -> None:
        service = ScopeApiService(
            scope_json=json.dumps(
                {
                    "kosa-sandbox": [
                        {
                            "repository_id": "test-s3-sandbox",
                            "github_repository": "awsproject-team1/test",
                            "aws_account_id": "369676914736",
                        }
                    ]
                }
            )
        )

        result = service.get_scope(PRINCIPAL)

        self.assertEqual(
            result["repositories"],
            [
                {
                    "repository_id": "test-s3-sandbox",
                    "github_repository": "awsproject-team1/test",
                    "aws_account_id": "369676914736",
                }
            ],
        )

    def test_never_returns_secret_references(self) -> None:
        service = ScopeApiService(
            scope_json=json.dumps(
                {
                    "kosa-sandbox": [
                        {
                            "repository_id": "test-s3-sandbox",
                            "github_repository": "awsproject-team1/test",
                            "aws_account_id": "369676914736",
                            "aws_read_role_arn": "arn:aws:iam::369676914736:role/secret",
                            "github_token_secret_id": "secret/github-token",
                        }
                    ]
                }
            )
        )

        [repo] = service.get_scope(PRINCIPAL)["repositories"]

        self.assertNotIn("aws_read_role_arn", repo)
        self.assertNotIn("github_token_secret_id", repo)

    def test_malformed_configuration_is_empty_scope(self) -> None:
        service = ScopeApiService(scope_json="{not json")

        self.assertEqual(service.get_scope(PRINCIPAL)["repositories"], [])
