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

from agent.runtime.github_tool import IaCDocument, IaCDocumentReader, IaCSnapshotRequest
from agent.runtime.github_write_tool import OpenedPullRequest
from apps.backend.remediation.patch_content import PatchContentStore
from apps.backend.remediation.terraform_change import (
    TerraformChangeError,
    render_unified_diff,
)
from packages.contracts import RemediationContext, RemediationPatch


class PullRequestActionError(ValueError):
    """The pull request could not be opened for this patch."""


class PullRequestWriter(Protocol):
    """D's write boundary: branch, commits, and pull request for one patch."""

    def open_pull_request(
        self,
        patch: RemediationPatch,
        changes: Mapping[str, str],
        *,
        description: str | None = None,
    ) -> OpenedPullRequest: ...


class PatchPullRequestAction:
    """Open the pull request for a stored patch through the injected writer.

    `iac_documents`가 주어지면 patch가 만든 변경을 그 commit의 원본과 대조한 unified diff를 PR
    본문에 싣는다. 사람이 승인하는 표면은 PR이므로, "무엇이 바뀌는가"는 저장된 patch 바이트가
    아니라 그 PR에서 읽을 수 있어야 한다.
    """

    def __init__(
        self,
        *,
        writer: PullRequestWriter,
        content_store: PatchContentStore,
        iac_documents: IaCDocumentReader | None = None,
    ) -> None:
        if writer is None or content_store is None:
            raise TypeError("writer and content_store are required")
        if iac_documents is not None and not isinstance(iac_documents, IaCDocumentReader):
            raise TypeError("iac_documents must implement IaCDocumentReader")
        self._writer = writer
        self._content_store = content_store
        self._iac_documents = iac_documents

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
        opened = self._writer.open_pull_request(
            patch, content.changes, description=self._description(context, content.changes)
        )
        if not isinstance(opened, OpenedPullRequest):
            raise PullRequestActionError("writer must return an OpenedPullRequest")
        if (
            opened.finding_id != patch.finding_id
            or opened.customer_id != patch.artifact.customer_id
            or opened.repository_id != patch.artifact.repository_id
        ):
            raise PullRequestActionError("opened pull request is outside the patch scope")
        return opened

    def _description(self, context: RemediationContext, changes: Mapping[str, str]) -> str:
        """The reviewer-facing summary: what the Finding said and the exact diff."""
        finding = context.finding
        lines = [
            f"Finding {finding.finding_id}: rule {finding.rule_id}@{finding.rule_version} "
            f"({finding.severity}, {finding.status.value}) on resource {finding.resource_id}.",
            f"Evaluator rationale: {finding.rationale}",
        ]
        if self._iac_documents is None:
            return "\n".join(lines)
        snapshot = context.snapshot
        document = self._iac_documents.read_iac_document(
            IaCSnapshotRequest(
                customer_id=snapshot.customer_id,
                repository_id=snapshot.repository_id,
                commit_sha=snapshot.commit_sha,
            )
        )
        if not isinstance(document, IaCDocument) or document.commit_sha != snapshot.commit_sha:
            raise PullRequestActionError("Terraform source document is outside the snapshot")
        try:
            diff = render_unified_diff(document, changes)
        except TerraformChangeError as error:
            raise PullRequestActionError(
                f"stored patch is not bound to the snapshot: {error}"
            ) from error
        lines.extend(("", "```diff", diff.rstrip("\n"), "```"))
        return "\n".join(lines)
