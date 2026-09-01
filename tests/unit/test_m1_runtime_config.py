"""M1 live targets are deployment-defined and fail closed outside scope."""

import json
import unittest

from apps.backend.assessment.runtime import DynamoM1WorkRepository
from apps.backend.assessment.runtime_config import (
    M1RuntimeConfiguration,
    M1RuntimeConfigurationError,
)

TARGET = {
    "customer_id": "cust-001",
    "repository_id": "repo-001",
    "policy_profile_id": "profile-mvp-baseline",
    "commit_sha": "a" * 40,
    "github_repository": "customer/iac",
    "github_token_secret_id": "github-token",
    "aws_account_id": "123456789012",
    "aws_read_role_arn": "arn:aws:iam::123456789012:role/Read",
    "aws_external_id_secret_id": "external-id",
    "s3_bucket_id": "customer-test-bucket",
}


class M1RuntimeConfigurationTest(unittest.TestCase):
    def test_resolves_only_an_exact_approved_scope(self) -> None:
        configuration = M1RuntimeConfiguration.from_json(json.dumps([TARGET]))
        target = configuration.resolve(
            customer_id="cust-001",
            repository_id="repo-001",
            policy_profile_id="profile-mvp-baseline",
        )
        self.assertEqual(target.commit_sha, "a" * 40)
        with self.assertRaisesRegex(M1RuntimeConfigurationError, "outside M1 runtime scope"):
            configuration.resolve(
                customer_id="cust-001",
                repository_id="repo-other",
                policy_profile_id="profile-mvp-baseline",
            )

    def test_rejects_missing_or_extra_configuration_fields(self) -> None:
        with self.assertRaisesRegex(M1RuntimeConfigurationError, "required"):
            M1RuntimeConfiguration.from_json("")
        invalid = dict(TARGET)
        invalid["unexpected"] = "value"
        with self.assertRaisesRegex(M1RuntimeConfigurationError, "invalid"):
            M1RuntimeConfiguration.from_json(json.dumps([invalid]))

    def test_worker_repository_resolves_only_persisted_assessment_selectors(self) -> None:
        class Table:
            def query(self, **kwargs: object) -> dict[str, object]:
                return {
                    "Items": [
                        {
                            "customer_id": "cust-001",
                            "assessment_id": "asm-001",
                            "revision": 0,
                        }
                    ]
                }

            def get_item(self, **kwargs: object) -> dict[str, object]:
                return {
                    "Item": {
                        "repository_id": "repo-001",
                        "policy_profile_id": "profile-mvp-baseline",
                    }
                }

        repository = DynamoM1WorkRepository(
            Table(), M1RuntimeConfiguration.from_json(json.dumps([TARGET]))
        )
        work = repository.get_resource_work(job_id="job-001", expected_revision=0)

        self.assertIsNotNone(work)
        assert work is not None
        self.assertEqual(work.resource_id, "customer-test-bucket")
        self.assertEqual(work.perspective.value, "AWS_ACTUAL")
