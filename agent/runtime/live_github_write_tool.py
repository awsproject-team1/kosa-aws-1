"""D-owned live GitHub write adapter: branch, commits, and pull request for one patch.

`github_write_tool.py`의 제안 경계(`ProposedPullRequest`)를 실제 GitHub REST 호출로 잇는다.
ADR-0007·ADR-0019 §3·§6의 경계를 그대로 지킨다.

- 접근은 승인된 하나의 (customer_id, repository_id) scope다. 그 밖의 patch는 거부한다.
- write 표면은 세 가지뿐이다: branch ref 생성, 파일 contents 갱신, pull request 생성. workflow
  파일은 만들거나 고치지 않는다(App에 `workflows: write`가 없다). apply나 merge는 하지 않는다 —
  merge는 사람이 하고, apply 대상은 그 merge commit이다.
- 같은 patch는 항상 같은 branch 이름(`derive_head_branch`)을 낸다. 그래서 at-least-once 재전달이
  두 번째 branch나 두 번째 PR을 만들지 않는다: 이미 있는 ref는 그대로 쓰고, 같은 blob은 다시
  commit하지 않으며, 열려 있는 PR이 있으면 그것을 돌려준다.

호출 순서:
    GET  /repos/{repo}                          default_branch
    GET  /repos/{repo}/git/ref/heads/{branch}   있으면 재사용, 없으면
    POST /repos/{repo}/git/refs                 base_commit_sha에서 branch 생성
    GET  /repos/{repo}/contents/{path}?ref=     현재 blob sha (없으면 신규)
    PUT  /repos/{repo}/contents/{path}          내용이 다를 때만 commit
    GET  /repos/{repo}/pulls?head=owner:branch  열린 PR 재사용, 없으면
    POST /repos/{repo}/pulls                    PR 생성
    GET  /repos/{repo}/git/ref/heads/{branch}   head commit sha
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from agent.runtime.github_tool import require_github_repository_full_name
from agent.runtime.github_write_tool import (
    GitHubWriteToolError,
    OpenedPullRequest,
    ProposedPullRequest,
    derive_head_branch,
    require_patch_scope,
    require_remediation_patch,
)
from packages.contracts import RemediationPatch

#: (status, payload) — 주입 가능한 요청 함수의 반환형. 테스트가 GitHub 응답을 흉내 낸다.
GitHubRequest = Callable[[str, str, Mapping[str, str], bytes | None], tuple[int, object]]


class LiveGitHubWriteTool:
    """Open one pull request for one snapshot-bound patch inside an approved repository."""

    def __init__(
        self,
        *,
        customer_id: str,
        repository_id: str,
        repository_full_name: str,
        token_provider: Callable[[], str],
        request: GitHubRequest | None = None,
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
        self._request = request or _github_request

    def propose_pull_request(self, patch: RemediationPatch) -> ProposedPullRequest:
        """Describe the pull request this patch would open, without touching GitHub."""
        patch = require_remediation_patch(patch)
        self._require_scope(patch)
        return _proposal(patch, self._repository_id)

    def open_pull_request(
        self,
        patch: RemediationPatch,
        changes: Mapping[str, str],
        *,
        description: str | None = None,
    ) -> OpenedPullRequest:
        """Create the branch, commit the changed files, and open (or reuse) the pull request.

        `description`은 PR 본문에 덧붙는 검토용 요약(Finding 근거와 unified diff)이다. branch
        이름과 commit 내용에는 영향을 주지 않으므로 재전달의 수렴성은 그대로다.
        """
        patch = require_remediation_patch(patch)
        self._require_scope(patch)
        if not isinstance(changes, Mapping) or not changes:
            raise GitHubWriteToolError("changes must be a non-empty mapping")
        if tuple(sorted(changes)) != tuple(sorted(patch.changed_paths)):
            # patch가 선언한 파일 집합과 다른 내용을 올리면 저장된 identity와 PR이 어긋난다.
            raise GitHubWriteToolError("changes do not match the patch's changed paths")
        if description is not None and not isinstance(description, str):
            raise GitHubWriteToolError("description must be a string or None")
        proposal = _proposal(patch, self._repository_id, description)
        headers = _headers(self._token_provider())

        default_branch = _text(_mapping(self._get(self._repo_url(), headers)), "default_branch")
        self._ensure_branch(proposal.head_branch, patch.base_commit_sha, headers)
        for path in proposal.changed_paths:
            self._put_file(
                path=path,
                contents=changes[path],
                branch=proposal.head_branch,
                message=f"{proposal.title} ({path})",
                headers=headers,
            )
        pull = self._existing_pull(proposal.head_branch, headers) or self._create_pull(
            proposal, default_branch, headers
        )
        head_sha = self._branch_head(proposal.head_branch, headers)
        return OpenedPullRequest(
            customer_id=self._customer_id,
            repository_id=self._repository_id,
            finding_id=patch.finding_id,
            head_branch=proposal.head_branch,
            head_commit_sha=head_sha,
            base_branch=default_branch,
            number=_number(pull.get("number")),
            url=_text(pull, "html_url"),
        )

    # --- GitHub steps -------------------------------------------------------------------

    def _ensure_branch(self, branch: str, base_commit_sha: str, headers: Mapping[str, str]) -> None:
        status, payload = self._request(
            "GET", f"{self._repo_url()}/git/ref/heads/{quote(branch, safe='/')}", headers, None
        )
        if status == 200:
            return  # 재시도: 같은 patch의 branch가 이미 있다.
        if status != 404:
            raise GitHubWriteToolError("GitHub branch lookup failed")
        status, payload = self._request(
            "POST",
            f"{self._repo_url()}/git/refs",
            headers,
            _json({"ref": f"refs/heads/{branch}", "sha": base_commit_sha}),
        )
        if status == 422:
            # 조회와 생성 사이에 다른 재시도가 만들었다. 같은 이름·같은 base이므로 그대로 쓴다.
            return
        if status != 201:
            raise GitHubWriteToolError("GitHub branch creation failed")

    def _put_file(
        self, *, path: str, contents: str, branch: str, message: str, headers: Mapping[str, str]
    ) -> None:
        encoded_path = "/".join(quote(part, safe="") for part in path.split("/"))
        url = f"{self._repo_url()}/contents/{encoded_path}"
        status, payload = self._request("GET", f"{url}?ref={quote(branch, safe='')}", headers, None)
        existing_sha: str | None = None
        if status == 200:
            current = _mapping(payload)
            existing_sha = _text(current, "sha")
            if existing_sha == _git_blob_sha(contents):
                return  # 이미 같은 내용이 branch에 있다(재시도).
        elif status != 404:
            raise GitHubWriteToolError("GitHub file lookup failed")
        body: dict[str, object] = {
            "message": message,
            "content": base64.b64encode(contents.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if existing_sha is not None:
            body["sha"] = existing_sha
        status, _ = self._request("PUT", url, headers, _json(body))
        if status not in (200, 201):
            raise GitHubWriteToolError("GitHub file commit failed")

    def _existing_pull(
        self, branch: str, headers: Mapping[str, str]
    ) -> Mapping[str, object] | None:
        owner = self._repository_full_name.split("/", 1)[0]
        head = quote(f"{owner}:{branch}", safe="")
        payload = self._get(f"{self._repo_url()}/pulls?state=open&head={head}&per_page=5", headers)
        if not isinstance(payload, list):
            raise GitHubWriteToolError("GitHub pull request list is invalid")
        for entry in payload:
            pull = _mapping(entry)
            head_ref = _mapping(pull.get("head")).get("ref")
            if head_ref == branch:
                return pull
        return None

    def _create_pull(
        self, proposal: ProposedPullRequest, base_branch: str, headers: Mapping[str, str]
    ) -> Mapping[str, object]:
        status, payload = self._request(
            "POST",
            f"{self._repo_url()}/pulls",
            headers,
            _json(
                {
                    "title": proposal.title,
                    "body": proposal.body,
                    "head": proposal.head_branch,
                    "base": base_branch,
                }
            ),
        )
        if status != 201:
            raise GitHubWriteToolError("GitHub pull request creation failed")
        return _mapping(payload)

    def _branch_head(self, branch: str, headers: Mapping[str, str]) -> str:
        payload = self._get(f"{self._repo_url()}/git/ref/heads/{quote(branch, safe='/')}", headers)
        return _text(_mapping(_mapping(payload).get("object")), "sha")

    # --- helpers ------------------------------------------------------------------------

    def _get(self, url: str, headers: Mapping[str, str]) -> object:
        status, payload = self._request("GET", url, headers, None)
        if status != 200:
            raise GitHubWriteToolError("GitHub read failed")
        return payload

    def _repo_url(self) -> str:
        return f"https://api.github.com/repos/{self._repository_full_name}"

    def _require_scope(self, patch: RemediationPatch) -> None:
        require_patch_scope(patch, customer_id=self._customer_id, repository_id=self._repository_id)


def _proposal(
    patch: RemediationPatch, repository_id: str, description: str | None = None
) -> ProposedPullRequest:
    body = (
        f"Automated remediation proposal for finding {patch.finding_id}.\n"
        f"Base commit: {patch.base_commit_sha}\n"
        f"Patch digest: {patch.artifact.content_sha256}\n"
        f"Changed paths: {', '.join(patch.changed_paths)}\n\n"
        "Review and merge to make this commit the deployment target; nothing is applied "
        "until a person approves the refreshed plan."
    )
    if description:
        body = f"{body}\n\n{description}"
    return ProposedPullRequest(
        customer_id=patch.artifact.customer_id,
        repository_id=repository_id,
        finding_id=patch.finding_id,
        base_commit_sha=patch.base_commit_sha,
        head_branch=derive_head_branch(patch),
        title=f"Remediation for {patch.finding_id}",
        body=body,
        changed_paths=patch.changed_paths,
    )


def _git_blob_sha(contents: str) -> str:
    """The SHA-1 GitHub reports for a blob: `blob {size}\\0{bytes}`."""
    raw = contents.encode("utf-8")
    return hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()


def _headers(token: object) -> dict[str, str]:
    if not isinstance(token, str) or not token.strip():
        raise GitHubWriteToolError("GitHub token is invalid")
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }


def _json(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _github_request(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> tuple[int, object]:
    request = Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urlopen(request, timeout=15) as response:  # noqa: S310 - fixed GitHub API origin.
            status = getattr(response, "status", response.getcode())
            raw = response.read()
    except HTTPError as error:
        # 404(없음)와 422(이미 있음)는 흐름의 일부다. 나머지는 호출자가 status로 판단한다.
        return error.code, None
    except (URLError, TimeoutError) as error:
        raise GitHubWriteToolError("GitHub request failed") from error
    if not raw:
        return status, None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GitHubWriteToolError("GitHub response is not JSON") from None


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise GitHubWriteToolError("GitHub response is invalid")
    return value


def _text(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise GitHubWriteToolError(f"GitHub response {key} is invalid")
    return item


def _number(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GitHubWriteToolError("GitHub pull request number is invalid")
    return value
