"""Read an approved Terraform repository revision through GitHub's REST API only."""

from __future__ import annotations

import hashlib
import json
from base64 import b64decode
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agent.runtime.github_tool import (
    GitHubSnapshotNotFoundError,
    GitHubTool,
    GitHubToolError,
    IaCDocument,
    IaCSnapshotRequest,
    require_repository_scope,
    require_snapshot_request,
)
from packages.contracts import ArtifactReference, ArtifactType, IaCSnapshot

# One Initial Assessment reads a Terraform root module, not an entire monorepo.
_MAX_DOCUMENT_BYTES = 1_000_000


@dataclass(frozen=True, slots=True, kw_only=True)
class GitHubRepositoryRevision:
    """Descriptive Terraform blob manifest for one immutable Git commit."""

    commit_sha: str
    terraform_blobs: tuple[tuple[str, str], ...]


class GitHubRestSnapshotTool(GitHubTool):
    """Scoped GitHub REST adapter with GET-only requests and no write surface."""

    def __init__(
        self,
        *,
        customer_id: str,
        repository_id: str,
        repository_full_name: str,
        token_provider: Callable[[], str],
        request: Callable[[str, Mapping[str, str]], Mapping[str, object]] | None = None,
    ) -> None:
        for name, value in (
            ("customer_id", customer_id),
            ("repository_id", repository_id),
            ("repository_full_name", repository_full_name),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not callable(token_provider):
            raise TypeError("token_provider must be callable")
        self._customer_id = customer_id
        self._repository_id = repository_id
        self._repository_full_name = repository_full_name
        self._token_provider = token_provider
        self._request = request or _github_get

    def read_iac_snapshot(self, request: IaCSnapshotRequest) -> IaCSnapshot:
        request = require_snapshot_request(request)
        require_repository_scope(
            request, customer_id=self._customer_id, repository_id=self._repository_id
        )
        revision = self._read_revision(request.commit_sha)
        manifest = _manifest(revision.terraform_blobs)
        return IaCSnapshot(
            customer_id=request.customer_id,
            repository_id=request.repository_id,
            commit_sha=revision.commit_sha,
            artifact=ArtifactReference(
                artifact_id=f"github-tree:{self._repository_id}:{revision.commit_sha}",
                artifact_type=ArtifactType.TERRAFORM_SNAPSHOT,
                content_sha256=hashlib.sha256(manifest).hexdigest(),
                customer_id=request.customer_id,
                repository_id=request.repository_id,
            ),
        )

    def read_iac_document(self, request: IaCSnapshotRequest) -> IaCDocument:
        """Read the Terraform body at the approved commit so IAC can be evaluated.

        Only the blobs already listed by the immutable tree read are fetched, and the
        combined body is capped so one repository cannot exhaust the Worker.
        """
        request = require_snapshot_request(request)
        require_repository_scope(
            request, customer_id=self._customer_id, repository_id=self._repository_id
        )
        revision = self._read_revision(request.commit_sha)
        headers = self._headers()
        total = 0
        files: list[tuple[str, str]] = []
        for path, blob_sha in revision.terraform_blobs:
            content = self._read_blob(blob_sha, headers)
            total += len(content.encode("utf-8"))
            if total > _MAX_DOCUMENT_BYTES:
                raise GitHubToolError("Terraform body exceeds the approved read limit")
            files.append((path, content))
        return IaCDocument(
            customer_id=request.customer_id,
            repository_id=request.repository_id,
            commit_sha=revision.commit_sha,
            files=tuple(files),
        )

    def _read_blob(self, blob_sha: str, headers: Mapping[str, str]) -> str:
        payload = self._request(
            f"https://api.github.com/repos/{self._repository_full_name}/git/blobs/{blob_sha}",
            headers,
        )
        if payload.get("encoding") != "base64":
            raise GitHubToolError("GitHub blob encoding is unsupported")
        try:
            raw = b64decode(_required_string(payload.get("content"), "GitHub blob content"))
            return raw.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise GitHubToolError("GitHub blob content is invalid") from None

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {_token(self._token_provider())}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _read_revision(self, commit_sha: str) -> GitHubRepositoryRevision:
        headers = self._headers()
        encoded_repo = self._repository_full_name
        try:
            commit = self._request(
                f"https://api.github.com/repos/{encoded_repo}/commits/{commit_sha}", headers
            )
            resolved_sha = _required_string(commit.get("sha"), "GitHub commit sha")
            tree = self._request(
                f"https://api.github.com/repos/{encoded_repo}/git/trees/{resolved_sha}?recursive=1",
                headers,
            )
        except GitHubSnapshotNotFoundError:
            raise
        except Exception as error:
            raise GitHubToolError("GitHub snapshot read failed") from error
        entries = tree.get("tree")
        if not isinstance(entries, list):
            raise GitHubToolError("GitHub repository tree is invalid")
        blobs: list[tuple[str, str]] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise GitHubToolError("GitHub repository tree is invalid")
            path, sha, kind = entry.get("path"), entry.get("sha"), entry.get("type")
            if not isinstance(path, str) or not isinstance(sha, str) or not isinstance(kind, str):
                raise GitHubToolError("GitHub repository tree is invalid")
            if kind == "blob" and path.endswith(".tf"):
                blobs.append((path, sha))
        if not blobs:
            raise GitHubSnapshotNotFoundError(
                "Terraform files were not found at the requested commit"
            )
        return GitHubRepositoryRevision(commit_sha=resolved_sha, terraform_blobs=tuple(blobs))


def _github_get(url: str, headers: Mapping[str, str]) -> Mapping[str, object]:
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310 - fixed GitHub API origin.
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        if error.code == 404:
            raise GitHubSnapshotNotFoundError("GitHub revision was not found") from None
        raise GitHubToolError("GitHub snapshot read failed") from None
    except (URLError, TimeoutError, ValueError):
        raise GitHubToolError("GitHub snapshot read failed") from None
    if not isinstance(payload, Mapping):
        raise GitHubToolError("GitHub response is invalid")
    return payload


def _manifest(blobs: tuple[tuple[str, str], ...]) -> bytes:
    return json.dumps(sorted(blobs), ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _token(value: object) -> str:
    return _required_string(value, "GitHub token")


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GitHubToolError(f"{field_name} is invalid")
    return value
