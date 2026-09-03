"""API composition root의 배포 commit 해석 배선 테스트 (ADR-0019 §3·§4).

고정하는 불변식:
- GitHub 설정이 없으면 `TERRAFORM_PATCH` 경로는 base commit으로 대체되지 않고 fail-closed한다.
- 설정이 있으면 요청한 (customer, repository)의 승인 target으로만 해석한다.
- 승인 목록 밖 target은 다른 target의 token으로 해석되지 않는다.
"""

import json
import os
import unittest
from unittest import mock

from apps.backend.api.runtime import (
    ConfiguredDeploymentCommitResolver,
    UnconfiguredDeploymentCommitResolver,
    _deployment_commit_resolver,
)
from apps.backend.deployment.runtime_config import (
    DeploymentRuntimeConfiguration,
    DeploymentRuntimeConfigurationError,
)
from packages.contracts import ArtifactReference, ArtifactType, RemediationPatch

CUSTOMER_ID = "cust-001"
REPOSITORY_ID = "repo-001"

TARGET = {
    "customer_id": CUSTOMER_ID,
    "repository_id": REPOSITORY_ID,
    "repository_full_name": "customer/iac",
    "github_token_secret_id": "github-token",
    "aws_account_id": "123456789012",
    "aws_read_role_arn": "arn:aws:iam::123456789012:role/Read",
    "aws_external_id_secret_id": "external-id",
    "resource_types": ["AWS::S3::Bucket"],
}


def _patch() -> RemediationPatch:
    return RemediationPatch(
        finding_id="find-001",
        base_commit_sha="a" * 40,
        artifact=ArtifactReference(
            artifact_id="patch-1",
            artifact_type=ArtifactType.REMEDIATION_PATCH,
            content_sha256="c" * 64,
            customer_id=CUSTOMER_ID,
            repository_id=REPOSITORY_ID,
        ),
        changed_paths=("main.tf",),
    )


class DeploymentCommitResolverWiringTest(unittest.TestCase):
    def test_missing_configuration_selects_the_fail_closed_resolver(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            resolver = _deployment_commit_resolver()
        self.assertIsInstance(resolver, UnconfiguredDeploymentCommitResolver)
        with self.assertRaises(DeploymentRuntimeConfigurationError):
            resolver.resolve_default_branch_commit(
                customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID, patch=_patch()
            )

    def test_blank_configuration_is_treated_as_missing(self) -> None:
        with mock.patch.dict(os.environ, {"DEPLOYMENT_RUNTIME_JSON": "   "}, clear=True):
            self.assertIsInstance(
                _deployment_commit_resolver(), UnconfiguredDeploymentCommitResolver
            )

    def test_configuration_selects_the_live_resolver(self) -> None:
        with mock.patch.dict(
            os.environ, {"DEPLOYMENT_RUNTIME_JSON": json.dumps([TARGET])}, clear=True
        ):
            self.assertIsInstance(_deployment_commit_resolver(), ConfiguredDeploymentCommitResolver)

    def test_an_unapproved_target_is_refused_not_resolved(self) -> None:
        resolver = ConfiguredDeploymentCommitResolver(
            DeploymentRuntimeConfiguration.from_json(json.dumps([TARGET]))
        )
        with self.assertRaises(DeploymentRuntimeConfigurationError):
            resolver.resolve_default_branch_commit(
                customer_id="cust-002", repository_id=REPOSITORY_ID, patch=_patch()
            )


if __name__ == "__main__":
    unittest.main()
