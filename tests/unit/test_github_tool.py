"""Unit tests for the read-only GitHub Integration Tool boundary and mock."""

import unittest

from agent.runtime import (
    GitHubSnapshotNotFoundError,
    GitHubToolScopeError,
    IaCSnapshotRequest,
    MockGitHubTool,
    require_snapshot_request,
)
from packages.contracts import ArtifactReference, ArtifactType, IaCSnapshot

CUSTOMER_ID = "cust-001"
REPOSITORY_ID = "repo-iac-001"
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


def build_snapshot(*, commit_sha: str) -> IaCSnapshot:
    return IaCSnapshot(
        customer_id=CUSTOMER_ID,
        repository_id=REPOSITORY_ID,
        commit_sha=commit_sha,
        artifact=ArtifactReference(
            artifact_id=f"artifact-{commit_sha[:8]}",
            artifact_type=ArtifactType.TERRAFORM_SNAPSHOT,
            content_sha256="0" * 64,
            customer_id=CUSTOMER_ID,
            repository_id=REPOSITORY_ID,
        ),
    )


def build_tool() -> MockGitHubTool:
    return MockGitHubTool(
        customer_id=CUSTOMER_ID,
        repository_id=REPOSITORY_ID,
        snapshots=[build_snapshot(commit_sha=COMMIT_A), build_snapshot(commit_sha=COMMIT_B)],
    )


def snapshot_request(*, commit_sha: str) -> IaCSnapshotRequest:
    return IaCSnapshotRequest(
        customer_id=CUSTOMER_ID,
        repository_id=REPOSITORY_ID,
        commit_sha=commit_sha,
    )


class GitHubToolTest(unittest.TestCase):
    def test_read_iac_snapshot_returns_the_scoped_snapshot(self) -> None:
        tool = build_tool()

        snapshot = tool.read_iac_snapshot(snapshot_request(commit_sha=COMMIT_A))

        self.assertEqual(snapshot.commit_sha, COMMIT_A)
        self.assertEqual(snapshot.artifact.artifact_type, ArtifactType.TERRAFORM_SNAPSHOT)

    def test_read_iac_snapshot_rejects_request_outside_customer_scope(self) -> None:
        tool = build_tool()
        other_customer = IaCSnapshotRequest(
            customer_id="cust-999",
            repository_id=REPOSITORY_ID,
            commit_sha=COMMIT_A,
        )

        with self.assertRaises(GitHubToolScopeError):
            tool.read_iac_snapshot(other_customer)

    def test_read_iac_snapshot_rejects_request_outside_repository_scope(self) -> None:
        tool = build_tool()
        other_repo = IaCSnapshotRequest(
            customer_id=CUSTOMER_ID,
            repository_id="repo-other",
            commit_sha=COMMIT_A,
        )

        with self.assertRaises(GitHubToolScopeError):
            tool.read_iac_snapshot(other_repo)

    def test_read_iac_snapshot_raises_for_unknown_commit(self) -> None:
        tool = build_tool()

        with self.assertRaises(GitHubSnapshotNotFoundError):
            tool.read_iac_snapshot(snapshot_request(commit_sha="c" * 40))

    def test_tool_only_exposes_a_read_operation(self) -> None:
        # Freeze the read-only boundary: no write/PR surface on the port.
        public_methods = {name for name in dir(MockGitHubTool) if not name.startswith("_")}
        self.assertEqual(public_methods, {"read_iac_snapshot"})

    def test_require_snapshot_request_rejects_non_request_objects(self) -> None:
        with self.assertRaises(TypeError):
            require_snapshot_request(object())

    def test_snapshot_request_round_trips_to_dict(self) -> None:
        request = snapshot_request(commit_sha=COMMIT_A)

        self.assertEqual(
            request.to_dict(),
            {
                "customer_id": CUSTOMER_ID,
                "repository_id": REPOSITORY_ID,
                "commit_sha": COMMIT_A,
            },
        )

    def test_snapshot_request_rejects_empty_fields(self) -> None:
        with self.assertRaises(ValueError):
            IaCSnapshotRequest(
                customer_id=CUSTOMER_ID,
                repository_id=REPOSITORY_ID,
                commit_sha="   ",
            )

    def test_tool_rejects_snapshot_from_a_different_customer(self) -> None:
        with self.assertRaises(ValueError):
            MockGitHubTool(
                customer_id=CUSTOMER_ID,
                repository_id=REPOSITORY_ID,
                snapshots=[
                    IaCSnapshot(
                        customer_id="cust-999",
                        repository_id=REPOSITORY_ID,
                        commit_sha=COMMIT_A,
                        artifact=ArtifactReference(
                            artifact_id="artifact-x",
                            artifact_type=ArtifactType.TERRAFORM_SNAPSHOT,
                            content_sha256="0" * 64,
                            customer_id="cust-999",
                            repository_id=REPOSITORY_ID,
                        ),
                    )
                ],
            )

    def test_tool_rejects_snapshot_from_a_different_repository(self) -> None:
        with self.assertRaises(ValueError):
            MockGitHubTool(
                customer_id=CUSTOMER_ID,
                repository_id=REPOSITORY_ID,
                snapshots=[
                    IaCSnapshot(
                        customer_id=CUSTOMER_ID,
                        repository_id="repo-other",
                        commit_sha=COMMIT_A,
                        artifact=ArtifactReference(
                            artifact_id="artifact-x",
                            artifact_type=ArtifactType.TERRAFORM_SNAPSHOT,
                            content_sha256="0" * 64,
                            customer_id=CUSTOMER_ID,
                            repository_id="repo-other",
                        ),
                    )
                ],
            )


if __name__ == "__main__":
    unittest.main()
