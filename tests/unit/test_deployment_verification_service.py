"""M3 A GET /deployments/{id}/verification wiring (ADR-0020 §1, §5, §7)."""

import unittest

from apps.backend.api.deployments import ComparisonInputReader, DeploymentApiService
from apps.backend.assessment import ComparisonAssessment
from apps.backend.assessment.reporting import AssessmentReport
from apps.backend.auth import Principal, Role
from apps.backend.deployment import DeploymentApprovalService, DeploymentRecord
from apps.backend.jobs import JobNotFoundError, RequestValidationError
from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    AssessmentCoverage,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    PlannedEvaluation,
    PlanSummary,
    ReadinessScore,
    TerraformStateVersion,
)

CUSTOMER = "cust-001"
DEPLOYMENT = "deployment-001"


class ApprovalRepo:
    def record_approval(self, *, customer_id, approval, readiness) -> None:  # pragma: no cover
        raise AssertionError("not used")


class PlanReader:
    def get_approval_input(self, *, customer_id, deployment_id):  # pragma: no cover - unused
        raise AssertionError("not used")


class DeploymentRepo:
    def __init__(self, record: DeploymentRecord | None) -> None:
        self._record = record

    def create_deployment(self, record, *, job, outbox) -> None:  # pragma: no cover - unused
        raise AssertionError("not used")

    def get_deployment(self, *, customer_id, deployment_id):
        return self._record


class ComparisonReader(ComparisonInputReader):
    def __init__(self, inputs, error: Exception | None = None) -> None:
        self._inputs = inputs
        self._error = error

    def get_comparison_inputs(
        self, *, customer_id, source_assessment_id, verification_assessment_id
    ):
        if self._error is not None:
            raise self._error
        return self._inputs


def _result(status: EvaluationStatus) -> EvaluationResult:
    return EvaluationResult(
        resource_id="bucket-001",
        rule_id="S3-001",
        perspective=EvaluationPerspective.AWS_ACTUAL,
        status=status,
        severity="HIGH",
        score=100 if status is EvaluationStatus.PASS else 20,
        rationale="fixture",
        evidence_references=("aws:s3:fixture",),
        rule_version="v1",
        rubric_version="m1-v1",
        model_profile_id="assessment-profile-v1",
    )


def _comparison(assessment_id: str, status: EvaluationStatus, score: float) -> ComparisonAssessment:
    results = (_result(status),)
    plan = (
        PlannedEvaluation(
            resource_id="bucket-001",
            rule_id="S3-001",
            perspective=EvaluationPerspective.AWS_ACTUAL,
        ),
    )
    report = AssessmentReport(
        assessment_id=assessment_id,
        results=results,
        findings=(),
        coverage=AssessmentCoverage(planned_evaluations=1, completed_evaluations=1),
        readiness_score=ReadinessScore(score=score, evaluated_evaluations=1),
    )
    return ComparisonAssessment(
        assessment_id=assessment_id,
        model_profile_id="assessment-profile-v1",
        rubric_version="m1-v1",
        planned_evaluations=plan,
        report=report,
    )


def _record(*, verification_assessment_id: str | None) -> DeploymentRecord:
    return DeploymentRecord(
        deployment_id=DEPLOYMENT,
        customer_id=CUSTOMER,
        repository_id="repo-001",
        job_id="job-001",
        remediation_id="remediation-001",
        commit_sha="commit-001",
        source_assessment_id="asm-source",
        plan_hash="plan-001",
        plan_artifact=ArtifactReference(
            artifact_id="art-plan-001",
            artifact_type=ArtifactType.TERRAFORM_PLAN,
            content_sha256="plan-001",
            customer_id=CUSTOMER,
            repository_id="repo-001",
        ),
        binary_artifact=ArtifactReference(
            artifact_id="art-plan-binary-001",
            artifact_type=ArtifactType.TERRAFORM_PLAN_BINARY,
            content_sha256="binary-001",
            customer_id=CUSTOMER,
            repository_id="repo-001",
        ),
        state_version=TerraformStateVersion(lineage="lineage-1", serial=1),
        plan_summary=PlanSummary(
            refreshed=True,
            has_destructive_changes=False,
            mapped_resource_ids=("bucket-public-001",),
        ),
        verification_assessment_id=verification_assessment_id,
    )


def _principal() -> Principal:
    return Principal(
        subject="user-001",
        client_id="client-001",
        customer_id=CUSTOMER,
        roles=frozenset({Role.USER}),
    )


def _service(record, reader) -> DeploymentApiService:
    return DeploymentApiService(
        plans=PlanReader(),
        approvals=DeploymentApprovalService(ApprovalRepo()),
        deployments=DeploymentRepo(record),
        comparisons=reader,
    )


class DeploymentVerificationServiceTest(unittest.TestCase):
    def test_comparable_verification_returns_score_delta(self) -> None:
        inputs = (
            _comparison("asm-source", EvaluationStatus.FAIL, 20),
            _comparison("asm-verify", EvaluationStatus.PASS, 100),
        )
        record = _record(verification_assessment_id="asm-verify")
        comparison = _service(record, ComparisonReader(inputs)).get_verification(
            _principal(), DEPLOYMENT
        )
        self.assertTrue(comparison.comparable)
        self.assertEqual(comparison.readiness_score_delta, 80.0)
        self.assertEqual(comparison.deployment_id, DEPLOYMENT)

    def test_missing_deployment_is_not_found(self) -> None:
        with self.assertRaises(JobNotFoundError):
            _service(None, ComparisonReader(None)).get_verification(_principal(), DEPLOYMENT)

    def test_no_verification_assessment_yet_is_not_found(self) -> None:
        record = _record(verification_assessment_id=None)
        with self.assertRaises(JobNotFoundError):
            _service(record, ComparisonReader(None)).get_verification(_principal(), DEPLOYMENT)

    def test_incomplete_comparison_input_is_validation_error(self) -> None:
        record = _record(verification_assessment_id="asm-verify")
        reader = ComparisonReader(None, error=ValueError("report must contain complete results"))
        with self.assertRaises(RequestValidationError):
            _service(record, reader).get_verification(_principal(), DEPLOYMENT)

    def test_verification_dependencies_must_be_configured(self) -> None:
        service = DeploymentApiService(
            plans=PlanReader(), approvals=DeploymentApprovalService(ApprovalRepo())
        )
        with self.assertRaises(TypeError):
            service.get_verification(_principal(), DEPLOYMENT)


if __name__ == "__main__":
    unittest.main()
