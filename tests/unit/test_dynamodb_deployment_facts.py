"""Deployment 상태 파생 입력 reader 테스트 (ADR-0019 §8).

고정하는 불변식:
- 하위 item(`#APPROVAL#`/`#REJECTION`/`#DISPATCH`/`#EVENT#`)은 SK prefix query 한 번에 읽는다.
- apply 결론은 D가 재조회로 확정한 `VERIFIED` EVENT item에서만 온다. dispatch 영수증과 예약된
  `PENDING_VERIFICATION` item은 "실행 중"일 뿐 결론이 아니다.
- 거절은 terminal이라 승인 표시를 이긴다.
- readiness는 근거가 없으면 `None`이다 — 추측한 `READY_FOR_APPROVAL`은 C가 막았을 plan을
  "승인 대기"로 보여준다.
"""

import unittest

from apps.backend.deployment.record import DeploymentRecord
from apps.backend.repositories.deployment_facts import DynamoDbDeploymentFactsReader
from apps.backend.repositories.ports import StoredDataError
from packages.contracts import (
    ApplyOutcome,
    ArtifactReference,
    ArtifactType,
    DeploymentReadinessSignal,
    DeploymentStatus,
    JobCurrentStep,
    JobStatus,
    TerraformStateVersion,
    VerificationOutcome,
    derive_deployment_status,
)

CUSTOMER_ID = "cust-001"
DEPLOYMENT_ID = "dep-001"
JOB_ID = "job-001"
RUN_ID = "run-001"
COMMIT = "a" * 40
PLAN_HASH = "b" * 64


class FakeJob:
    def __init__(
        self,
        status: JobStatus = JobStatus.RUNNING,
        current_step: JobCurrentStep = JobCurrentStep.TERRAFORM_PLAN,
    ) -> None:
        self.status = status
        self.current_step = current_step


def _record(*, with_plan: bool = False, verification_assessment_id: str | None = None):
    plan_fields: dict[str, object] = {}
    if with_plan:
        artifact = ArtifactReference(
            artifact_id="plan-1",
            artifact_type=ArtifactType.TERRAFORM_PLAN,
            content_sha256=PLAN_HASH,
            customer_id=CUSTOMER_ID,
            repository_id="repo-001",
        )
        plan_fields = {
            "plan_hash": PLAN_HASH,
            "plan_artifact": artifact,
            "binary_artifact": ArtifactReference(
                artifact_id="plan-bin-1",
                artifact_type=ArtifactType.TERRAFORM_PLAN_BINARY,
                content_sha256="d" * 64,
                customer_id=CUSTOMER_ID,
                repository_id="repo-001",
            ),
            "state_version": TerraformStateVersion(lineage="lin-1", serial=3),
        }
    return DeploymentRecord(
        deployment_id=DEPLOYMENT_ID,
        customer_id=CUSTOMER_ID,
        repository_id="repo-001",
        job_id=JOB_ID,
        remediation_id="rem-001",
        commit_sha=COMMIT,
        source_assessment_id="asm-source",
        verification_assessment_id=verification_assessment_id,
        **plan_fields,  # type: ignore[arg-type]
    )


def _sk(suffix: str = "") -> str:
    return f"DEPLOYMENT#{DEPLOYMENT_ID}{suffix}"


class FakeTable:
    def __init__(self, items: list[dict[str, object]]) -> None:
        self.items = items
        self.calls: list[dict[str, object]] = []

    def query(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"Items": self.items}


class FakeDeployments:
    def __init__(self, record: object) -> None:
        self.record = record

    def get_deployment(self, *, customer_id: str, deployment_id: str):
        return self.record


class FakeJobs:
    def __init__(self, job: object) -> None:
        self.job = job

    def get_job(self, customer_id: str, job_id: str):
        return self.job


class FakeReadiness:
    def __init__(self, signal: object) -> None:
        self.signal = signal

    def get_readiness_signal(self, *, customer_id: str, deployment_id: str):
        return self.signal


_DEFAULT = object()


def _reader(
    items: list[dict[str, object]],
    *,
    record: object = _DEFAULT,
    job: object = _DEFAULT,
    readiness: object | None = None,
    comparisons: object | None = None,
) -> DynamoDbDeploymentFactsReader:
    return DynamoDbDeploymentFactsReader(
        FakeTable(items),
        deployments=FakeDeployments(_record() if record is _DEFAULT else record),
        jobs=FakeJobs(FakeJob() if job is _DEFAULT else job),
        comparisons=comparisons,
        readiness=readiness,
    )


def _facts(reader: DynamoDbDeploymentFactsReader):
    return reader.get_deployment_facts(customer_id=CUSTOMER_ID, deployment_id=DEPLOYMENT_ID)


class DeploymentFactsReaderTest(unittest.TestCase):
    def test_reads_the_deployment_prefix_in_one_consistent_query(self) -> None:
        table = FakeTable([{"SK": _sk()}])
        reader = DynamoDbDeploymentFactsReader(
            table, deployments=FakeDeployments(_record()), jobs=FakeJobs(FakeJob())
        )
        _facts(reader)
        call = table.calls[0]
        self.assertEqual(call["KeyConditionExpression"], "PK = :pk AND begins_with(SK, :prefix)")
        self.assertEqual(
            call["ExpressionAttributeValues"],
            {":pk": f"CUSTOMER#{CUSTOMER_ID}", ":prefix": _sk()},
        )
        self.assertIs(call["ConsistentRead"], True)

    def test_a_fresh_deployment_has_no_apply_and_no_verification(self) -> None:
        facts = _facts(_reader([{"SK": _sk()}]))
        self.assertIs(facts.apply_outcome, ApplyOutcome.NOT_STARTED)
        self.assertIs(facts.verification_outcome, VerificationOutcome.NOT_STARTED)
        self.assertFalse(facts.is_approved)
        self.assertFalse(facts.is_rejected)
        self.assertIsNone(facts.readiness)
        self.assertIs(derive_deployment_status(facts), DeploymentStatus.PLAN_REQUESTED)

    def test_an_approval_item_marks_the_deployment_approved(self) -> None:
        facts = _facts(_reader([{"SK": _sk()}, {"SK": _sk("#APPROVAL#app-1")}]))
        self.assertTrue(facts.is_approved)
        self.assertIs(derive_deployment_status(facts), DeploymentStatus.APPROVED)

    def test_a_dispatch_receipt_alone_is_only_running(self) -> None:
        """영수증은 workflow를 시작해달라고 했다는 사실일 뿐 결론이 아니다."""
        facts = _facts(_reader([{"SK": _sk()}, {"SK": _sk("#DISPATCH")}]))
        self.assertIs(facts.apply_outcome, ApplyOutcome.RUNNING)
        self.assertIs(derive_deployment_status(facts), DeploymentStatus.APPLYING)

    def test_a_reserved_event_is_not_yet_a_conclusion(self) -> None:
        """`PENDING_VERIFICATION`은 재조회할 좌표 포인터이지 검증된 사실이 아니다."""
        items = [
            {"SK": _sk()},
            {"SK": _sk("#DISPATCH")},
            {"SK": _sk(f"#EVENT#{RUN_ID}"), "status": "PENDING_VERIFICATION"},
        ]
        self.assertIs(_facts(_reader(items)).apply_outcome, ApplyOutcome.RUNNING)

    def test_a_verified_successful_run_completes_the_apply(self) -> None:
        items = [
            {"SK": _sk()},
            {"SK": _sk("#DISPATCH")},
            {"SK": _sk(f"#EVENT#{RUN_ID}"), "status": "VERIFIED", "conclusion": "SUCCESS"},
        ]
        facts = _facts(_reader(items))
        self.assertIs(facts.apply_outcome, ApplyOutcome.SUCCEEDED)
        self.assertIs(derive_deployment_status(facts), DeploymentStatus.APPLIED)

    def test_a_verified_failed_run_routes_to_manual_review(self) -> None:
        items = [
            {"SK": _sk()},
            {"SK": _sk("#DISPATCH")},
            {"SK": _sk(f"#EVENT#{RUN_ID}"), "status": "VERIFIED", "conclusion": "FAILURE"},
        ]
        facts = _facts(_reader(items))
        self.assertIs(facts.apply_outcome, ApplyOutcome.FAILED)
        self.assertIs(derive_deployment_status(facts), DeploymentStatus.MANUAL_REVIEW)

    def test_an_invalid_verified_conclusion_fails_closed(self) -> None:
        items = [
            {"SK": _sk()},
            {"SK": _sk(f"#EVENT#{RUN_ID}"), "status": "VERIFIED", "conclusion": "MAYBE"},
        ]
        with self.assertRaises(StoredDataError):
            _facts(_reader(items))

    def test_rejection_wins_over_an_approval_item(self) -> None:
        items = [{"SK": _sk()}, {"SK": _sk("#APPROVAL#app-1")}, {"SK": _sk("#REJECTION")}]
        facts = _facts(_reader(items))
        self.assertTrue(facts.is_rejected)
        self.assertFalse(facts.is_approved)
        self.assertIs(derive_deployment_status(facts), DeploymentStatus.REJECTED)

    def test_readiness_needs_both_a_plan_and_a_reader(self) -> None:
        blocked = FakeReadiness(DeploymentReadinessSignal.BLOCKED)
        # plan facts가 없으면 판단할 plan 자체가 없다.
        self.assertIsNone(_facts(_reader([{"SK": _sk()}], readiness=blocked)).readiness)
        # reader가 없으면 근거가 없다 — READY_FOR_APPROVAL로 추측하지 않는다.
        self.assertIsNone(
            _facts(_reader([{"SK": _sk()}], record=_record(with_plan=True))).readiness
        )

    def test_readiness_signal_is_passed_through(self) -> None:
        facts = _facts(
            _reader(
                [{"SK": _sk()}],
                record=_record(with_plan=True),
                readiness=FakeReadiness(DeploymentReadinessSignal.BLOCKED),
            )
        )
        self.assertIs(facts.readiness, DeploymentReadinessSignal.BLOCKED)
        self.assertIs(derive_deployment_status(facts), DeploymentStatus.BLOCKED)

    def test_a_verification_without_a_comparison_reader_is_running(self) -> None:
        facts = _facts(
            _reader(
                [{"SK": _sk()}, {"SK": _sk(f"#EVENT#{RUN_ID}")}],
                record=_record(verification_assessment_id="asm-verify"),
            )
        )
        self.assertIs(facts.verification_outcome, VerificationOutcome.RUNNING)

    def test_a_missing_job_is_corrupt_storage(self) -> None:
        with self.assertRaises(StoredDataError):
            _facts(_reader([{"SK": _sk()}], job=None))

    def test_a_missing_deployment_is_reported(self) -> None:
        with self.assertRaises(StoredDataError):
            _facts(_reader([{"SK": _sk()}], record=None))


if __name__ == "__main__":
    unittest.main()
