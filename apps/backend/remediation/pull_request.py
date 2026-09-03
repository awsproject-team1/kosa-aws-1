"""C-side pull request action: read the stored patch bytes and hand them to D's writer.

Worker는 patch identity(`RemediationPatch`)만 다룬다. 실제 파일 내용은 content store에 있고, PR을
여는 D 어댑터는 그 내용이 필요하다. 이 action이 둘을 잇는다 — 저장된 바이트를 patch의 digest로
검증해 읽고, patch가 선언한 파일 집합과 같음을 확인한 뒤에만 writer를 부른다.

여기서는 GitHub를 직접 부르지 않고 어떤 판정도 하지 않는다. 판정(TERRAFORM_PATCH 허용)은
Worker가 먼저 통과시켰고, 이 action은 이미 저장된 patch에 대해서만 실행된다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from agent.runtime.github_write_tool import OpenedPullRequest
from apps.backend.remediation.patch_content import PatchContentStore
from packages.contracts import RemediationContext, RemediationPatch


class PullRequestActionError(ValueError):
    """The pull request could not be opened for this patch."""


class PullRequestWriter(Protocol):
    """D's write boundary: branch, commits, and pull request for one patch."""

    def open_pull_request(
        self, patch: RemediationPatch, changes: Mapping[str, str]
    ) -> OpenedPullRequest: ...


class PatchPullRequestAction:
    """Open the pull request for a stored patch through the injected writer."""

    def __init__(self, *, writer: PullRequestWriter, content_store: PatchContentStore) -> None:
        if writer is None or content_store is None:
            raise TypeError("writer and content_store are required")
        self._writer = writer
        self._content_store = content_store

    def open(self, *, context: RemediationContext, patch: RemediationPatch) -> OpenedPullRequest:
        if not isinstance(context, RemediationContext):
            raise TypeError("context must be a RemediationContext")
        if not isinstance(patch, RemediationPatch):
            raise TypeError("patch must be a RemediationPatch")
        snapshot = context.snapshot
        if (
            patch.finding_id != context.finding.finding_id
            or patch.base_commit_sha != snapshot.commit_sha
            or patch.artifact.customer_id != snapshot.customer_id
            or patch.artifact.repository_id != snapshot.repository_id
        ):
            raise PullRequestActionError("patch is outside the remediation context")
        content = self._content_store.get(patch=patch)
        opened = self._writer.open_pull_request(patch, content.changes)
        if not isinstance(opened, OpenedPullRequest):
            raise PullRequestActionError("writer must return an OpenedPullRequest")
        if (
            opened.finding_id != patch.finding_id
            or opened.customer_id != patch.artifact.customer_id
            or opened.repository_id != patch.artifact.repository_id
        ):
            raise PullRequestActionError("opened pull request is outside the patch scope")
        return opened
