"""GitHub REST snapshot adapter stays scoped, deterministic, and GET-only."""

import hashlib
import unittest
from base64 import b64encode

from agent.runtime import (
    GitHubRestSnapshotTool,
    GitHubSnapshotNotFoundError,
    GitHubToolError,
    GitHubToolScopeError,
    IaCDocumentReader,
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


class DocumentClient(Client):
    """Serve base64 Terraform blobs so the IAC perspective has a body to evaluate."""

    def __init__(self, *, blobs: dict[str, dict[str, object]] | None = None) -> None:
        super().__init__()
        self.blobs = (
            blobs
            if blobs is not None
            else {
                "blob-a": {
                    "encoding": "base64",
                    "content": b64encode(b'resource "aws_s3_bucket" "a" {}').decode(),
                },
                "blob-c": {
                    "encoding": "base64",
                    "content": b64encode(b"# logging module").decode(),
                },
            }
        )

    def request(self, url: str, headers: dict[str, str]) -> dict[str, object]:
        if "/git/blobs/" in url:
            self.calls.append((url, headers))
            return self.blobs[url.rsplit("/", 1)[1]]
        return super().request(url, headers)


def document_tool(client: DocumentClient) -> GitHubRestSnapshotTool:
    return GitHubRestSnapshotTool(
        customer_id="cust-001",
        repository_id="repo-001",
        repository_full_name="customer/iac",
        token_provider=lambda: "installation-token",
        request=client.request,
    )


class GitHubRestIaCDocumentTest(unittest.TestCase):
    def test_reads_only_terraform_blobs_at_the_pinned_commit(self) -> None:
        client = DocumentClient()

        document = document_tool(client).read_iac_document(request())

        self.assertEqual(document.commit_sha, "a" * 40)
        self.assertEqual(
            document.files,
            (
                ("main.tf", 'resource "aws_s3_bucket" "a" {}'),
                ("modules/logging.tf", "# logging module"),
            ),
        )
        self.assertEqual(
            document.evidence_references, ("terraform:main.tf", "terraform:modules/logging.tf")
        )
        # notes.txt is never fetched: only the manifest's Terraform blobs are read.
        self.assertTrue(all("blob-b" not in url for url, _ in client.calls))

    def test_rejects_scope_escape_before_any_read(self) -> None:
        client = DocumentClient()

        with self.assertRaises(GitHubToolScopeError):
            document_tool(client).read_iac_document(request(customer_id="cust-002"))
        self.assertEqual(client.calls, [])

    def test_rejects_unsupported_blob_encoding(self) -> None:
        client = DocumentClient(blobs={"blob-a": {"encoding": "utf-8", "content": "x"}})

        with self.assertRaisesRegex(GitHubToolError, "encoding"):
            document_tool(client).read_iac_document(request())

    def test_rejects_undecodable_blob_content(self) -> None:
        client = DocumentClient(
            blobs={"blob-a": {"encoding": "base64", "content": b64encode(b"\xff\xfe").decode()}}
        )

        with self.assertRaisesRegex(GitHubToolError, "invalid"):
            document_tool(client).read_iac_document(request())

    def test_rejects_a_body_over_the_approved_read_limit(self) -> None:
        oversized = b64encode(b"a" * 1_000_001).decode()
        client = DocumentClient(blobs={"blob-a": {"encoding": "base64", "content": oversized}})

        with self.assertRaisesRegex(GitHubToolError, "read limit"):
            document_tool(client).read_iac_document(request())

    def test_satisfies_the_document_reader_protocol(self) -> None:
        self.assertIsInstance(document_tool(DocumentClient()), IaCDocumentReader)
