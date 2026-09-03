"""The pull request action reads verified patch bytes and hands them to the writer."""

import unittest

from agent.runtime import MockGitHubWriteTool
from apps.backend.remediation.patch_content import (
    InMemoryPatchContentStore,
    PatchContentError,
    encode_patch_content,
    patch_content_digest,
)
from apps.backend.remediation.pull_request import PatchPullRequestAction, PullRequestActionError
from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    EvaluationPerspective,
    EvaluationStatus,
    Finding,
    IaCSnapshot,
    RemediationContext,
    RemediationPatch,
)

CUSTOMER = "cust-001"
REPOSITORY_ID = "repo-001"
FINDING_ID = "finding-abc"
COMMIT = "a" * 40
CHANGES = {"main.tf": 'resource "aws_s3_bucket_public_access_block" "x" {}\n'}
CONTENT = encode_patch_content(finding_id=FINDING_ID, base_commit_sha=COMMIT, changes=CHANGES)


def _context() -> RemediationContext:
    return RemediationContext(
        finding=Finding(
            finding_id=FINDING_ID,
            resource_id="bucket-001",
            rule_id="S3-PUBLIC-001",
            rule_version="2026-08-31",
            perspective=EvaluationPerspective.IAC,
            status=EvaluationStatus.FAIL,
            severity="CRITICAL",
            score=0,
            rationale="public",
            evidence_references=("terraform:main.tf",),
        ),
        snapshot=IaCSnapshot(
            customer_id=CUSTOMER,
            repository_id=REPOSITORY_ID,
            commit_sha=COMMIT,
            artifact=ArtifactReference(
                artifact_id="snap-1",
                artifact_type=ArtifactType.TERRAFORM_SNAPSHOT,
                content_sha256="b" * 64,
                customer_id=CUSTOMER,
                repository_id=REPOSITORY_ID,
            ),
        ),
        evidence_references=("terraform:main.tf",),
    )


def _patch(digest: str = patch_content_digest(CONTENT), commit: str = COMMIT) -> RemediationPatch:
    return RemediationPatch(
        finding_id=FINDING_ID,
        base_commit_sha=commit,
        artifact=ArtifactReference(
            artifact_id=f"remediation-patch:{REPOSITORY_ID}:{FINDING_ID}:{digest}",
            artifact_type=ArtifactType.REMEDIATION_PATCH,
            content_sha256=digest,
            customer_id=CUSTOMER,
            repository_id=REPOSITORY_ID,
        ),
        changed_paths=("main.tf",),
    )


class PatchPullRequestActionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryPatchContentStore()
        self.store.put(patch=_patch(), content=CONTENT)
        self.writer = MockGitHubWriteTool(customer_id=CUSTOMER, repository_id=REPOSITORY_ID)
        self.action = PatchPullRequestAction(writer=self.writer, content_store=self.store)

    def test_opens_the_pull_request_with_the_stored_changes(self) -> None:
        opened = self.action.open(context=_context(), patch=_patch())
        self.assertEqual(opened.finding_id, FINDING_ID)
        self.assertEqual(self.writer.opened[0][1], CHANGES)

    def test_a_patch_from_another_commit_is_refused(self) -> None:
        with self.assertRaisesRegex(PullRequestActionError, "outside the remediation context"):
            self.action.open(context=_context(), patch=_patch(commit="c" * 40))

    def test_missing_content_is_an_error(self) -> None:
        with self.assertRaises(PatchContentError):
            self.action.open(context=_context(), patch=_patch(digest="e" * 64))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
