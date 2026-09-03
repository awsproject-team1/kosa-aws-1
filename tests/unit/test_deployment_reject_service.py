"""M3 A POST /deployments/{id}/reject is Admin-only and cancels the Job (ADR-0019 §8)."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime

from apps.backend.api.deployments import DeploymentApiService, DeploymentRejectRequest
from apps.backend.auth import AuthorizationDenied, Principal, Role
from apps.backend.deployment import (
    DeploymentApprovalService,
    DeploymentConflictError,
    DeploymentRecord,
    DeploymentRejection,
)
from apps.backend.jobs import JobNotFoundError
from apps.backend.jobs.lifecycle import create_job
from apps.backend.jobs.models import Job
from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    DeploymentRejectionReason,
    JobCurrentStep,
    JobStatus,
    PlanSummary,
    TerraformStateVersion,
)

CUSTOMER = "cust-001"
DEPLOYMENT = "deployment-001"
JOB = "job-001"


class ApprovalRepo:
    def record_approval(self, *, customer_id, approval, readiness) -> None:  # pragma: no cover
        raise AssertionError("not used")


class PlanReader:
    def get_approval_input(self, *, customer_id, deployment_id):  # pragma: no cover - unused
        raise AssertionError("not used")


class DeploymentRepo:
    def __init__(self, record: DeploymentRecord | None) -> None:
        self._record = record
        self.rejections: list[tuple[DeploymentRejection, Job, int]] = []

    def create_deployment(self, record, *, job, outbox) -> None:  # pragma: no cover - unused
        raise AssertionError("not used")

    def get_deployment(self, *, customer_id, deployment_id):
        return self._record

    def reject_deployment(self, *, rejection, cancelled_job, expected_revision) -> None:
        self.rejections.append((rejection, cancelled_job, expected_revision))


class JobRepo:
    def __init__(self, job: Job | None) -> None:
        self._job = job

    def create_job(self, job) -> None:  # pragma: no cover - unused
        raise AssertionError("not used")

    def get_job(self, customer_id, job_id):
        return self._job

    def update_job(self, job, *, expected_revision) -> None:  # pragma: no cover - unused
        raise AssertionError("not used")


def _record() -> DeploymentRecord:
    return DeploymentRecord(
        deployment_id=DEPLOYMENT,
        customer_id=CUSTOMER,
        repository_id="repo-001",
        job_id=JOB,
        remediation_id="remediation-001",
        commit_sha="commit-001",
        source_assessment_id="asm-001",
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
    )


def _waiting_job() -> Job:
    job = create_job(
        job_id=JOB,
        customer_id=CUSTOMER,
        job_type="DEPLOYMENT",
        initial_step=JobCurrentStep.PRE_DEPLOY_VALIDATION,
        requested_by="user-001",
    )
    job = replace(job, deployment_id=DEPLOYMENT, status=JobStatus.WAITING_APPROVAL)
    return job


def _principal(*, role: Role) -> Principal:
    return Principal(
        subject="admin-001", client_id="client-001", customer_id=CUSTOMER, roles=frozenset({role})
    )


def _service(record, job) -> DeploymentApiService:
    return DeploymentApiService(
        plans=PlanReader(),
        approvals=DeploymentApprovalService(ApprovalRepo()),
        deployments=DeploymentRepo(record),
        jobs=JobRepo(job),
        now=lambda: datetime(2026, 9, 2, tzinfo=UTC),
    )


def _service_with_repos(record, job):
    repo = DeploymentRepo(record)
    service = DeploymentApiService(
        plans=PlanReader(),
        approvals=DeploymentApprovalService(ApprovalRepo()),
        deployments=repo,
        jobs=JobRepo(job),
        now=lambda: datetime(2026, 9, 2, tzinfo=UTC),
    )
    return service, repo


class DeploymentRejectServiceTest(unittest.TestCase):
    def test_admin_reject_cancels_job_and_records_rejection(self) -> None:
        service, repo = _service_with_repos(_record(), _waiting_job())
        rejection = service.reject(
            _principal(role=Role.ADMIN),
            DEPLOYMENT,
            DeploymentRejectRequest(reason=DeploymentRejectionReason.RISK_TOO_HIGH),
        )
        self.assertEqual(rejection.reason, DeploymentRejectionReason.RISK_TOO_HIGH)
        stored_rejection, cancelled_job, expected_revision = repo.rejections[0]
        self.assertIs(cancelled_job.status, JobStatus.CANCELLED)
        self.assertEqual(expected_revision, 0)
        self.assertEqual(stored_rejection.rejected_by, "admin-001")

    def test_user_cannot_reject(self) -> None:
        service = _service(_record(), _waiting_job())
        with self.assertRaises(AuthorizationDenied):
            service.reject(
                _principal(role=Role.USER),
                DEPLOYMENT,
                DeploymentRejectRequest(reason=DeploymentRejectionReason.OTHER),
            )

    def test_missing_deployment_is_not_found(self) -> None:
        service = _service(None, _waiting_job())
        with self.assertRaises(JobNotFoundError):
            service.reject(
                _principal(role=Role.ADMIN),
                DEPLOYMENT,
                DeploymentRejectRequest(reason=DeploymentRejectionReason.OTHER),
            )

    def test_terminal_job_cannot_be_rejected(self) -> None:
        completed = replace(_waiting_job(), status=JobStatus.COMPLETED)
        # WAITING_APPROVAL → COMPLETED is not a real path; force a terminal state directly.
        completed = replace(_waiting_job())
        completed = replace(completed, status=JobStatus.CANCELLED)
        service = _service(_record(), completed)
        with self.assertRaises(DeploymentConflictError):
            service.reject(
                _principal(role=Role.ADMIN),
                DEPLOYMENT,
                DeploymentRejectRequest(reason=DeploymentRejectionReason.OTHER),
            )

    def test_reject_dependencies_must_be_configured(self) -> None:
        service = DeploymentApiService(
            plans=PlanReader(), approvals=DeploymentApprovalService(ApprovalRepo())
        )
        with self.assertRaises(TypeError):
            service.reject(
                _principal(role=Role.ADMIN),
                DEPLOYMENT,
                DeploymentRejectRequest(reason=DeploymentRejectionReason.OTHER),
            )


if __name__ == "__main__":
    unittest.main()
