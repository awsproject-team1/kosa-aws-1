"""M2 A approval gate tests."""

import unittest

from apps.backend.auth import AuthorizationDenied, Principal, Role
from apps.backend.deployment import DeploymentApprovalError, DeploymentApprovalService
from apps.backend.deployment.record import DeploymentRecord
from apps.backend.jobs.models import Job
from apps.backend.jobs.outbox import OutboxDispatcher
from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    DeploymentReadiness,
    DeploymentReadinessStatus,
    JobCurrentStep,
    JobStatus,
    TerraformPlan,
    WorkflowCommand,
)


class Repository:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def record_approval(self, **kwargs) -> None:
        self.calls.append(kwargs)


class Deployments:
    def get_deployment(self, *, customer_id, deployment_id):
        return DeploymentRecord(
            deployment_id=deployment_id,
            customer_id=customer_id,
            repository_id="repo-001",
            job_id="job-001",
            remediation_id="rem-001",
            commit_sha="commit-001",
            source_assessment_id="asm-001",
        )


class Jobs:
    def get_job(self, customer_id, job_id):
        return Job(
            job_id=job_id,
            customer_id=customer_id,
            job_type="DEPLOYMENT",
            status=JobStatus.QUEUED,
            current_step=JobCurrentStep.TERRAFORM_PLAN,
            requested_by="admin-001",
            revision=0,
            deployment_id="deployment-001",
        )


class Dispatcher:
    def __init__(self) -> None:
        self.tasks: list[object] = []

    def dispatch(self, task) -> None:
        self.tasks.append(task)


class OutboxRepository:
    def mark_outbox_dispatched(self, entry) -> None:
        return None

    def record_outbox_dispatch_failure(self, entry) -> None:
        return None

    def list_pending_outbox(self, *, limit):
        return ()


def service(repository):
    dispatcher = Dispatcher()
    return (
        DeploymentApprovalService(
            repository,
            deployments=Deployments(),
            jobs=Jobs(),
            outbox_dispatcher=OutboxDispatcher(
                repository=OutboxRepository(), dispatcher=dispatcher
            ),
        ),
        dispatcher,
    )


def plan() -> TerraformPlan:
    return TerraformPlan(
        deployment_id="deployment-001",
        commit_sha="commit-001",
        plan_hash="plan-hash-001",
        artifact=ArtifactReference(
            artifact_id="plan-001",
            artifact_type=ArtifactType.TERRAFORM_PLAN,
            content_sha256="plan-hash-001",
            customer_id="cust-001",
            repository_id="repo-001",
        ),
    )


def readiness(*, status=DeploymentReadinessStatus.READY_FOR_APPROVAL) -> DeploymentReadiness:
    return DeploymentReadiness(
        deployment_id="deployment-001",
        finding_id="finding-001",
        commit_sha="commit-001",
        plan_hash="plan-hash-001",
        status=status,
        reason_codes=("REFRESHED_PLAN_BOUND_TO_REMEDIATION_CONTEXT",),
    )


def principal(*, role: Role) -> Principal:
    return Principal(
        subject="admin-001",
        client_id="client-001",
        customer_id="cust-001",
        roles=frozenset({role}),
    )


class DeploymentApprovalServiceTest(unittest.TestCase):
    def test_admin_approval_is_bound_and_audited(self) -> None:
        repository = Repository()
        subject, dispatcher = service(repository)
        approval = subject.approve(
            principal=principal(role=Role.ADMIN), plan=plan(), readiness=readiness()
        )
        self.assertTrue(approval.matches(plan()))
        self.assertEqual(repository.calls[0]["customer_id"], "cust-001")
        self.assertEqual(repository.calls[0]["outbox"].task.command, WorkflowCommand.PLAN_COMPLETED)
        self.assertEqual(repository.calls[0]["resumed_job"].current_step, JobCurrentStep.APPLY)
        self.assertEqual(len(dispatcher.tasks), 1)

    def test_user_cannot_approve(self) -> None:
        with self.assertRaises(AuthorizationDenied):
            service(Repository())[0].approve(
                principal=principal(role=Role.USER), plan=plan(), readiness=readiness()
            )

    def test_blocked_readiness_never_writes_an_approval(self) -> None:
        repository = Repository()
        with self.assertRaises(DeploymentApprovalError):
            service(repository)[0].approve(
                principal=principal(role=Role.ADMIN),
                plan=plan(),
                readiness=readiness(status=DeploymentReadinessStatus.BLOCKED),
            )
        self.assertEqual(repository.calls, [])
