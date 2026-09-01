"""A → B → C remediation integration without live D adapters."""

import unittest
from datetime import UTC, datetime

from apps.backend.api.remediations import RemediationApiService
from apps.backend.auth import Principal, Role
from apps.backend.jobs import OutboxDispatcher
from apps.backend.policy.remediation import RemediationPolicy
from apps.backend.remediation import RemediationSyncTarget, RemediationWork, RemediationWorker
from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    EvaluationPerspective,
    EvaluationStatus,
    Finding,
    IaCSnapshot,
    RemediationAction,
    RemediationContext,
    RemediationEligibility,
    RemediationException,
    RemediationExceptionReason,
    RemediationPatch,
    RemediationRuleScope,
    RemediationTarget,
)

NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


def context() -> RemediationContext:
    finding = Finding(
        finding_id="finding-001",
        resource_id="bucket-001",
        rule_id="rule-001",
        rule_version="v1",
        perspective=EvaluationPerspective.IAC,
        status=EvaluationStatus.FAIL,
        severity="HIGH",
        score=0,
        rationale="unsafe",
        evidence_references=("terraform:bucket-001",),
    )
    return RemediationContext(
        finding=finding,
        snapshot=IaCSnapshot(
            customer_id="cust-001",
            repository_id="repo-001",
            commit_sha="base-commit",
            artifact=ArtifactReference(
                artifact_id="snapshot-001",
                artifact_type=ArtifactType.TERRAFORM_SNAPSHOT,
                content_sha256="snapshot-hash",
                customer_id="cust-001",
                repository_id="repo-001",
            ),
        ),
        evidence_references=finding.evidence_references,
    )


class Contexts:
    def get_context(self, *, customer_id, finding_id):
        return context()


class Targets:
    def get_target(self, *, customer_id, finding_id):
        return RemediationTarget(
            resource_id="bucket-001",
            resource_type="AWS::S3::Bucket",
            rule_id="rule-001",
            rule_version="v1",
            terraform_managed=True,
        )


class Exceptions:
    def __init__(self, values=()):
        self.values = values

    def list_exceptions(self, *, customer_id, finding):
        return self.values


class InMemoryRepository:
    def __init__(self):
        self.workflow = None
        self.decision_only = None
        self.pending = []
        self.results = []

    def create_remediation_workflow(self, **kwargs):
        self.workflow = kwargs
        self.pending.append(kwargs["outbox"])

    def record_remediation_decision(self, **kwargs):
        self.decision_only = kwargs

    def list_pending_outbox(self, *, limit):
        return tuple(self.pending[:limit])

    def mark_outbox_dispatched(self, entry):
        self.pending.remove(entry)

    def record_outbox_dispatch_failure(self, entry):
        return None

    def get_work(self, *, job_id, expected_revision):
        if self.workflow is None:
            return None
        job = self.workflow["job"]
        if job.job_id != job_id or job.revision != expected_revision:
            return None
        return RemediationWork(
            customer_id=job.customer_id,
            remediation_id=self.workflow["remediation_id"],
            job_id=job_id,
            revision=expected_revision,
            context=self.workflow["context"],
            decision=self.workflow["decision"],
        )

    def put_result_if_absent(self, *, work, result):
        if (work.remediation_id, result) not in self.results:
            self.results.append((work.remediation_id, result))


class Dispatcher:
    def __init__(self):
        self.tasks = []

    def dispatch(self, task):
        self.tasks.append(task)


class PatchAction:
    def __init__(self):
        self.calls = 0

    def generate(self, *, context, decision):
        self.calls += 1
        return RemediationPatch(
            finding_id=context.finding.finding_id,
            base_commit_sha=context.snapshot.commit_sha,
            artifact=ArtifactReference(
                artifact_id="patch-001",
                artifact_type=ArtifactType.REMEDIATION_PATCH,
                content_sha256="patch-hash",
                customer_id=context.snapshot.customer_id,
                repository_id=context.snapshot.repository_id,
            ),
            changed_paths=("main.tf",),
        )


class SyncAction:
    def prepare(self, *, context, decision):
        return RemediationSyncTarget(
            finding_id=context.finding.finding_id,
            customer_id=context.snapshot.customer_id,
            repository_id=context.snapshot.repository_id,
            commit_sha=context.snapshot.commit_sha,
        )


def principal() -> Principal:
    return Principal(
        subject="user-001",
        client_id="client-001",
        customer_id="cust-001",
        roles=frozenset({Role.USER}),
    )


def api(repository, dispatcher, exceptions=()):
    return RemediationApiService(
        contexts=Contexts(),
        targets=Targets(),
        exceptions=Exceptions(exceptions),
        decision_maker=RemediationPolicy(
            [
                RemediationRuleScope(
                    rule_id="rule-001",
                    version="v1",
                    eligibility=RemediationEligibility.AUTOMATIC,
                )
            ]
        ),
        repository=repository,
        outbox_dispatcher=OutboxDispatcher(repository=repository, dispatcher=dispatcher),
        now=lambda: NOW,
        job_id_factory=lambda: "job-001",
        remediation_id_factory=lambda: "rem-001",
    )


class RemediationPolicyWorkflowIntegrationTest(unittest.TestCase):
    def test_policy_decision_is_stored_and_consumed_by_c_worker(self):
        repository = InMemoryRepository()
        dispatcher = Dispatcher()

        response = api(repository, dispatcher).create_remediation(principal(), "finding-001")

        self.assertTrue(response.accepted)
        self.assertIs(response.decision.action, RemediationAction.TERRAFORM_PATCH)
        self.assertEqual(repository.workflow["decision"], response.decision)
        patch = PatchAction()
        result = RemediationWorker(
            work_repository=repository,
            patch_action=patch,
            sync_action=SyncAction(),
            result_store=repository,
        ).handle(dispatcher.tasks[0])
        self.assertEqual(patch.calls, 1)
        self.assertIsInstance(result, RemediationPatch)
        self.assertEqual(repository.results, [("rem-001", result)])

    def test_active_customer_exception_stops_before_job_and_worker(self):
        exception = RemediationException(
            exception_id="exception-001",
            customer_id="cust-001",
            rule_id="rule-001",
            rule_version="v1",
            reason=RemediationExceptionReason.ACCEPTED_RISK,
            approved_by="admin-001",
            approved_at="2026-09-01T07:00:00+00:00",
            expires_at="2026-09-02T07:00:00+00:00",
        )
        repository = InMemoryRepository()
        dispatcher = Dispatcher()

        response = api(repository, dispatcher, (exception,)).create_remediation(
            principal(), "finding-001"
        )

        self.assertFalse(response.accepted)
        self.assertIs(response.decision.action, RemediationAction.SUPPRESSED)
        self.assertEqual(response.decision.exception_id, "exception-001")
        self.assertIsNone(repository.workflow)
        self.assertEqual(dispatcher.tasks, [])
        self.assertEqual(repository.decision_only["decision"], response.decision)


if __name__ == "__main__":
    unittest.main()
