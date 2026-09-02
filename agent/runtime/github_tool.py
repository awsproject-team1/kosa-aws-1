"""D(Remediation/Deployment)를 위한 read-only GitHub Integration Tool 경계.

이 모듈은 agent runtime이 승인된 GitHub repository에서 Customer IaC 상태를 읽을 때
사용하는 provider-neutral port를 정의한다. ADR-0007에 따라 GitHub App은 승인된
Customer IaC repository에만 최소 권한으로 접근하며, Remediation write 경로
(Branch/Commit/PR)는 M2까지 의도적으로 범위 밖이다. 따라서 이 경계는 IaC snapshot
read만 노출하며 write나 mutation을 표현할 수 없다. 접근은 승인된
(customer_id, repository_id) 쌍으로 scope가 제한되고, 호출자는 그 scope를 policy나
AI 입력에 위임해서는 안 된다.
"""

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from packages.contracts import IaCSnapshot

_GITHUB_OWNER = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
_GITHUB_REPOSITORY = re.compile(r"^[A-Za-z0-9._-]+$")


class GitHubToolError(RuntimeError):
    """read-only GitHub Integration Tool 작업의 기본 실패 타입."""


class GitHubToolScopeError(GitHubToolError):
    """요청이 tool scope 밖의 customer/repository를 대상으로 할 때 발생한다."""


class GitHubSnapshotNotFoundError(GitHubToolError):
    """요청한 IaC snapshot이 read 상태에 존재하지 않을 때 발생한다."""


def require_github_repository_full_name(value: object) -> str:
    """Validate one canonical GitHub ``owner/repository`` path identity."""
    if not isinstance(value, str):
        raise ValueError("GitHub repository full name must be a string")
    parts = value.split("/")
    if len(parts) != 2:
        raise ValueError("GitHub repository full name must use owner/repository")
    owner, repository = parts
    if len(owner) > 39 or _GITHUB_OWNER.fullmatch(owner) is None:
        raise ValueError("GitHub repository owner is invalid")
    if (
        len(repository) > 100
        or repository in {".", ".."}
        or _GITHUB_REPOSITORY.fullmatch(repository) is None
        or re.search(r"[A-Za-z0-9]", repository) is None
    ):
        raise ValueError("GitHub repository name is invalid")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class IaCSnapshotRequest:
    """하나의 repository IaC snapshot에 대한 불변 read 요청.

    요청은 승인된 (customer_id, repository_id) scope와 정확한 ``commit_sha``를
    명시한다. repository를 변경할 수 있는 필드는 담지 않으며, tool은 이 좌표에
    해당하는 서술적(descriptive) snapshot만 반환한다.
    """

    customer_id: str
    repository_id: str
    commit_sha: str

    def __post_init__(self) -> None:
        for name in ("customer_id", "repository_id", "commit_sha"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")

    def to_dict(self) -> dict[str, str]:
        return {
            "customer_id": self.customer_id,
            "repository_id": self.repository_id,
            "commit_sha": self.commit_sha,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class IaCDocument:
    """하나의 불변 commit에 대한 read-only Terraform 본문.

    ``IaCSnapshot``은 Artifact reference(무엇을 읽었는지)만 담기 때문에 IAC 관점
    평가에는 부족하다. C가 IaC 준수 여부를 판정하려면 그 commit의 Terraform 본문이
    필요하다. 이 값은 read 결과이며 어떤 write 표면도 포함하지 않는다.
    """

    customer_id: str
    repository_id: str
    commit_sha: str
    files: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        for name in ("customer_id", "repository_id", "commit_sha"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.files, tuple) or not self.files:
            raise ValueError("files must be a non-empty tuple")
        paths: set[str] = set()
        for entry in self.files:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise TypeError("files must contain (path, content) pairs")
            path, content = entry
            if not isinstance(path, str) or not path.strip():
                raise ValueError("file path must be a non-empty string")
            if not isinstance(content, str):
                raise TypeError("file content must be a string")
            if path in paths:
                raise ValueError(f"duplicate IaC file path {path!r}")
            paths.add(path)

    @property
    def evidence_references(self) -> tuple[str, ...]:
        """평가기가 인용할 수 있는 `terraform:` namespace locator."""
        return tuple(f"terraform:{path}" for path, _ in self.files)

    def to_dict(self) -> dict[str, object]:
        return {
            "customer_id": self.customer_id,
            "repository_id": self.repository_id,
            "commit_sha": self.commit_sha,
            "files": [{"path": path, "content": content} for path, content in self.files],
        }


@runtime_checkable
class GitHubTool(Protocol):
    """Customer IaC 상태를 조회하는 데 필요한 read-only 작업."""

    def read_iac_snapshot(self, request: IaCSnapshotRequest) -> IaCSnapshot:
        """tool scope 안에 있는 요청에 대한 IaC snapshot을 반환한다."""
        ...


@runtime_checkable
class IaCDocumentReader(Protocol):
    """IAC 관점 평가를 위해 Terraform 본문까지 읽는 선택적 read-only 확장."""

    def read_iac_document(self, request: IaCSnapshotRequest) -> IaCDocument:
        """tool scope 안에 있는 요청에 대한 Terraform 본문을 반환한다."""
        ...


def require_snapshot_request(request: object) -> IaCSnapshotRequest:
    """read-only IaC snapshot 조회를 위한 요청 객체를 검증한다.

    이 검사를 한 곳에 모아두면, 호출자가 올바른 형태를 넘길 거라 믿는 대신
    모든 adapter가 동일한 read-only 경계를 강제하게 된다.
    """
    if not isinstance(request, IaCSnapshotRequest):
        raise TypeError("request must be an IaCSnapshotRequest")
    return request


def require_repository_scope(
    request: IaCSnapshotRequest, *, customer_id: str, repository_id: str
) -> IaCSnapshotRequest:
    """요청이 승인된 하나의 (customer, repo) scope 안에 머물도록 요구한다.

    ADR-0007은 승인된 Customer IaC repository에만 최소 권한 접근을 부여한다. 이
    공유 가드가 그 scope 축을 강제하므로, 모든 adapter(mock이든 실제 GitHub App이든)가
    adapter별 관례에 의존하지 않고 동일한 검사를 적용한다.
    """
    if not isinstance(request, IaCSnapshotRequest):
        raise TypeError("request must be an IaCSnapshotRequest")
    if request.customer_id != customer_id or request.repository_id != repository_id:
        raise GitHubToolScopeError("request customer_id/repository_id is outside the tool scope")
    return request
