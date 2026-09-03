"""Fixture/Mock 개발을 위한 결정적 in-memory GitHub write 제안 어댑터.

이 어댑터는 실제 GitHub App write 경로(Branch/Commit/PR 생성 API)가 존재하기 전에 D와
그 소비자가 write 제안 경계를 상대로 개발할 수 있게 한다. 정확히 하나의 승인된
(customer_id, repository_id) scope에 대해서만 동작하며, 그 scope 밖의 patch는 거부한다.
구조적으로 어떤 실제 write 경로도 없다 — patch를 받아 결정적 PR 제안만 만든다.
"""

from __future__ import annotations

from collections.abc import Mapping

from agent.runtime.github_write_tool import (
    OpenedPullRequest,
    ProposedPullRequest,
    derive_head_branch,
    require_patch_scope,
    require_remediation_patch,
)
from packages.contracts import RemediationPatch


class MockGitHubWriteTool:
    """하나의 scope로 제한된 결정적 PR 제안을 만드는 write-없는 어댑터."""

    def __init__(self, *, customer_id: str, repository_id: str) -> None:
        _require_non_empty_string(customer_id, "customer_id")
        _require_non_empty_string(repository_id, "repository_id")
        self._customer_id = customer_id
        self._repository_id = repository_id
        self.opened: list[tuple[RemediationPatch, dict[str, str]]] = []

    def propose_pull_request(self, patch: RemediationPatch) -> ProposedPullRequest:
        """tool scope 안에 있는 patch에 대한 결정적 PR 제안을 반환한다."""
        patch = require_remediation_patch(patch)
        self._require_scope(patch)
        head_branch = derive_head_branch(patch)
        return ProposedPullRequest(
            customer_id=patch.artifact.customer_id,
            repository_id=_require_repository_id(patch),
            finding_id=patch.finding_id,
            base_commit_sha=patch.base_commit_sha,
            head_branch=head_branch,
            title=f"Remediation for {patch.finding_id}",
            body=(
                f"Automated remediation proposal for finding {patch.finding_id}.\n"
                f"Base commit: {patch.base_commit_sha}\n"
                f"Changed paths: {', '.join(patch.changed_paths)}"
            ),
            changed_paths=patch.changed_paths,
        )

    def open_pull_request(
        self, patch: RemediationPatch, changes: Mapping[str, str]
    ) -> OpenedPullRequest:
        """Record the write that a live adapter would perform and return a deterministic PR."""
        proposal = self.propose_pull_request(patch)
        if tuple(sorted(changes)) != tuple(sorted(patch.changed_paths)):
            raise ValueError("changes do not match the patch's changed paths")
        self.opened.append((patch, dict(changes)))
        return OpenedPullRequest(
            customer_id=proposal.customer_id,
            repository_id=proposal.repository_id,
            finding_id=proposal.finding_id,
            head_branch=proposal.head_branch,
            head_commit_sha=f"{patch.artifact.content_sha256[:40]}",
            base_branch="main",
            number=len(self.opened),
            url=f"https://github.example/{proposal.repository_id}/pull/{len(self.opened)}",
        )

    def _require_scope(self, patch: RemediationPatch) -> None:
        require_patch_scope(
            patch,
            customer_id=self._customer_id,
            repository_id=self._repository_id,
        )


def _require_repository_id(patch: RemediationPatch) -> str:
    """scope 검증을 통과한 patch의 repository_id를 좁혀서 반환한다.

    `ArtifactReference.repository_id`는 Optional이지만, `require_patch_scope`가 이미
    tool의 non-empty repository_id와 일치함을 확인했으므로 이 시점에는 str이다.
    타입을 좁혀 `ProposedPullRequest`의 non-empty 계약을 만족시킨다.
    """
    repository_id = patch.artifact.repository_id
    if repository_id is None:
        raise ValueError("patch artifact repository_id must be set within tool scope")
    return repository_id


def _require_non_empty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
