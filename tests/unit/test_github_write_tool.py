"""GitHub write 제안 경계와 결정적 Mock 어댑터에 대한 Unit 테스트."""

import unittest

from agent.runtime import (
    GitHubWriteScopeError,
    GitHubWriteTool,
    MockGitHubWriteTool,
    ProposedPullRequest,
    derive_head_branch,
    require_patch_scope,
    require_remediation_patch,
)
from packages.contracts import ArtifactReference, ArtifactType, RemediationPatch

CUSTOMER_ID = "cust-001"
REPOSITORY_ID = "repo-iac-001"
FINDING_ID = "finding-abc123"
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


def build_patch(
    *,
    finding_id: str = FINDING_ID,
    base_commit_sha: str = COMMIT_A,
    customer_id: str = CUSTOMER_ID,
    repository_id: str | None = REPOSITORY_ID,
    changed_paths: tuple[str, ...] = ("modules/s3/main.tf",),
    content_sha256: str = "0" * 64,
) -> RemediationPatch:
    return RemediationPatch(
        finding_id=finding_id,
        base_commit_sha=base_commit_sha,
        artifact=ArtifactReference(
            artifact_id=f"remediation-patch:{repository_id}:{finding_id}",
            artifact_type=ArtifactType.REMEDIATION_PATCH,
            content_sha256=content_sha256,
            customer_id=customer_id,
            repository_id=repository_id,
        ),
        changed_paths=changed_paths,
    )


def build_tool() -> MockGitHubWriteTool:
    return MockGitHubWriteTool(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID)


class GitHubWriteToolTest(unittest.TestCase):
    def test_propose_pull_request_binds_proposal_to_patch(self) -> None:
        tool = build_tool()

        proposal = tool.propose_pull_request(build_patch())

        self.assertEqual(proposal.customer_id, CUSTOMER_ID)
        self.assertEqual(proposal.repository_id, REPOSITORY_ID)
        self.assertEqual(proposal.finding_id, FINDING_ID)
        self.assertEqual(proposal.base_commit_sha, COMMIT_A)
        self.assertEqual(proposal.changed_paths, ("modules/s3/main.tf",))
        self.assertIn(FINDING_ID, proposal.head_branch)

    def test_proposal_is_deterministic_for_the_same_patch(self) -> None:
        tool = build_tool()
        patch = build_patch()

        first = tool.propose_pull_request(patch)
        second = tool.propose_pull_request(patch)

        self.assertEqual(first, second)

    def test_head_branch_differs_by_base_commit(self) -> None:
        tool = build_tool()

        branch_a = tool.propose_pull_request(build_patch(base_commit_sha=COMMIT_A)).head_branch
        branch_b = tool.propose_pull_request(build_patch(base_commit_sha=COMMIT_B)).head_branch

        self.assertNotEqual(branch_a, branch_b)

    def test_head_branch_differs_by_patch_content(self) -> None:
        tool = build_tool()

        branch_1 = tool.propose_pull_request(build_patch(content_sha256="1" * 64)).head_branch
        branch_2 = tool.propose_pull_request(build_patch(content_sha256="2" * 64)).head_branch

        self.assertNotEqual(branch_1, branch_2)

    def test_propose_rejects_patch_outside_customer_scope(self) -> None:
        tool = build_tool()

        with self.assertRaises(GitHubWriteScopeError):
            tool.propose_pull_request(build_patch(customer_id="cust-999"))

    def test_propose_rejects_patch_outside_repository_scope(self) -> None:
        tool = build_tool()

        with self.assertRaises(GitHubWriteScopeError):
            tool.propose_pull_request(build_patch(repository_id="repo-other"))

    def test_propose_rejects_non_patch_input(self) -> None:
        tool = build_tool()

        with self.assertRaises(TypeError):
            tool.propose_pull_request(object())

    def test_tool_only_exposes_the_write_proposal_operation(self) -> None:
        # write 경계는 PR 제안 표면만 노출한다(실제 write/apply 없음).
        public_methods = {name for name in dir(MockGitHubWriteTool) if not name.startswith("_")}
        self.assertEqual(public_methods, {"propose_pull_request"})

    def test_mock_tool_satisfies_the_write_tool_protocol(self) -> None:
        self.assertIsInstance(build_tool(), GitHubWriteTool)

    def test_require_remediation_patch_rejects_non_patch_objects(self) -> None:
        with self.assertRaises(TypeError):
            require_remediation_patch(object())

    def test_require_patch_scope_rejects_out_of_scope_patch(self) -> None:
        with self.assertRaises(GitHubWriteScopeError):
            require_patch_scope(
                build_patch(customer_id="cust-999"),
                customer_id=CUSTOMER_ID,
                repository_id=REPOSITORY_ID,
            )

    def test_derive_head_branch_is_stable(self) -> None:
        patch = build_patch()

        self.assertEqual(derive_head_branch(patch), derive_head_branch(patch))

    def test_tool_rejects_empty_scope(self) -> None:
        with self.assertRaises(ValueError):
            MockGitHubWriteTool(customer_id="   ", repository_id=REPOSITORY_ID)

    def test_proposal_round_trips_to_dict(self) -> None:
        proposal = build_tool().propose_pull_request(build_patch())

        payload = proposal.to_dict()

        self.assertEqual(payload["customer_id"], CUSTOMER_ID)
        self.assertEqual(payload["repository_id"], REPOSITORY_ID)
        self.assertEqual(payload["finding_id"], FINDING_ID)
        self.assertEqual(payload["base_commit_sha"], COMMIT_A)
        self.assertEqual(payload["changed_paths"], ["modules/s3/main.tf"])

    def test_proposed_pull_request_rejects_absolute_paths(self) -> None:
        with self.assertRaises(ValueError):
            ProposedPullRequest(
                customer_id=CUSTOMER_ID,
                repository_id=REPOSITORY_ID,
                finding_id=FINDING_ID,
                base_commit_sha=COMMIT_A,
                head_branch="remediation/x",
                title="t",
                body="b",
                changed_paths=("/etc/passwd",),
            )


if __name__ == "__main__":
    unittest.main()
