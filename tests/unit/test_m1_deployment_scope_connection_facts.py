"""The deployment gate carries the console's connection facts without widening the scope.

`GET /scope` shows the operator which GitHub repository and AWS account the platform is wired to,
so they can confirm a live assessment targets the real customer before approving anything. Those
facts live in `ASSESSMENT_SCOPE_JSON`, which only the deploy workflow can set — so a gate that
rejects them leaves no way to deploy them, and the next redeploy silently erases whatever was put
into the live Lambda by hand. These tests pin the two properties that make carrying them safe:
the fields never widen what may be assessed, and they cannot disagree with the M1 runtime target.
"""

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import path bootstrap
    sys.path.insert(0, str(REPO_ROOT))

from scripts.validate_m1_deployment_config import (  # noqa: E402
    DeploymentConfigurationError,
    validate_environment,
)

ACCOUNT = "123456789012"
OTHER_ACCOUNT = "210987654321"
REGION = "us-east-1"
GITHUB_SECRET = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:m1/github-token-AbCdEf"
EXTERNAL_ID_SECRET = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:m1/external-id-AbCdEf"
READ_ROLE = f"arn:aws:iam::{ACCOUNT}:role/GovernanceRead"
REPOSITORY = "customer/iac"

TARGET = {
    "customer_id": "cust-001",
    "repository_id": "repo-001",
    "commit_sha": "a" * 40,
    "github_repository": REPOSITORY,
    "github_token_secret_id": GITHUB_SECRET,
    "aws_account_id": ACCOUNT,
    "aws_read_role_arn": READ_ROLE,
    "aws_external_id_secret_id": EXTERNAL_ID_SECRET,
    "s3_bucket_id": "demo-bucket",
}


def _environment(scope_entry: dict, *, target: dict | None = None) -> dict[str, str]:
    return {
        "M1_ASSESSMENT_MODE": "live",
        "EXPECTED_AWS_ACCOUNT_ID": ACCOUNT,
        "AWS_REGION": REGION,
        "ASSESSMENT_SCOPE_JSON": json.dumps({"cust-001": [scope_entry]}),
        "M1_ASSESSMENT_RUNTIME_JSON": json.dumps([target or TARGET]),
        "M1_ASSESSMENT_SECRET_ARNS": f"{GITHUB_SECRET},{EXTERNAL_ID_SECRET}",
        "M1_ASSESSMENT_READ_ROLE_ARNS": READ_ROLE,
    }


class ScopeConnectionFactsTest(unittest.TestCase):
    def test_the_selector_alone_is_still_accepted(self) -> None:
        """The facts are optional; a scope that omits them deploys exactly as before."""
        environment = _environment({"repository_id": "repo-001"})
        self.assertEqual(validate_environment(environment), "live")

    def test_the_console_connection_facts_are_accepted(self) -> None:
        """Without this the deploy workflow has no way to set what `GET /scope` returns."""
        environment = _environment(
            {
                "repository_id": "repo-001",
                "github_repository": REPOSITORY,
                "aws_account_id": ACCOUNT,
            }
        )
        self.assertEqual(validate_environment(environment), "live")

    def test_either_fact_may_be_declared_on_its_own(self) -> None:
        for field_name, value in (
            ("github_repository", REPOSITORY),
            ("aws_account_id", ACCOUNT),
        ):
            with self.subTest(field=field_name):
                environment = _environment({"repository_id": "repo-001", field_name: value})
                self.assertEqual(validate_environment(environment), "live")


class ScopeStaysFailClosedTest(unittest.TestCase):
    """Widening the allow-list must not become a hole for anything else."""

    def test_a_secret_reference_in_the_scope_is_refused(self) -> None:
        """`ASSESSMENT_SCOPE_JSON` reaches the API Lambda as a plain env var and `GET /scope`
        reads it. A secret ARN parked here would be one field rename away from being returned."""
        environment = _environment(
            {
                "repository_id": "repo-001",
                "aws_read_role_arn": READ_ROLE,
            }
        )
        with self.assertRaises(DeploymentConfigurationError):
            validate_environment(environment)

    def test_policy_profile_id_is_still_refused(self) -> None:
        """ADR-0023 moved the Profile to the customer Catalog; the old field must stay rejected."""
        environment = _environment(
            {"repository_id": "repo-001", "policy_profile_id": "profile-mvp-baseline"}
        )
        with self.assertRaises(DeploymentConfigurationError):
            validate_environment(environment)

    def test_a_scope_entry_without_a_repository_id_is_refused(self) -> None:
        environment = _environment({"github_repository": REPOSITORY})
        with self.assertRaises(DeploymentConfigurationError):
            validate_environment(environment)

    def test_a_malformed_repository_name_is_refused(self) -> None:
        environment = _environment(
            {"repository_id": "repo-001", "github_repository": "not-a-full-name"}
        )
        with self.assertRaises(DeploymentConfigurationError):
            validate_environment(environment)

    def test_a_malformed_account_id_is_refused(self) -> None:
        environment = _environment({"repository_id": "repo-001", "aws_account_id": "12345"})
        with self.assertRaises(DeploymentConfigurationError):
            validate_environment(environment)


class ScopeMatchesTheRuntimeTargetTest(unittest.TestCase):
    """A screen that names a different target than the Worker evaluates is worse than no screen.

    The operator reads "connected to customer/iac" and approves on that basis; the assessment runs
    against whatever the M1 runtime target says. These two must not be able to disagree.
    """

    def test_a_repository_that_disagrees_with_the_target_is_refused(self) -> None:
        environment = _environment(
            {"repository_id": "repo-001", "github_repository": "someone-else/iac"}
        )
        with self.assertRaises(DeploymentConfigurationError) as raised:
            validate_environment(environment)
        self.assertIn("github_repository", str(raised.exception))

    def test_an_account_that_disagrees_with_the_target_is_refused(self) -> None:
        environment = _environment({"repository_id": "repo-001", "aws_account_id": OTHER_ACCOUNT})
        with self.assertRaises(DeploymentConfigurationError) as raised:
            validate_environment(environment)
        self.assertIn("aws_account_id", str(raised.exception))

    def test_the_selector_sets_must_still_match(self) -> None:
        """The facts do not smuggle in a selector the runtime has no target for."""
        environment = _environment(
            {
                "repository_id": "repo-002",
                "github_repository": REPOSITORY,
                "aws_account_id": ACCOUNT,
            }
        )
        with self.assertRaises(DeploymentConfigurationError) as raised:
            validate_environment(environment)
        self.assertIn("selector sets must match", str(raised.exception))


class FixtureModeTest(unittest.TestCase):
    def test_fixture_mode_shape_checks_the_facts_without_a_target(self) -> None:
        """There is no runtime target to cross-check against, so the shape check is all there is."""
        environment = {
            "M1_ASSESSMENT_MODE": "fixture",
            "EXPECTED_AWS_ACCOUNT_ID": ACCOUNT,
            "AWS_REGION": REGION,
            "ASSESSMENT_SCOPE_JSON": json.dumps(
                {
                    "cust-001": [
                        {
                            "repository_id": "repo-001",
                            "github_repository": REPOSITORY,
                            "aws_account_id": ACCOUNT,
                        }
                    ]
                }
            ),
        }
        self.assertEqual(validate_environment(environment), "fixture")

        environment["ASSESSMENT_SCOPE_JSON"] = json.dumps(
            {"cust-001": [{"repository_id": "repo-001", "aws_account_id": "12345"}]}
        )
        with self.assertRaises(DeploymentConfigurationError):
            validate_environment(environment)


class RuntimeAllowListParityTest(unittest.TestCase):
    """The gate and the runtime must accept the same scope fields.

    They are two different parsers over one env var. If the gate is narrower, a value the runtime
    would happily serve cannot be deployed; if it is wider, a deploy succeeds and the API Lambda
    fails closed at cold start. Either way the mismatch shows up only in a live deployment.
    """

    def test_the_gate_allows_exactly_what_the_runtime_allows(self) -> None:
        from apps.backend.api import runtime as api_runtime
        from scripts.validate_m1_deployment_config import SCOPE_DISPLAY_FIELDS, SCOPE_FIELDS

        selector = {"repository_id": "repo-001"}
        for field_name in SCOPE_DISPLAY_FIELDS:
            selector[field_name] = REPOSITORY if "repository" in field_name else ACCOUNT
        # 런타임 파서가 같은 항목을 받아들이고 repository_id를 그대로 뽑아내는지 확인한다.
        self.assertEqual(
            api_runtime._repository_ids([selector]),
            frozenset({"repo-001"}),
        )
        for field_name in sorted(SCOPE_FIELDS | SCOPE_DISPLAY_FIELDS):
            with self.subTest(field=field_name):
                self.assertIn(field_name, selector)


if __name__ == "__main__":
    unittest.main()
