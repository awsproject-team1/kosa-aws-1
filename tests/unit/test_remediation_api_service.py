"""M2 A policy-gated remediation API service tests."""

import unittest
from datetime import UTC, datetime

from apps.backend.api.remediations import RemediationApiService
from apps.backend.auth import Principal, Role
from apps.backend.jobs import OutboxDispatcher
from apps.backend.repositories import RepositoryError
from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    EvaluationPerspective,
    EvaluationStatus,
    Finding,
    IaCSnapshot,
    ManualReviewCode,
    RemediationAction,
    RemediationContext,
    RemediationDecision,
    RemediationException,
    RemediationExceptionReason,
    RemediationTarget,
)

NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


class Contexts:
    def get_context(self, *, customer_id, finding_id):
        return context(customer_id=customer_id, finding_id=finding_id)


class Targets:
    def get_target(self, *, customer_id, finding_id):
        return RemediationTarget(
            resource_id="bucket",
            resource_type="AWS::S3::Bucket",
            rule_id="rule",
            rule_version="1",
            terraform_managed=True,
            iac_status=EvaluationStatus.PASS,
            iac_perspective=EvaluationPerspective.IAC,
            iac_commit_sha="commit",
        )


class Exceptions:
    def __init__(self):
        self.value = (
            RemediationException(
                exception_id="exception-001",
                customer_id="cust",
                rule_id="rule",
                rule_version="1",
                reason=RemediationExceptionReason.ACCEPTED_RISK,
                approved_by="admin",
                approved_at="2026-09-01T07:00:00+00:00",
                expires_at="2026-09-02T07:00:00+00:00",
            ),
        )

    def list_exceptions(self, *, customer_id, finding):
        return self.value


class DecisionMaker:
    def __init__(self, action: RemediationAction):
        self.action = action
        self.calls = []

    def decide(self, finding, **kwargs):
        self.calls.append((finding, kwargs))
        return decision(finding, self.action)


class Repository:
    def __init__(self):
        self.workflow_calls = []
        self.decision_calls = []
        self.pending = []

    def create_remediation_workflow(self, **kwargs):
        self.workflow_calls.append(kwargs)
        self.pending.append(kwargs["outbox"])

    def record_remediation_decision(self, **kwargs):
        self.decision_calls.append(kwargs)

    def list_pending_outbox(self, *, limit):
        return tuple(self.pending[:limit])

    def mark_outbox_dispatched(self, entry):
        self.pending.remove(entry)

    def record_outbox_dispatch_failure(self, entry):
        return None


class Dispatcher:
    def __init__(self):
        self.tasks = []

    def dispatch(self, task):
        self.tasks.append(task)


def context(*, customer_id="cust", finding_id="finding-001"):
    finding = Finding(
        finding_id=finding_id,
        resource_id="bucket",
        rule_id="rule",
        rule_version="1",
        perspective=EvaluationPerspective.DRIFT,
        status=EvaluationStatus.FAIL,
        severity="HIGH",
        score=0,
        rationale="test",
        evidence_references=("aws:bucket",),
        assessed_commit_sha="commit",
        evaluated_at="2026-09-01T07:00:00+00:00",
    )
    snapshot = IaCSnapshot(
        customer_id=customer_id,
        repository_id="repo",
        commit_sha="commit",
        artifact=ArtifactReference(
            artifact_id="snapshot",
            artifact_type=ArtifactType.TERRAFORM_SNAPSHOT,
            content_sha256="hash",
            customer_id=customer_id,
            repository_id="repo",
        ),
    )
    return RemediationContext(
        finding=finding,
        snapshot=snapshot,
        evidence_references=("aws:bucket",),
    )


def decision(finding: Finding, action: RemediationAction) -> RemediationDecision:
    return RemediationDecision(
        finding_id=finding.finding_id,
        resource_id=finding.resource_id,
        rule_id=finding.rule_id,
        rule_version=finding.rule_version,
        perspective=finding.perspective,
        action=action,
        manual_review_code=(
            ManualReviewCode.RULE_NOT_IN_SCOPE
            if action is RemediationAction.MANUAL_REVIEW
            else None
        ),
        exception_id=("exception-001" if action is RemediationAction.SUPPRESSED else None),
    )


def principal() -> Principal:
    return Principal(
        subject="user",
        client_id="client",
        customer_id="cust",
        roles=frozenset({Role.USER}),
    )


def service_for(action: RemediationAction):
    repository = Repository()
    dispatcher = Dispatcher()
    maker = DecisionMaker(action)
    service = RemediationApiService(
        contexts=Contexts(),
        targets=Targets(),
        exceptions=Exceptions(),
        decision_maker=maker,
        repository=repository,
        outbox_dispatcher=OutboxDispatcher(repository=repository, dispatcher=dispatcher),
        now=lambda: NOW,
        job_id_factory=lambda: "job-001",
        remediation_id_factory=lambda: "rem-001",
    )
    return service, repository, dispatcher, maker


class RemediationApiServiceTest(unittest.TestCase):
    def test_terraform_patch_persists_decision_job_and_outbox(self):
        service, repository, dispatcher, maker = service_for(RemediationAction.TERRAFORM_PATCH)

        response = service.create_remediation(principal(), "finding-001")

        self.assertTrue(response.accepted)
        self.assertEqual(response.job.remediation_id, "rem-001")
        saved = repository.workflow_calls[0]
        self.assertIs(saved["decision"].action, RemediationAction.TERRAFORM_PATCH)
        self.assertEqual(saved["decided_at"], NOW)
        self.assertEqual(saved["outbox"].task.command.value, "GENERATE_REMEDIATION")
        self.assertEqual(dispatcher.tasks, [saved["outbox"].task])
        self.assertEqual(maker.calls[0][1]["at"], NOW)
        self.assertEqual(len(maker.calls[0][1]["exceptions"]), 1)

    def test_actual_sync_uses_c_remediation_command_not_deployment_worker(self):
        service, repository, _, _ = service_for(RemediationAction.ACTUAL_SYNC)

        response = service.create_remediation(principal(), "finding-001")

        self.assertTrue(response.accepted)
        call = repository.workflow_calls[0]
        self.assertEqual(call["outbox"].task.command.value, "SYNC_ACTUAL_STATE")
        self.assertEqual(call["job"].current_step.value, "SYNC_ACTUAL_STATE")

    def test_manual_review_is_persisted_without_job_or_outbox(self):
        service, repository, dispatcher, _ = service_for(RemediationAction.MANUAL_REVIEW)

        response = service.create_remediation(principal(), "finding-001")

        self.assertFalse(response.accepted)
        self.assertIsNone(response.job)
        self.assertEqual(
            response.decision.manual_review_code,
            ManualReviewCode.RULE_NOT_IN_SCOPE,
        )
        self.assertEqual(len(repository.decision_calls), 1)
        self.assertEqual(repository.workflow_calls, [])
        self.assertEqual(dispatcher.tasks, [])

    def test_suppressed_is_persisted_without_job_or_outbox(self):
        service, repository, dispatcher, _ = service_for(RemediationAction.SUPPRESSED)

        response = service.create_remediation(principal(), "finding-001")

        self.assertFalse(response.accepted)
        self.assertEqual(response.decision.exception_id, "exception-001")
        self.assertEqual(len(repository.decision_calls), 1)
        self.assertEqual(repository.workflow_calls, [])
        self.assertEqual(dispatcher.tasks, [])

    def test_policy_decision_for_another_finding_is_rejected_before_write(self):
        service, repository, _, maker = service_for(RemediationAction.TERRAFORM_PATCH)
        original = maker.decide

        def mismatched(finding, **kwargs):
            value = original(finding, **kwargs)
            return RemediationDecision(
                finding_id="other",
                resource_id=value.resource_id,
                rule_id=value.rule_id,
                rule_version=value.rule_version,
                perspective=value.perspective,
                action=value.action,
            )

        maker.decide = mismatched
        with self.assertRaisesRegex(RepositoryError, "outside"):
            service.create_remediation(principal(), "finding-001")
        self.assertEqual(repository.workflow_calls, [])

    def test_legacy_or_future_finding_provenance_is_rejected_before_policy_or_write(self):
        service, repository, _, maker = service_for(RemediationAction.TERRAFORM_PATCH)
        original = service._contexts.get_context

        def future(*, customer_id, finding_id):
            value = original(customer_id=customer_id, finding_id=finding_id)
            finding = value.finding
            return RemediationContext(
                finding=Finding(
                    finding_id=finding.finding_id,
                    resource_id=finding.resource_id,
                    rule_id=finding.rule_id,
                    rule_version=finding.rule_version,
                    perspective=finding.perspective,
                    status=finding.status,
                    severity=finding.severity,
                    score=finding.score,
                    rationale=finding.rationale,
                    evidence_references=finding.evidence_references,
                    assessed_commit_sha="commit",
                    evaluated_at="2026-09-01T09:00:00+00:00",
                ),
                snapshot=value.snapshot,
                evidence_references=value.evidence_references,
            )

        service._contexts.get_context = future
        with self.assertRaisesRegex(RepositoryError, "after decision"):
            service.create_remediation(principal(), "finding-001")
        self.assertEqual(maker.calls, [])
        self.assertEqual(repository.workflow_calls, [])


if __name__ == "__main__":
    unittest.main()
