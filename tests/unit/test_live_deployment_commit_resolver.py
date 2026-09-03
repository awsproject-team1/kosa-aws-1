"""D의 default-branch commit 해석 adapter 테스트 (ADR-0019 §3·§4).

고정하는 불변식:
- default branch 이름은 설정이 아니라 repository에서 읽는다.
- merge된 PR의 `merge_commit_sha`만 대상이 되고, 열린 PR은 대상이 아니다.
- merge 뒤 default branch에서 사라진 commit(revert/force-push)은 도달 불가다.
- 승인된 (customer, repository) scope 밖 요청은 조용히 처리하지 않는다.
"""

import unittest

from agent.runtime.github_write_tool import derive_head_branch
from agent.runtime.live_deployment_commit_resolver import (
    DeploymentCommitResolverError,
    LiveDeploymentCommitResolver,
)
from packages.contracts import ArtifactReference, ArtifactType, RemediationPatch

CUSTOMER_ID = "cust-001"
REPOSITORY_ID = "repo-001"
FULL_NAME = "customer/iac"
BASE_COMMIT = "a" * 40
MERGE_COMMIT = "e" * 40


def _patch(*, customer_id: str = CUSTOMER_ID, repository_id: str = REPOSITORY_ID):
    return RemediationPatch(
        finding_id="find-001",
        base_commit_sha=BASE_COMMIT,
        artifact=ArtifactReference(
            artifact_id="patch-1",
            artifact_type=ArtifactType.REMEDIATION_PATCH,
            content_sha256="c" * 64,
            customer_id=customer_id,
            repository_id=repository_id,
        ),
        changed_paths=("main.tf",),
    )


class FakeGitHub:
    def __init__(self, *, pulls: list[dict[str, object]], status: str = "behind") -> None:
        self.pulls = pulls
        self.status = status
        self.urls: list[str] = []

    def __call__(self, url: str, headers):
        self.urls.append(url)
        if url.endswith(f"/repos/{FULL_NAME}"):
            return {"default_branch": "main"}
        if "/pulls" in url:
            return self.pulls
        if "/compare/" in url:
            return {"status": self.status}
        raise AssertionError(f"unexpected url {url}")


def _resolver(github: FakeGitHub) -> LiveDeploymentCommitResolver:
    return LiveDeploymentCommitResolver(
        customer_id=CUSTOMER_ID,
        repository_id=REPOSITORY_ID,
        repository_full_name=FULL_NAME,
        token_provider=lambda: "token",
        request=github,
    )


def _merged_pull() -> dict[str, object]:
    return {"merged_at": "2026-09-02T10:00:00Z", "merge_commit_sha": MERGE_COMMIT}


def _resolve(resolver: LiveDeploymentCommitResolver, patch=None):
    return resolver.resolve_default_branch_commit(
        customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID, patch=patch or _patch()
    )


class LiveDeploymentCommitResolverTest(unittest.TestCase):
    def test_returns_the_merge_commit_of_a_merged_pull_request(self) -> None:
        github = FakeGitHub(pulls=[_merged_pull()])
        self.assertEqual(_resolve(_resolver(github)), MERGE_COMMIT)

    def test_reads_the_default_branch_from_the_repository(self) -> None:
        """설정된 이름을 믿지 않는다 — 어긋나면 엉뚱한 branch를 기준으로 판정한다."""
        github = FakeGitHub(pulls=[_merged_pull()])
        _resolve(_resolver(github))
        self.assertIn(f"https://api.github.com/repos/{FULL_NAME}", github.urls)
        self.assertTrue(any("/compare/main..." in url for url in github.urls))

    def test_looks_up_the_pull_request_by_the_deterministic_head_branch(self) -> None:
        github = FakeGitHub(pulls=[_merged_pull()])
        patch = _patch()
        _resolve(_resolver(github), patch)
        branch = derive_head_branch(patch)
        pulls_url = next(url for url in github.urls if "/pulls" in url)
        self.assertIn(branch.replace("/", "%2F"), pulls_url)

    def test_an_open_pull_request_is_not_a_target(self) -> None:
        github = FakeGitHub(pulls=[{"merged_at": None, "merge_commit_sha": MERGE_COMMIT}])
        self.assertIsNone(_resolve(_resolver(github)))

    def test_no_pull_request_is_not_a_target(self) -> None:
        self.assertIsNone(_resolve(_resolver(FakeGitHub(pulls=[]))))

    def test_identical_head_counts_as_reachable(self) -> None:
        github = FakeGitHub(pulls=[_merged_pull()], status="identical")
        self.assertEqual(_resolve(_resolver(github)), MERGE_COMMIT)

    def test_a_reverted_merge_is_no_longer_reachable(self) -> None:
        """merge 뒤 default branch가 되돌려지면 그 commit은 배포 대상이 아니다."""
        for status in ("ahead", "diverged"):
            github = FakeGitHub(pulls=[_merged_pull()], status=status)
            self.assertIsNone(_resolve(_resolver(github)))

    def test_a_merged_pull_request_without_a_merge_commit_fails_closed(self) -> None:
        github = FakeGitHub(pulls=[{"merged_at": "2026-09-02T10:00:00Z"}])
        with self.assertRaises(DeploymentCommitResolverError):
            _resolve(_resolver(github))

    def test_rejects_a_request_outside_the_approved_scope(self) -> None:
        resolver = _resolver(FakeGitHub(pulls=[_merged_pull()]))
        with self.assertRaises(DeploymentCommitResolverError):
            resolver.resolve_default_branch_commit(
                customer_id="cust-002", repository_id=REPOSITORY_ID, patch=_patch()
            )
        with self.assertRaises(DeploymentCommitResolverError):
            resolver.resolve_default_branch_commit(
                customer_id=CUSTOMER_ID, repository_id="repo-999", patch=_patch()
            )

    def test_rejects_a_patch_outside_the_approved_scope(self) -> None:
        resolver = _resolver(FakeGitHub(pulls=[_merged_pull()]))
        with self.assertRaises(DeploymentCommitResolverError):
            _resolve(resolver, _patch(customer_id="cust-002"))

    def test_requires_a_token(self) -> None:
        resolver = LiveDeploymentCommitResolver(
            customer_id=CUSTOMER_ID,
            repository_id=REPOSITORY_ID,
            repository_full_name=FULL_NAME,
            token_provider=lambda: "",
            request=FakeGitHub(pulls=[]),
        )
        with self.assertRaises(DeploymentCommitResolverError):
            _resolve(resolver)


if __name__ == "__main__":
    unittest.main()
