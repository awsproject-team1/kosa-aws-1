"""M3 A deployment creation from a stored remediation (ADR-0019 §4)."""

import unittest

from apps.backend.api.deployments import (
    DeploymentApiService,
    DeploymentSource,
    DeploymentSourceReader,
)
from apps.backend.auth import Principal, Role
from apps.backend.deployment import (
    DeploymentApprovalService,
    DeploymentConflictError,
    DeploymentRecord,
)
from apps.backend.jobs.models import Job
from apps.backend.jobs.outbox import WorkflowOutboxEntry
from packages.contracts import JobCurrentStep, JobStatus, RemediationAction, WorkflowCommand

CUSTOMER = "cust-001"


class SourceReader(DeploymentSourceReader):
    def __init__(self, source: DeploymentSource) -> None:
        self._source = source

    def get_deployment_source(self, *, customer_id: str, remediation_id: str) -> DeploymentSource:
        return self._source


class PlanReader:
    def get_approval_input(self, *, customer_id, deployment_id):  # pragma: no cover - unused here
        raise AssertionError("not used")


class ApprovalRepo:
    def record_approval(self, *, customer_id, approval, readiness) -> None:  # pragma: no cover
        raise AssertionError("not used")


class DeploymentRepo:
    def __init__(self) -> None:
        self.records: list[tuple[DeploymentRecord, Job, WorkflowOutboxEntry]] = []

    def create_deployment(self, record, *, job, outbox) -> None:
        self.records.append((record, job, outbox))

    def get_deployment(self, *, customer_id, deployment_id):  # pragma: no cover - unused
        return None


class Dispatcher:
    def __init__(self) -> None:
        self.entries: list[WorkflowOutboxEntry] = []

    def dispatch_entry(self, entry: WorkflowOutboxEntry) -> None:
        self.entries.append(entry)


def _source(**overrides: object) -> DeploymentSource:
    base: dict[str, object] = {
        "remediation_id": "remediation-001",
        "customer_id": CUSTOMER,
        "repository_id": "repo-001",
        "commit_sha": "commit-001",
        "source_assessment_id": "asm-001",
        "action": RemediationAction.TERRAFORM_PATCH,
        "has_worker_result": True,
        "commit_reachable_from_default_branch": True,
    }
    base.update(overrides)
    return DeploymentSource(**base)  # type: ignore[arg-type]


def _principal(*, role: Role = Role.USER) -> Principal:
    return Principal(
        subject="user-001", client_id="client-001", customer_id=CUSTOMER, roles=frozenset({role})
    )


def _service(source: DeploymentSource, repo: DeploymentRepo, dispatcher: Dispatcher):
    return DeploymentApiService(
        plans=PlanReader(),
        approvals=DeploymentApprovalService(ApprovalRepo()),
        sources=SourceReader(source),
        deployments=repo,
        outbox_dispatcher=dispatcher,
        deployment_id_factory=lambda: "deployment-001",
        job_id_factory=lambda: "job-001",
    )


class DeploymentCreationServiceTest(unittest.TestCase):
    def test_creates_deployment_job_and_dispatches_run_deployment(self) -> None:
        repo, dispatcher = DeploymentRepo(), Dispatcher()
        job = _service(_source(), repo, dispatcher).create_deployment(
            _principal(), "remediation-001"
        )
        self.assertEqual(job.job_type, "DEPLOYMENT")
        self.assertEqual(job.status, JobStatus.QUEUED)
        self.assertEqual(job.current_step, JobCurrentStep.TERRAFORM_PLAN)
        self.assertEqual(job.deployment_id, "deployment-001")
        record, stored_job, outbox = repo.records[0]
        self.assertEqual(record.commit_sha, "commit-001")
        self.assertFalse(record.has_plan)
        self.assertEqual(record.source_assessment_id, "asm-001")
        self.assertIs(outbox.task.command, WorkflowCommand.RUN_DEPLOYMENT)
        self.assertEqual(dispatcher.entries, [outbox])

    def test_actual_sync_does_not_require_default_branch_reachability(self) -> None:
        repo, dispatcher = DeploymentRepo(), Dispatcher()
        source = _source(
            action=RemediationAction.ACTUAL_SYNC, commit_reachable_from_default_branch=False
        )
        job = _service(source, repo, dispatcher).create_deployment(_principal(), "remediation-001")
        self.assertEqual(job.deployment_id, "deployment-001")

    def test_non_deployable_action_is_conflict(self) -> None:
        repo, dispatcher = DeploymentRepo(), Dispatcher()
        source = _source(action=RemediationAction.MANUAL_REVIEW)
        with self.assertRaises(DeploymentConflictError):
            _service(source, repo, dispatcher).create_deployment(_principal(), "remediation-001")
        self.assertEqual(repo.records, [])

    def test_missing_worker_result_is_conflict(self) -> None:
        repo, dispatcher = DeploymentRepo(), Dispatcher()
        with self.assertRaises(DeploymentConflictError):
            _service(_source(has_worker_result=False), repo, dispatcher).create_deployment(
                _principal(), "remediation-001"
            )

    def test_unreachable_commit_for_patch_is_conflict(self) -> None:
        repo, dispatcher = DeploymentRepo(), Dispatcher()
        source = _source(commit_reachable_from_default_branch=False)
        with self.assertRaises(DeploymentConflictError):
            _service(source, repo, dispatcher).create_deployment(_principal(), "remediation-001")

    def test_creation_dependencies_must_be_configured(self) -> None:
        service = DeploymentApiService(
            plans=PlanReader(), approvals=DeploymentApprovalService(ApprovalRepo())
        )
        with self.assertRaises(TypeError):
            service.create_deployment(_principal(), "remediation-001")


if __name__ == "__main__":
    unittest.main()
