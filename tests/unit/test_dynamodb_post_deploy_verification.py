"""검증 Assessment 시작의 DynamoDB write·read 경계 테스트 (ADR-0020 §1·§3·§7).

고정하는 불변식:
- 검증 Assessment item, 다음 revision의 Job, ASSESS_RESOURCE outbox, Deployment record의
  `verification_assessment_id`가 **하나의** transaction으로 써진다.
- Assessment item은 새 SK에만 써지고(원 Assessment를 덮어쓰지 않는다), Job은 같은 revision에서만
  올라가며, record link는 한 번만 붙는다.
- 조건 실패는 `DuplicateJobError`로 드러나고 호출자가 흡수한다.
- 원 Assessment의 Model Profile·rubric은 item의 pin이 아니라 **결과**에서 파생한다.
"""

import unittest

from apps.backend.assessment.models import Assessment
from apps.backend.assessment.reporting import AssessmentReport
from apps.backend.jobs.models import Job
from apps.backend.jobs.outbox import WorkflowOutboxEntry
from apps.backend.repositories import (
    DynamoDbPostDeployVerificationStore,
    DynamoDbVerificationSourceReader,
)
from apps.backend.repositories.errors import DuplicateJobError, StoredDataError
from packages.contracts import (
    AssessmentCoverage,
    AssessmentPhase,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    JobCurrentStep,
    JobStatus,
    PlannedEvaluation,
    ScoringMode,
    WorkflowCommand,
    WorkflowTask,
)
from tests.unit.test_dynamodb_deployment_stores import Transactions

CUSTOMER_ID = "cust-001"
DEPLOYMENT_ID = "dep-001"
JOB_ID = "job-dep-1"
SOURCE_ASSESSMENT = "asm-source"
VERIFICATION_ASSESSMENT = "asm-verify"


def _assessment() -> Assessment:
    return Assessment(
        assessment_id=VERIFICATION_ASSESSMENT,
        customer_id=CUSTOMER_ID,
        job_id=JOB_ID,
        repository_id="repo-001",
        policy_profile_id="profile-mvp-baseline",
        policy_profile_version="v2",
        phase=AssessmentPhase.POST_DEPLOY_VERIFICATION,
        source_assessment_id=SOURCE_ASSESSMENT,
        deployment_id=DEPLOYMENT_ID,
        model_profile_id="assessment-nova-lite-m1-v2",
        rubric_version="m1-three-perspective-v1",
    )


def _job(revision: int = 2, assessment_id: str | None = VERIFICATION_ASSESSMENT) -> Job:
    return Job(
        job_id=JOB_ID,
        customer_id=CUSTOMER_ID,
        job_type="DEPLOYMENT",
        status=JobStatus.RUNNING,
        current_step=JobCurrentStep.POST_DEPLOY_VERIFICATION,
        requested_by="subject-001",
        revision=revision,
        assessment_id=assessment_id,
        deployment_id=DEPLOYMENT_ID,
    )


def _outbox(revision: int = 2, command: WorkflowCommand = WorkflowCommand.ASSESS_RESOURCE):
    return WorkflowOutboxEntry(
        customer_id=CUSTOMER_ID,
        job_id=JOB_ID,
        task=WorkflowTask(job_id=JOB_ID, expected_revision=revision, command=command),
    )


class PostDeployVerificationStoreTest(unittest.TestCase):
    def _store(self, transactions: Transactions) -> DynamoDbPostDeployVerificationStore:
        return DynamoDbPostDeployVerificationStore(
            table_name="metadata", transaction_client=transactions
        )

    def test_writes_assessment_job_outbox_and_record_link_in_one_transaction(self) -> None:
        transactions = Transactions()
        self._store(transactions).create_verification_assessment(
            assessment=_assessment(), job=_job(), expected_revision=1, outbox=_outbox()
        )
        items = transactions.calls[0]["TransactItems"]
        self.assertEqual(len(items), 4)

        assessment_put = items[0]["Put"]
        self.assertEqual(
            assessment_put["Item"]["SK"], {"S": f"ASSESSMENT#{VERIFICATION_ASSESSMENT}"}
        )
        self.assertEqual(assessment_put["ConditionExpression"], "attribute_not_exists(SK)")
        self.assertEqual(assessment_put["Item"]["phase"], {"S": "POST_DEPLOY_VERIFICATION"})
        self.assertEqual(assessment_put["Item"]["source_assessment_id"], {"S": SOURCE_ASSESSMENT})
        self.assertEqual(
            assessment_put["Item"]["model_profile_id"], {"S": "assessment-nova-lite-m1-v2"}
        )

        job_put = items[1]["Put"]
        self.assertEqual(job_put["ConditionExpression"], "#revision = :expected")
        self.assertEqual(job_put["ExpressionAttributeValues"][":expected"], {"N": "1"})
        self.assertEqual(job_put["Item"]["assessment_id"], {"S": VERIFICATION_ASSESSMENT})

        outbox_put = items[2]["Put"]
        self.assertEqual(outbox_put["Item"]["command"], {"S": "ASSESS_RESOURCE"})
        self.assertEqual(outbox_put["Item"]["expected_revision"], {"N": "2"})
        self.assertNotIn("ConditionExpression", outbox_put)  # Job당 한 칸, 단계마다 overwrite

        link = items[3]["Update"]
        self.assertEqual(link["Key"]["SK"], {"S": f"DEPLOYMENT#{DEPLOYMENT_ID}"})
        self.assertIn(
            "attribute_not_exists(verification_assessment_id)", link["ConditionExpression"]
        )
        self.assertEqual(
            link["ExpressionAttributeValues"][":assessment_id"], {"S": VERIFICATION_ASSESSMENT}
        )

    def test_a_conditional_failure_is_a_duplicate_start(self) -> None:
        with self.assertRaises(DuplicateJobError):
            self._store(Transactions(fail=True)).create_verification_assessment(
                assessment=_assessment(), job=_job(), expected_revision=1, outbox=_outbox()
            )

    def test_rejects_a_job_that_does_not_point_at_the_assessment(self) -> None:
        with self.assertRaises(ValueError):
            self._store(Transactions()).create_verification_assessment(
                assessment=_assessment(),
                job=_job(assessment_id="asm-other"),
                expected_revision=1,
                outbox=_outbox(),
            )

    def test_rejects_a_job_more_than_one_revision_ahead(self) -> None:
        with self.assertRaises(ValueError):
            self._store(Transactions()).create_verification_assessment(
                assessment=_assessment(),
                job=_job(revision=3),
                expected_revision=1,
                outbox=_outbox(3),
            )

    def test_rejects_an_outbox_that_is_not_the_assessment_task(self) -> None:
        with self.assertRaises(ValueError):
            self._store(Transactions()).create_verification_assessment(
                assessment=_assessment(),
                job=_job(),
                expected_revision=1,
                outbox=_outbox(command=WorkflowCommand.RUN_DEPLOYMENT),
            )

    def test_rejects_an_initial_assessment(self) -> None:
        initial = Assessment(
            assessment_id=VERIFICATION_ASSESSMENT,
            customer_id=CUSTOMER_ID,
            job_id=JOB_ID,
            repository_id="repo-001",
            policy_profile_id="profile-mvp-baseline",
            policy_profile_version="v2",
        )
        with self.assertRaises(ValueError):
            self._store(Transactions()).create_verification_assessment(
                assessment=initial, job=_job(), expected_revision=1, outbox=_outbox()
            )


PLANNED = (
    PlannedEvaluation(
        resource_id="bucket-001", rule_id="S3-PUBLIC-001", perspective=EvaluationPerspective.IAC
    ),
)


def _result(model_profile_id: str = "assessment-nova-lite-m1-v2") -> EvaluationResult:
    return EvaluationResult(
        resource_id="bucket-001",
        rule_id="S3-PUBLIC-001",
        perspective=EvaluationPerspective.IAC,
        status=EvaluationStatus.FAIL,
        severity="CRITICAL",
        score=10,
        rationale="Public access block is disabled.",
        evidence_references=("terraform:storage.tf",),
        rule_version="2026-08-31",
        rubric_version="m1-three-perspective-v1",
        model_profile_id=model_profile_id,
        scoring_mode=ScoringMode.CONTINUOUS,
    )


class FakeReports:
    def __init__(self, results: tuple[EvaluationResult, ...]) -> None:
        self.results = results

    def get_report(self, *, customer_id: str, assessment_id: str) -> AssessmentReport:
        return AssessmentReport(
            assessment_id=assessment_id,
            results=self.results,
            findings=(),
            coverage=AssessmentCoverage(planned_evaluations=1, completed_evaluations=1),
            readiness_score=None,
        )

    def get_planned_evaluations(self, *, customer_id: str, assessment_id: str):
        return PLANNED


class FakeTable:
    def __init__(self, item: dict[str, object] | None) -> None:
        self.item = item

    def get_item(self, **kwargs: object) -> dict[str, object]:
        return {} if self.item is None else {"Item": self.item}


def _assessment_item(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "PK": f"CUSTOMER#{CUSTOMER_ID}",
        "SK": f"ASSESSMENT#{SOURCE_ASSESSMENT}",
        "entity_type": "ASSESSMENT",
        "customer_id": CUSTOMER_ID,
        "assessment_id": SOURCE_ASSESSMENT,
        "job_id": "job-asm-1",
        "repository_id": "repo-001",
        "policy_profile_id": "profile-mvp-baseline",
        "policy_profile_version": "v2",
        "phase": "INITIAL",
    }
    item.update(overrides)
    return item


class VerificationSourceReaderTest(unittest.TestCase):
    def test_assembles_the_source_scope_from_item_plan_and_results(self) -> None:
        reader = DynamoDbVerificationSourceReader(
            FakeTable(_assessment_item()), reports=FakeReports((_result(),))
        )
        source = reader.get_verification_source(
            customer_id=CUSTOMER_ID, assessment_id=SOURCE_ASSESSMENT
        )
        self.assertEqual(source.repository_id, "repo-001")
        self.assertEqual(source.policy_profile_version, "v2")
        self.assertEqual(source.planned_coordinates, PLANNED)
        # Model Profile/rubric은 결과에서 파생한다 — Initial item에는 그 pin이 없다 (§3).
        self.assertEqual(source.model_profile_id, "assessment-nova-lite-m1-v2")
        self.assertEqual(source.rubric_version, "m1-three-perspective-v1")
        self.assertIs(source.phase, AssessmentPhase.INITIAL)

    def test_a_record_without_a_profile_version_pin_is_refused(self) -> None:
        item = _assessment_item()
        del item["policy_profile_version"]
        reader = DynamoDbVerificationSourceReader(
            FakeTable(item), reports=FakeReports((_result(),))
        )
        with self.assertRaisesRegex(StoredDataError, "policy_profile_version"):
            reader.get_verification_source(customer_id=CUSTOMER_ID, assessment_id=SOURCE_ASSESSMENT)

    def test_a_missing_source_is_refused(self) -> None:
        reader = DynamoDbVerificationSourceReader(
            FakeTable(None), reports=FakeReports((_result(),))
        )
        with self.assertRaisesRegex(StoredDataError, "not found"):
            reader.get_verification_source(customer_id=CUSTOMER_ID, assessment_id=SOURCE_ASSESSMENT)

    def test_mixed_model_profiles_in_the_source_results_are_refused(self) -> None:
        reader = DynamoDbVerificationSourceReader(
            FakeTable(_assessment_item()), reports=FakeReports((_result(), _result("other")))
        )
        with self.assertRaises(StoredDataError):
            reader.get_verification_source(customer_id=CUSTOMER_ID, assessment_id=SOURCE_ASSESSMENT)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
