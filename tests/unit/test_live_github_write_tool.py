"""The live GitHub write adapter opens exactly one branch and pull request per patch.

고정하는 불변식 (ADR-0007, ADR-0019 §3·§6):
- write는 branch ref 생성, 파일 contents 갱신, pull request 생성 세 가지뿐이다. merge·apply·workflow
  파일 변경은 없다.
- 재전달은 같은 branch 이름으로 수렴한다: 있는 ref는 재사용, 같은 blob은 다시 commit하지 않으며,
  열린 PR이 있으면 그것을 돌려준다.
- scope 밖의 patch, patch가 선언한 파일 집합과 다른 changes는 거부한다.
"""

import base64
import json
import unittest
from collections.abc import Mapping
from urllib.parse import unquote

from agent.runtime.github_write_tool import (
    GitHubWriteScopeError,
    GitHubWriteToolError,
    OpenedPullRequest,
    derive_head_branch,
)
from agent.runtime.live_github_write_tool import LiveGitHubWriteTool, _git_blob_sha
from packages.contracts import ArtifactReference, ArtifactType, RemediationPatch

CUSTOMER = "cust-001"
REPOSITORY_ID = "repo-001"
REPO = "acme/iac"
BASE_COMMIT = "a" * 40
MAIN_TF = 'resource "aws_s3_bucket_public_access_block" "x" { block_public_acls = true }\n'


def _patch(paths: tuple[str, ...] = ("main.tf",)) -> RemediationPatch:
    return RemediationPatch(
        finding_id="finding-abc",
        base_commit_sha=BASE_COMMIT,
        artifact=ArtifactReference(
            artifact_id="remediation-patch:repo-001:finding-abc:" + "d" * 64,
            artifact_type=ArtifactType.REMEDIATION_PATCH,
            content_sha256="d" * 64,
            customer_id=CUSTOMER,
            repository_id=REPOSITORY_ID,
        ),
        changed_paths=paths,
    )


class FakeGitHub:
    """A tiny GitHub REST model: refs, file contents per branch, pull requests."""

    def __init__(self, *, existing_files: Mapping[str, str] | None = None) -> None:
        self.default_branch = "main"
        self.refs: dict[str, str] = {}
        self.files: dict[tuple[str, str], str] = {}
        for path, contents in (existing_files or {}).items():
            self.files[(self.default_branch, path)] = contents
        self.pulls: list[dict[str, object]] = []
        self.calls: list[tuple[str, str]] = []
        self.commits = 0

    def __call__(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None):
        self.calls.append((method, url))
        assert headers["Authorization"] == "Bearer token-1"
        prefix = f"https://api.github.com/repos/{REPO}"
        assert url.startswith(prefix), url
        path = url[len(prefix) :]
        if path == "":
            return 200, {"default_branch": self.default_branch}
        if path.startswith("/git/ref/heads/"):
            branch = unquote(path[len("/git/ref/heads/") :])
            sha = self.refs.get(branch)
            return (404, None) if sha is None else (200, {"object": {"sha": sha}})
        if path == "/git/refs" and method == "POST":
            payload = json.loads(body)
            branch = payload["ref"].removeprefix("refs/heads/")
            if branch in self.refs:
                return 422, {"message": "Reference already exists"}
            self.refs[branch] = payload["sha"]
            # 새 branch는 default branch의 파일을 물려받는다.
            for (existing_branch, file_path), contents in list(self.files.items()):
                if existing_branch == self.default_branch:
                    self.files[(branch, file_path)] = contents
            return 201, {"ref": payload["ref"]}
        if path.startswith("/contents/"):
            file_path, _, query = path[len("/contents/") :].partition("?")
            file_path = unquote(file_path)
            if method == "GET":
                branch = unquote(query.removeprefix("ref="))
                contents = self.files.get((branch, file_path))
                if contents is None:
                    return 404, None
                return 200, {"sha": _git_blob_sha(contents)}
            payload = json.loads(body)
            branch = payload["branch"]
            contents = base64.b64decode(payload["content"]).decode("utf-8")
            existing = self.files.get((branch, file_path))
            if existing is not None and payload.get("sha") != _git_blob_sha(existing):
                return 409, {"message": "sha mismatch"}
            self.files[(branch, file_path)] = contents
            self.commits += 1
            self.refs[branch] = f"{self.commits:040x}"
            return 200 if existing is not None else 201, {"content": {"path": file_path}}
        if path.startswith("/pulls?"):
            return 200, [pull for pull in self.pulls if pull["state"] == "open"]
        if path == "/pulls" and method == "POST":
            payload = json.loads(body)
            number = len(self.pulls) + 1
            pull = {
                "number": number,
                "html_url": f"https://github.com/{REPO}/pull/{number}",
                "state": "open",
                "head": {"ref": payload["head"]},
                "base": {"ref": payload["base"]},
                "title": payload["title"],
            }
            self.pulls.append(pull)
            return 201, pull
        raise AssertionError(f"unexpected call {method} {url}")


def _tool(github: FakeGitHub) -> LiveGitHubWriteTool:
    return LiveGitHubWriteTool(
        customer_id=CUSTOMER,
        repository_id=REPOSITORY_ID,
        repository_full_name=REPO,
        token_provider=lambda: "token-1",
        request=github,
    )


class OpenPullRequestTest(unittest.TestCase):
    def test_creates_branch_commits_files_and_opens_a_pull_request(self) -> None:
        github = FakeGitHub(existing_files={"main.tf": "old\n"})
        patch = _patch()
        opened = _tool(github).open_pull_request(patch, {"main.tf": MAIN_TF})

        self.assertIsInstance(opened, OpenedPullRequest)
        branch = derive_head_branch(patch)
        self.assertEqual(opened.head_branch, branch)
        self.assertEqual(opened.base_branch, "main")
        self.assertEqual(opened.number, 1)
        self.assertEqual(github.files[(branch, "main.tf")], MAIN_TF)
        self.assertEqual(github.files[("main", "main.tf")], "old\n")  # default branch untouched
        self.assertEqual(github.pulls[0]["head"]["ref"], branch)
        self.assertEqual(opened.head_commit_sha, github.refs[branch])
        methods = [method for method, _ in github.calls]
        self.assertNotIn("DELETE", methods)
        self.assertFalse(any("/merge" in url or "workflows" in url for _, url in github.calls))

    def test_a_redelivery_reuses_the_branch_commit_and_pull_request(self) -> None:
        github = FakeGitHub(existing_files={"main.tf": "old\n"})
        tool = _tool(github)
        first = tool.open_pull_request(_patch(), {"main.tf": MAIN_TF})
        commits_after_first = github.commits
        second = tool.open_pull_request(_patch(), {"main.tf": MAIN_TF})
        self.assertEqual(first.number, second.number)
        self.assertEqual(len(github.pulls), 1)
        self.assertEqual(github.commits, commits_after_first)  # same blob: no new commit

    def test_a_new_file_is_created_without_a_prior_sha(self) -> None:
        github = FakeGitHub()
        opened = _tool(github).open_pull_request(
            _patch(("modules/s3/new.tf",)), {"modules/s3/new.tf": MAIN_TF}
        )
        self.assertEqual(github.files[(opened.head_branch, "modules/s3/new.tf")], MAIN_TF)

    def test_changes_must_match_the_patch_paths(self) -> None:
        with self.assertRaisesRegex(GitHubWriteToolError, "changed paths"):
            _tool(FakeGitHub()).open_pull_request(_patch(), {"other.tf": MAIN_TF})

    def test_a_patch_outside_the_scope_is_refused_before_any_call(self) -> None:
        github = FakeGitHub()
        tool = LiveGitHubWriteTool(
            customer_id="cust-other",
            repository_id=REPOSITORY_ID,
            repository_full_name=REPO,
            token_provider=lambda: "token-1",
            request=github,
        )
        with self.assertRaises(GitHubWriteScopeError):
            tool.open_pull_request(_patch(), {"main.tf": MAIN_TF})
        self.assertEqual(github.calls, [])

    def test_propose_does_not_call_github(self) -> None:
        github = FakeGitHub()
        proposal = _tool(github).propose_pull_request(_patch())
        self.assertEqual(proposal.head_branch, derive_head_branch(_patch()))
        self.assertEqual(github.calls, [])


class BlobShaTest(unittest.TestCase):
    def test_matches_git_hash_object(self) -> None:
        # `git hash-object` of "hello\n"
        self.assertEqual(_git_blob_sha("hello\n"), "ce013625030ba8dba906f756967f9e9ca394464a")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
