"""Security invariants for the approval-gated frontend publishing workflow."""

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-frontend.yml"


class FrontendDeployWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.workflow = yaml.load(cls.text, Loader=yaml.BaseLoader)

    def test_only_the_approval_gated_job_receives_aws_identity(self) -> None:
        jobs = self.workflow["jobs"]
        self.assertNotIn("id-token", jobs["prepare"].get("permissions", {}))
        self.assertEqual(jobs["deploy"]["permissions"]["id-token"], "write")
        self.assertEqual(jobs["deploy"]["environment"], "${{ inputs.environment }}")

    def test_aws_credentials_are_oidc_only(self) -> None:
        self.assertIn("aws-actions/configure-aws-credentials@", self.text)
        for forbidden in (
            "aws-access-key-id",
            "aws-secret-access-key",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.text)

    def test_deployment_actions_are_pinned_to_commits(self) -> None:
        uses = re.findall(r"uses:\s+([^\s]+)", self.text)
        self.assertGreaterEqual(len(uses), 4)
        for action in uses:
            with self.subTest(action=action):
                self.assertRegex(action, r"@[0-9a-f]{40}$")

    def test_destructive_sync_is_guarded_by_the_exact_derived_bucket(self) -> None:
        validation = (
            'test "${SPA_BUCKET_NAME}" = '
            '"${PROJECT_NAME}-${STACK_ENVIRONMENT}-frontend-${EXPECTED_AWS_ACCOUNT_ID}"'
        )
        self.assertIn(validation, self.text)
        self.assertIn("--delete", self.text)
        self.assertLess(self.text.index(validation), self.text.index("--delete"))

    def test_artifact_and_published_index_are_hash_verified(self) -> None:
        self.assertGreaterEqual(self.text.count('= "${ARTIFACT_SHA256}"'), 1)
        self.assertGreaterEqual(self.text.count('= "${INDEX_SHA256}"'), 2)
        self.assertIn("aws cloudfront wait invalidation-completed", self.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
