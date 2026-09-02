"""D(Remediation/Deployment)를 위한 GitHub write 제안 경계.

이 모듈은 승인된 `RemediationPatch`를 받아 그 patch를 적용할 Branch/Commit/Pull Request의
좌표를 *제안*하는 provider-neutral port를 정의한다. read-only `github_tool.py`가 IaC
snapshot을 읽는 경계라면, 이 경계는 그 다음 단계인 "제안된 변경을 어디에 어떻게 올릴지"를
결정적으로 규정한다.

핵심 원칙(ADR-0007):
- 접근은 승인된 하나의 (customer_id, repository_id) scope로 제한된다. patch가 그 scope
  밖이면 거부한다. 호출자는 scope를 policy나 AI 입력에 위임할 수 없다.
- 생성되는 것은 "제안"(ProposedPullRequest)이며, 이 경계 자체는 어떤 실제 GitHub write도
  하지 않는다. 실제 Branch/Commit/PR 생성 API 호출, Terraform Plan(OIDC), Apply는 각각
  Integrated 단계와 이후 Task(task8, M3) 범위다.
- 같은 patch는 항상 같은 제안(branch 이름·PR 좌표)을 만든다. 결정성으로 재실행이 중복
  branch/PR을 만들지 않도록 한다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from packages.contracts import RemediationPatch


class GitHubWriteToolError(RuntimeError):
    """GitHub write 제안 경계 작업의 기본 실패 타입."""


class GitHubWriteScopeError(GitHubWriteToolError):
    """patch가 tool scope 밖의 customer/repository를 대상으로 할 때 발생한다."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ProposedPullRequest:
    """하나의 `RemediationPatch`를 올릴 Branch/Commit/PR에 대한 불변 제안.

    이 값은 서술적(descriptive) 제안일 뿐이며, 실제 GitHub 리소스를 만들지 않는다.
    좌표는 patch로부터 결정적으로 유도되므로, 같은 patch는 항상 같은 제안을 낸다.

    - ``base_commit_sha``: patch가 바인딩된 snapshot commit. PR의 base가 이 commit을
      가리키는 branch임을 규정한다.
    - ``head_branch``: 제안된 작업 branch 이름. patch 좌표로부터 결정적으로 만든다.
    - ``title`` / ``body``: PR 메타데이터. finding_id를 인용해 추적 가능하게 한다.
    - ``changed_paths``: PR이 건드리는 repository-relative 경로. patch와 동일하다.
    """

    customer_id: str
    repository_id: str
    finding_id: str
    base_commit_sha: str
    head_branch: str
    title: str
    body: str
    changed_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            "customer_id",
            "repository_id",
            "finding_id",
            "base_commit_sha",
            "head_branch",
            "title",
            "body",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.changed_paths, tuple) or not self.changed_paths:
            raise ValueError("changed_paths must be a non-empty tuple")
        for path in self.changed_paths:
            if not isinstance(path, str) or not path.strip():
                raise ValueError("changed_paths item must be a non-empty string")
            if path.startswith("/") or ".." in path.split("/"):
                raise ValueError("changed_paths items must be repository-relative paths")

    def to_dict(self) -> dict[str, object]:
        return {
            "customer_id": self.customer_id,
            "repository_id": self.repository_id,
            "finding_id": self.finding_id,
            "base_commit_sha": self.base_commit_sha,
            "head_branch": self.head_branch,
            "title": self.title,
            "body": self.body,
            "changed_paths": list(self.changed_paths),
        }


@runtime_checkable
class GitHubWriteTool(Protocol):
    """승인된 patch에 대한 GitHub write 제안을 만드는 경계."""

    def propose_pull_request(self, patch: RemediationPatch) -> ProposedPullRequest:
        """tool scope 안에 있는 patch에 대한 PR 제안을 반환한다."""
        ...


def require_remediation_patch(patch: object) -> RemediationPatch:
    """write 제안 입력이 `RemediationPatch`인지 검증한다.

    이 검사를 한 곳에 모아두면, 모든 adapter가 동일한 입력 경계를 강제한다.
    """
    if not isinstance(patch, RemediationPatch):
        raise TypeError("patch must be a RemediationPatch")
    return patch


def require_patch_scope(
    patch: RemediationPatch, *, customer_id: str, repository_id: str
) -> RemediationPatch:
    """patch가 승인된 하나의 (customer, repo) scope 안에 머물도록 요구한다.

    ADR-0007은 승인된 Customer IaC repository에만 최소 권한 접근을 부여한다. patch의
    scope는 그 artifact reference의 customer_id/repository_id로 규정되며, 이 공유 가드가
    그 축을 강제한다. read-only 경계의 ``require_repository_scope``와 대칭이다.
    """
    if not isinstance(patch, RemediationPatch):
        raise TypeError("patch must be a RemediationPatch")
    if patch.artifact.customer_id != customer_id or patch.artifact.repository_id != repository_id:
        raise GitHubWriteScopeError("patch customer_id/repository_id is outside the tool scope")
    return patch


def derive_head_branch(patch: RemediationPatch) -> str:
    """patch로부터 결정적인 작업 branch 이름을 만든다.

    finding_id와 base_commit_sha, patch artifact digest를 좁은 접두사로 조합한다. 같은
    patch는 항상 같은 branch 이름을 내므로 재실행이 중복 branch를 만들지 않는다.
    """
    seed = "\x1f".join((patch.finding_id, patch.base_commit_sha, patch.artifact.content_sha256))
    short = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"remediation/{patch.finding_id}/{short}"
