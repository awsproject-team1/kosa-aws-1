"""Assessment creation pins the Profile version, and the two scopes stay separate.

두 경계가 서로 다른 질문에 답한다.

    Runtime configuration — 이 고객이 어떤 Repository와 AWS Resource를 읽을 수 있는가
    DynamoDB Policy Catalog — 이 고객이 어떤 게시된 Policy Profile을 쓸 수 있는가

Profile을 배포 구성에 묶으면, 고객이 정책을 승인·게시할 때마다 인프라 배포가 필요해진다 —
"승인 직후 평가에 쓸 수 있다"는 목표와 충돌한다.

그리고 **판본은 생성 시점에 고정된다.** 고정하지 않으면 실행 도중 게시된 새 Profile이 이미
계획된 평가의 Rule 집합을 바꾸고, 그 사실이 결과 어디에도 남지 않는다.
"""

import json
import unittest

from apps.backend.api.jobs import (
    AssessmentRequest,
    JobApiService,
    PolicyProfileNotPublished,
)
from apps.backend.assessment import Assessment
from apps.backend.assessment.runtime_config import (
    M1RuntimeConfiguration,
    M1RuntimeConfigurationError,
)
from apps.backend.auth import Principal, Role
from apps.backend.jobs import OutboxDispatcher, OutboxStatus, WorkflowOutboxEntry
from packages.contracts import AssessmentPhase, PolicyProfile, PolicyRuleReference

TARGET = {
    "customer_id": "cust-001",
    "repository_id": "repo-001",
    "commit_sha": "a" * 40,
    "github_repository": "customer/iac",
    "github_token_secret_id": "github-token",
    "aws_account_id": "123456789012",
    "aws_read_role_arn": "arn:aws:iam::123456789012:role/Read",
    "aws_external_id_secret_id": "external-id",
    "s3_bucket_id": "customer-test-bucket",
}


class Repository:
    def __init__(self) -> None:
        self.assessments: list[Assessment] = []
        self.outbox: list[WorkflowOutboxEntry] = []

    def create_assessment_workflow(self, assessment, job, outbox) -> None:
        self.assessments.append(assessment)
        self.outbox.append(outbox)

    def list_pending_outbox(self, *, limit: int) -> tuple[WorkflowOutboxEntry, ...]:
        return tuple(entry for entry in self.outbox if entry.status is OutboxStatus.PENDING)[:limit]

    def mark_outbox_dispatched(self, entry: WorkflowOutboxEntry) -> None:
        return None

    def record_outbox_dispatch_failure(self, entry: WorkflowOutboxEntry) -> None:
        return None


class Dispatcher:
    def dispatch(self, task) -> None:
        return None


class ApprovedRepositories:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def authorize(self, principal, *, repository_id: str) -> None:
        self.calls.append((principal.customer_id, repository_id))


class Catalog:
    """A tenant-scoped Profile reader that records which partition it was asked about."""

    def __init__(self, *, version: str | None = "v3") -> None:
        self.version = version
        self.customers: list[str] = []

    def __call__(self, *, customer_id: str) -> "Catalog":
        self.customers.append(customer_id)
        return self

    def get_profile(self, policy_profile_id: str, version: str | None = None):
        if self.version is None:
            return None
        return PolicyProfile(
            policy_profile_id=policy_profile_id,
            version=self.version,
            rule_references=(PolicyRuleReference(rule_id="CUST-RULE-1", version="2026-09-01"),),
        )


def _principal(customer_id: str = "cust-001") -> Principal:
    return Principal(
        subject="user-001",
        client_id="client-001",
        customer_id=customer_id,
        roles=frozenset({Role.USER}),
    )


def _service(catalog: Catalog, scope: ApprovedRepositories | None = None) -> JobApiService:
    repository = Repository()
    service = JobApiService(
        repository=repository,
        assessment_scope=scope or ApprovedRepositories(),
        policy_catalog_factory=catalog,
        outbox_dispatcher=OutboxDispatcher(repository=repository, dispatcher=Dispatcher()),
        job_id_factory=lambda: "job-001",
        assessment_id_factory=lambda: "asm-001",
    )
    service._recorded = repository  # type: ignore[attr-defined]
    return service


class ProfileVersionPinTest(unittest.TestCase):
    def test_creation_pins_the_current_profile_version(self) -> None:
        catalog = Catalog(version="v3")
        service = _service(catalog)

        service.create_assessment(
            _principal(),
            AssessmentRequest(repository_id="repo-001", policy_profile_id="profile-baseline"),
        )

        assessment = service._recorded.assessments[0]  # type: ignore[attr-defined]
        self.assertEqual(assessment.policy_profile_version, "v3")
        self.assertEqual(assessment.policy_profile_id, "profile-baseline")
        self.assertIs(assessment.phase, AssessmentPhase.INITIAL)

    def test_an_unpublished_profile_is_refused_at_creation(self) -> None:
        """게시되지 않은 Profile로 만들면 worker가 Rule을 못 찾아 실행 단계에서 실패한다.

        그 실패는 "정책 위반 없음"과 구별하기 어렵다. 생성 단계에서 거절한다.
        """
        service = _service(Catalog(version=None))

        with self.assertRaises(PolicyProfileNotPublished):
            service.create_assessment(
                _principal(),
                AssessmentRequest(repository_id="repo-001", policy_profile_id="profile-baseline"),
            )

        self.assertEqual(service._recorded.assessments, [])  # type: ignore[attr-defined]

    def test_the_profile_is_read_from_the_callers_own_partition(self) -> None:
        catalog = Catalog()
        service = _service(catalog)

        service.create_assessment(
            _principal("cust-002"),
            AssessmentRequest(repository_id="repo-001", policy_profile_id="profile-baseline"),
        )

        self.assertEqual(catalog.customers, ["cust-002"])


class ScopeSeparationTest(unittest.TestCase):
    def test_the_repository_scope_no_longer_decides_the_profile(self) -> None:
        scope = ApprovedRepositories()
        service = _service(Catalog(), scope)

        service.create_assessment(
            _principal(),
            AssessmentRequest(repository_id="repo-001", policy_profile_id="profile-baseline"),
        )

        self.assertEqual(scope.calls, [("cust-001", "repo-001")])

    def test_the_runtime_configuration_resolves_on_repository_alone(self) -> None:
        configuration = M1RuntimeConfiguration.from_json(json.dumps([TARGET]))

        target = configuration.resolve(customer_id="cust-001", repository_id="repo-001")

        self.assertEqual(target.commit_sha, "a" * 40)
        self.assertFalse(hasattr(target, "policy_profile_id"))

    def test_a_deployment_target_that_still_names_a_profile_fails_closed(self) -> None:
        """배포 JSON에 Profile이 남아 있으면 두 경계가 서로 다른 것을 말하게 된다."""
        with self.assertRaises(M1RuntimeConfigurationError):
            M1RuntimeConfiguration.from_json(
                json.dumps([{**TARGET, "policy_profile_id": "profile-baseline"}])
            )

    def test_an_unapproved_repository_is_outside_runtime_scope(self) -> None:
        configuration = M1RuntimeConfiguration.from_json(json.dumps([TARGET]))

        with self.assertRaisesRegex(M1RuntimeConfigurationError, "outside M1 runtime scope"):
            configuration.resolve(customer_id="cust-001", repository_id="repo-other")


if __name__ == "__main__":
    unittest.main()
