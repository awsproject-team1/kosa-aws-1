"""M3 A GET /deployments/{id} derives status from durable facts (ADR-0019 §8)."""

import unittest

from apps.backend.api.deployments import DeploymentApiService
from apps.backend.auth import Principal, Role
from apps.backend.deployment import DeploymentApprovalService, DeploymentRecord
from apps.backend.jobs import JobNotFoundError
from packages.contracts import (
    ApplyOutcome,
    ArtifactReference,
    ArtifactType,
    DeploymentFacts,
    DeploymentReadinessSignal,
    DeploymentStatus,
    JobCurrentStep,
    JobStatus,
    PlanSummary,
    TerraformStateVersion,
    VerificationOutcome,
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


class FactsReader:
    def __init__(self, facts: DeploymentFacts) -> None:
        self._facts = facts

    def get_deployment_facts(self, *, customer_id, deployment_id) -> DeploymentFacts:
        return self._facts


def _record() -> DeploymentRecord:
    return DeploymentRecord(
        deployment_id=DEPLOYMENT,
        customer_id=CUSTOMER,
        repository_id="repo-001",
        job_id="job-001",
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


def _principal() -> Principal:
    return Principal(
        subject="user-001",
        client_id="client-001",
        customer_id=CUSTOMER,
        roles=frozenset({Role.USER}),
    )


def _service(record, facts) -> DeploymentApiService:
    return DeploymentApiService(
        plans=PlanReader(),
        approvals=DeploymentApprovalService(ApprovalRepo()),
        deployments=DeploymentRepo(record),
        facts=FactsReader(facts) if facts is not None else None,
    )


class DeploymentReadServiceTest(unittest.TestCase):
    def test_returns_verified_status_and_identity(self) -> None:
        facts = DeploymentFacts(
            job_status=JobStatus.COMPLETED,
            current_step=JobCurrentStep.POST_DEPLOY_VERIFICATION,
            is_approved=True,
            apply_outcome=ApplyOutcome.SUCCEEDED,
            verification_outcome=VerificationOutcome.COMPARABLE,
        )
        view = _service(_record(), facts).get_deployment(_principal(), DEPLOYMENT)
        self.assertIs(view.status, DeploymentStatus.VERIFIED)
        self.assertEqual(view.deployment_id, DEPLOYMENT)
        self.assertEqual(view.plan_hash, "plan-001")
        self.assertEqual(view.to_dict()["status"], "VERIFIED")

    def test_waiting_approval_status_before_approval(self) -> None:
        facts = DeploymentFacts(
            job_status=JobStatus.WAITING_APPROVAL,
            current_step=JobCurrentStep.PRE_DEPLOY_VALIDATION,
            readiness=DeploymentReadinessSignal.READY_FOR_APPROVAL,
        )
        view = _service(_record(), facts).get_deployment(_principal(), DEPLOYMENT)
        self.assertIs(view.status, DeploymentStatus.WAITING_APPROVAL)

    def test_missing_deployment_is_not_found(self) -> None:
        facts = DeploymentFacts(
            job_status=JobStatus.QUEUED, current_step=JobCurrentStep.TERRAFORM_PLAN
        )
        with self.assertRaises(JobNotFoundError):
            _service(None, facts).get_deployment(_principal(), DEPLOYMENT)

    def test_read_dependencies_must_be_configured(self) -> None:
        service = DeploymentApiService(
            plans=PlanReader(), approvals=DeploymentApprovalService(ApprovalRepo())
        )
        with self.assertRaises(TypeError):
            service.get_deployment(_principal(), DEPLOYMENT)


if __name__ == "__main__":
    unittest.main()
