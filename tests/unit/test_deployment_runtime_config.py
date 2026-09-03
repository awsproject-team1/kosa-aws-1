"""Deployment Worker의 live target은 배포 configuration으로 정의되고 scope 밖은 fail-closed다."""

import json
import unittest

from apps.backend.deployment.runtime_config import (
    DeploymentRuntimeConfiguration,
    DeploymentRuntimeConfigurationError,
)

TARGET = {
    "customer_id": "cust-001",
    "repository_id": "repo-001",
    "repository_full_name": "customer/iac",
    "github_token_secret_id": "github-token",
    "aws_account_id": "123456789012",
    "aws_read_role_arn": "arn:aws:iam::123456789012:role/Read",
    "aws_external_id_secret_id": "external-id",
    "resource_types": ["AWS::S3::Bucket"],
}


class DeploymentRuntimeConfigurationTest(unittest.TestCase):
    def test_resolves_only_an_exact_approved_scope(self) -> None:
        configuration = DeploymentRuntimeConfiguration.from_json(json.dumps([TARGET]))
        target = configuration.resolve(customer_id="cust-001", repository_id="repo-001")
        self.assertEqual(target.aws_account_id, "123456789012")
        self.assertEqual(target.resource_types, ("AWS::S3::Bucket",))
        with self.assertRaisesRegex(DeploymentRuntimeConfigurationError, "outside runtime scope"):
            configuration.resolve(customer_id="cust-001", repository_id="repo-other")

    def test_aws_account_id_for_is_a_resolver_adapter(self) -> None:
        configuration = DeploymentRuntimeConfiguration.from_json(json.dumps([TARGET]))
        self.assertEqual(configuration.aws_account_id_for("cust-001", "repo-001"), "123456789012")

    def test_rejects_absent_configuration(self) -> None:
        with self.assertRaisesRegex(DeploymentRuntimeConfigurationError, "required"):
            DeploymentRuntimeConfiguration.from_json("")

    def test_rejects_missing_or_extra_fields(self) -> None:
        incomplete = {k: v for k, v in TARGET.items() if k != "aws_account_id"}
        with self.assertRaises(DeploymentRuntimeConfigurationError):
            DeploymentRuntimeConfiguration.from_json(json.dumps([incomplete]))

    def test_rejects_empty_resource_types(self) -> None:
        empty = {**TARGET, "resource_types": []}
        with self.assertRaises(DeploymentRuntimeConfigurationError):
            DeploymentRuntimeConfiguration.from_json(json.dumps([empty]))

    def test_rejects_overlapping_secret_roles(self) -> None:
        overlapping = {**TARGET, "aws_external_id_secret_id": "github-token"}
        with self.assertRaises(DeploymentRuntimeConfigurationError):
            DeploymentRuntimeConfiguration.from_json(json.dumps([overlapping]))

    def test_rejects_duplicate_target_scope(self) -> None:
        with self.assertRaises(DeploymentRuntimeConfigurationError):
            DeploymentRuntimeConfiguration.from_json(json.dumps([TARGET, TARGET]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
