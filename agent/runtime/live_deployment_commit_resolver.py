"""D-owned read-only GitHub adapter resolving a patch's default-branch commit.

ADR-0019 §3은 apply 대상을 "사람이 merge한 default branch의 merge commit"으로 고정하고, §4는
"default branch에서 도달 가능"을 Deployment 생성의 전제조건으로 만든다. 두 질문은 같은 GitHub
read로 답하므로 하나의 port(`DeploymentCommitResolver`)로 둔다.

읽기 전용이다. 이 adapter는 branch·commit·PR을 만들지 않는다 — 그 write 경계는 M2 D의 별도
adapter이며, 여기서는 이미 존재하는 PR의 merge 결과만 관측한다.

해석 순서:
1. repository의 `default_branch`를 읽는다. 이름을 설정으로 받지 않는다 — 설정이 실제 repository와
   어긋나면 엉뚱한 branch를 "default"로 믿고 apply 대상을 고르게 된다.
2. patch에서 결정적으로 유도한 head branch(`derive_head_branch`)의 PR을 찾는다. 같은 patch는 항상
   같은 branch 이름을 내므로 PR 번호를 따로 저장할 필요가 없다.
3. merge된 PR의 `merge_commit_sha`가 default branch에서 도달 가능한지 compare로 확인한다.
   `merged_at`만 보고 끝내지 않는다 — merge 뒤 default branch가 되돌려졌으면(revert/force-push)
   그 commit은 더 이상 배포 대상이 아니다.

merge되지 않았거나 도달 불가면 `None`을 돌려준다. 오류가 아니라 "아직 아님"이므로 호출자가
`CONFLICT`로 보고하고 고객이 준비되면 merge한다(ADR-0019 §4).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from agent.runtime.github_tool import require_github_repository_full_name
from agent.runtime.github_write_tool import derive_head_branch
from packages.contracts import RemediationPatch

# 하나의 patch에 대한 PR은 결정적 branch 이름으로 조회하므로 여러 건이 나올 수 없다. 열려 있는
# 재시도 PR과 merge된 PR이 함께 잡히는 경우만 대비해 작은 상한을 둔다.
_MAX_PULL_REQUESTS = 10


class DeploymentCommitResolverError(RuntimeError):
    """GitHub read를 완결할 수 없어 도달 가능성을 판정하지 못했다."""


class LiveDeploymentCommitResolver:
    """Resolve the merged default-branch commit for one approved repository scope."""

    def __init__(
        self,
        *,
        customer_id: str,
        repository_id: str,
        repository_full_name: str,
        token_provider: Callable[[], str],
        request: Callable[[str, Mapping[str, str]], object] | None = None,
    ) -> None:
        for name, value in (("customer_id", customer_id), ("repository_id", repository_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not callable(token_provider):
            raise TypeError("token_provider must be callable")
        self._customer_id = customer_id
        self._repository_id = repository_id
        self._repository_full_name = require_github_repository_full_name(repository_full_name)
        self._token_provider = token_provider
        self._request = request or _github_get

    def resolve_default_branch_commit(
        self, *, customer_id: str, repository_id: str, patch: RemediationPatch
    ) -> str | None:
        if not isinstance(patch, RemediationPatch):
            raise TypeError("patch must be a RemediationPatch")
        # 이 adapter는 승인된 하나의 (customer, repository)에만 붙는다. scope 밖 요청을 조용히
        # 처리하면 한 고객의 승인이 다른 고객 repository를 읽는 경로가 된다.
        if customer_id != self._customer_id or repository_id != self._repository_id:
            raise DeploymentCommitResolverError("request is outside the approved repository scope")
        if (
            patch.artifact.customer_id != customer_id
            or patch.artifact.repository_id != repository_id
        ):
            raise DeploymentCommitResolverError("patch is outside the approved repository scope")

        headers = self._headers()
        default_branch = _string(
            _mapping(self._get(f"https://api.github.com/repos/{self._repo()}", headers)).get(
                "default_branch"
            ),
            "default_branch",
        )
        merge_commit = self._merged_commit(derive_head_branch(patch), headers)
        if merge_commit is None:
            return None
        if not self._is_reachable(default_branch, merge_commit, headers):
            return None
        return merge_commit

    def _merged_commit(self, head_branch: str, headers: Mapping[str, str]) -> str | None:
        owner = self._repository_full_name.split("/", 1)[0]
        url = (
            f"https://api.github.com/repos/{self._repo()}/pulls"
            f"?state=all&per_page={_MAX_PULL_REQUESTS}"
            f"&head={quote(f'{owner}:{head_branch}', safe='')}"
        )
        payload = self._get(url, headers)
        if not isinstance(payload, list):
            raise DeploymentCommitResolverError("GitHub pull request list is invalid")
        for entry in payload[:_MAX_PULL_REQUESTS]:
            pull = _mapping(entry)
            if pull.get("merged_at") is None:
                continue
            merge_commit_sha = pull.get("merge_commit_sha")
            if isinstance(merge_commit_sha, str) and merge_commit_sha.strip():
                return merge_commit_sha
            # merge 표시가 있는데 merge commit이 없으면 어느 commit을 배포할지 알 수 없다.
            raise DeploymentCommitResolverError("merged pull request has no merge commit")
        return None

    def _is_reachable(
        self, default_branch: str, commit_sha: str, headers: Mapping[str, str]
    ) -> bool:
        """Return whether `commit_sha` is the default branch head or an ancestor of it.

        `compare/{base}...{head}`의 `status`는 base 기준이다. `identical`은 같은 commit,
        `behind`는 head가 base의 조상 — 둘 다 default branch가 그 commit을 포함한다는 뜻이다.
        `ahead`/`diverged`는 default branch에 없는 commit이므로 배포 대상이 아니다.
        """
        url = (
            f"https://api.github.com/repos/{self._repo()}/compare/"
            f"{quote(default_branch, safe='')}...{quote(commit_sha, safe='')}"
        )
        status = _string(_mapping(self._get(url, headers)).get("status"), "compare status")
        return status in {"identical", "behind"}

    def _repo(self) -> str:
        return self._repository_full_name

    def _headers(self) -> dict[str, str]:
        token = self._token_provider()
        if not isinstance(token, str) or not token.strip():
            raise DeploymentCommitResolverError("GitHub token is unavailable")
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _get(self, url: str, headers: Mapping[str, str]) -> object:
        return self._request(url, headers)


def _github_get(url: str, headers: Mapping[str, str]) -> object:
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with urlopen(request, timeout=15) as response:  # noqa: S310 - fixed https GitHub host
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise DeploymentCommitResolverError(f"GitHub read failed with {error.code}") from None
    except (URLError, TimeoutError, ValueError):
        raise DeploymentCommitResolverError("GitHub read failed") from None


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DeploymentCommitResolverError("GitHub response is invalid")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeploymentCommitResolverError(f"GitHub {name} is invalid")
    return value
