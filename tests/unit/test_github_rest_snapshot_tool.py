"""GitHub REST snapshot adapter stays scoped, deterministic, and GET-only."""

import hashlib
import unittest

from agent.runtime import (
    GitHubRestSnapshotTool,
    GitHubSnapshotNotFoundError,
    GitHubToolScopeError,
    IaCSnapshotRequest,
)


class Client:
    def __init__(self, *, tree: list[dict[str, str]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.tree = (
            tree
            if tree is not None
            else [
                {"path": "main.tf", "sha": "blob-a", "type": "blob"},
                {"path": "notes.txt", "sha": "blob-b", "type": "blob"},
                {"path": "modules/logging.tf", "sha": "blob-c", "type": "blob"},
            ]
        )

    def request(self, url: str, headers: dict[str, str]) -> dict[str, object]:
        self.calls.append((url, headers))
        if "/commits/" in url:
            return {"sha": "a" * 40}
        return {"tree": self.tree}


def request(
    *, customer_id: str = "cust-001", repository_id: str = "repo-001"
) -> IaCSnapshotRequest:
    return IaCSnapshotRequest(
        customer_id=customer_id, repository_id=repository_id, commit_sha="main"
    )


class GitHubRestSnapshotToolTest(unittest.TestCase):
    def test_reads_only_the_pinned_commit_and_hashes_terraform_manifest(self) -> None:
        client = Client()
        tool = GitHubRestSnapshotTool(
            customer_id="cust-001",
            repository_id="repo-001",
            repository_full_name="customer/iac",
            token_provider=lambda: "installation-token",
            request=client.request,
        )

        snapshot = tool.read_iac_snapshot(request())

        self.assertEqual(snapshot.commit_sha, "a" * 40)
        self.assertEqual(
            snapshot.artifact.content_sha256,
            hashlib.sha256(b'[["main.tf","blob-a"],["modules/logging.tf","blob-c"]]').hexdigest(),
        )
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(
            all(
                url.startswith("https://api.github.com/repos/customer/iac/")
                for url, _ in client.calls
            )
        )
        self.assertTrue(
            all(
                headers["Authorization"] == "Bearer installation-token"
                for _, headers in client.calls
            )
        )

    def test_rejects_scope_escape_and_non_terraform_revision(self) -> None:
        tool = GitHubRestSnapshotTool(
            customer_id="cust-001",
            repository_id="repo-001",
            repository_full_name="customer/iac",
            token_provider=lambda: "token",
            request=Client(tree=[]).request,
        )
        with self.assertRaises(GitHubToolScopeError):
            tool.read_iac_snapshot(request(customer_id="cust-002"))
        with self.assertRaises(GitHubSnapshotNotFoundError):
            tool.read_iac_snapshot(request())
