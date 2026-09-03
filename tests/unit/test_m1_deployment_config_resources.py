"""The protected M1 deployment gate accepts the resource list and fails closed on its edges."""

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
REGION = "us-east-1"
GITHUB_SECRET = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:m1/github-token-AbCdEf"
EXTERNAL_ID_SECRET = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:m1/external-id-AbCdEf"
READ_ROLE = f"arn:aws:iam::{ACCOUNT}:role/GovernanceRead"

COMMON = {
    "customer_id": "cust-001",
    "repository_id": "repo-001",
    "commit_sha": "a" * 40,
    "github_repository": "customer/iac",
    "github_token_secret_id": GITHUB_SECRET,
    "aws_account_id": ACCOUNT,
    "aws_read_role_arn": READ_ROLE,
    "aws_external_id_secret_id": EXTERNAL_ID_SECRET,
}


def _environment(target: dict) -> dict[str, str]:
    return {
        "M1_ASSESSMENT_MODE": "live",
        "EXPECTED_AWS_ACCOUNT_ID": ACCOUNT,
        "AWS_REGION": REGION,
        # Assessment scope는 Repository 경계만 선언한다. Profile은 고객 Catalog가 정한다.
        "ASSESSMENT_SCOPE_JSON": json.dumps({"cust-001": [{"repository_id": "repo-001"}]}),
        "M1_ASSESSMENT_RUNTIME_JSON": json.dumps([target]),
        "M1_ASSESSMENT_SECRET_ARNS": f"{GITHUB_SECRET},{EXTERNAL_ID_SECRET}",
        "M1_ASSESSMENT_READ_ROLE_ARNS": READ_ROLE,
    }


class M1DeploymentResourceConfigTest(unittest.TestCase):
    def test_accepts_an_explicit_multi_type_resource_list(self) -> None:
        target = {
            **COMMON,
            "resources": [
                {"resource_type": "AWS::S3::Bucket", "resource_id": "demo-bucket"},
                {"resource_type": "AWS::EC2::Instance", "resource_id": "i-0123456789abcdef0"},
                {"resource_type": "AWS::RDS::DBInstance", "resource_id": "demo-db-001"},
                {
                    "resource_type": "AWS::ElasticLoadBalancingV2::LoadBalancer",
                    "resource_id": (
                        f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:"
                        "loadbalancer/app/demo/50dc6c495c0c9188"
                    ),
                },
            ],
        }

        self.assertEqual(validate_environment(_environment(target)), "live")

    def test_accepts_the_legacy_single_bucket_target(self) -> None:
        target = {**COMMON, "s3_bucket_id": "demo-bucket"}

        self.assertEqual(validate_environment(_environment(target)), "live")

    def test_rejects_declaring_resources_two_ways_at_once(self) -> None:
        target = {
            **COMMON,
            "s3_bucket_id": "demo-bucket",
            "resources": [{"resource_type": "AWS::S3::Bucket", "resource_id": "demo-bucket"}],
        }

        with self.assertRaisesRegex(DeploymentConfigurationError, "target fields are invalid"):
            validate_environment(_environment(target))

    def test_rejects_a_resource_type_without_a_read_adapter(self) -> None:
        target = {
            **COMMON,
            "resources": [{"resource_type": "AWS::DynamoDB::Table", "resource_id": "table-1"}],
        }

        with self.assertRaisesRegex(DeploymentConfigurationError, "no Actual read adapter"):
            validate_environment(_environment(target))

    def test_rejects_an_empty_or_duplicated_resource_list(self) -> None:
        with self.assertRaisesRegex(DeploymentConfigurationError, "non-empty array"):
            validate_environment(_environment({**COMMON, "resources": []}))

        duplicated = {
            **COMMON,
            "resources": [
                {"resource_type": "AWS::S3::Bucket", "resource_id": "demo-bucket"},
                {"resource_type": "AWS::S3::Bucket", "resource_id": "demo-bucket"},
            ],
        }
        with self.assertRaisesRegex(DeploymentConfigurationError, "must be unique"):
            validate_environment(_environment(duplicated))

    def test_rejects_a_malformed_resource_entry(self) -> None:
        target = {**COMMON, "resources": [{"resource_type": "AWS::S3::Bucket"}]}

        with self.assertRaisesRegex(DeploymentConfigurationError, "resource fields are invalid"):
            validate_environment(_environment(target))


if __name__ == "__main__":
    unittest.main()
