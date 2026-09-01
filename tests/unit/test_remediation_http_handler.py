"""HTTP tests for policy-gated remediation routes."""

import json
import unittest
from datetime import UTC, datetime

from apps.backend.api.handler import JobHttpHandler
from apps.backend.api.jobs import JobApiService
from apps.backend.api.remediation_exceptions import RemediationExceptionApiService
from apps.backend.api.remediations import RemediationApiService
from apps.backend.jobs import OutboxDispatcher
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
    RemediationExceptionReason,
    RemediationTarget,
)

NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


class Repository:
    def __init__(self):
        self.pending = []
        self.decisions = []
        self.workflows = []

    def create_assessment_workflow(self, assessment, job, outbox):
        return None

    def get_job(self, customer_id, job_id):
        return None

    def create_remediation_workflow(self, **kwargs):
        self.workflows.append(kwargs)
        self.pending.append(kwargs["outbox"])

    def record_remediation_decision(self, **kwargs):
        self.decisions.append(kwargs)

    def list_pending_outbox(self, *, limit):
        return tuple(self.pending[:limit])

    def mark_outbox_dispatched(self, entry):
        self.pending.remove(entry)

    def record_outbox_dispatch_failure(self, entry):
        return None


class Dispatcher:
    def dispatch(self, task):
        return None


class Scope:
    def authorize(self, principal, *, repository_id, policy_profile_id):
        return None


class Contexts:
    def get_context(self, *, customer_id, finding_id):
        finding = Finding(
            finding_id=finding_id,
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
                customer_id=customer_id,
                repository_id="repo-001",
                commit_sha="commit-001",
                artifact=ArtifactReference(
                    artifact_id="snapshot-001",
                    artifact_type=ArtifactType.TERRAFORM_SNAPSHOT,
                    content_sha256="snapshot-hash",
                    customer_id=customer_id,
                    repository_id="repo-001",
                ),
            ),
            evidence_references=finding.evidence_references,
        )


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
    def list_exceptions(self, *, customer_id, finding):
        return ()


class DecisionMaker:
    def __init__(self, action):
        self.action = action

    def decide(self, finding, **kwargs):
        return RemediationDecision(
            finding_id=finding.finding_id,
            resource_id=finding.resource_id,
            rule_id=finding.rule_id,
            rule_version=finding.rule_version,
            perspective=finding.perspective,
            action=self.action,
            manual_review_code=(
                ManualReviewCode.RULE_NOT_IN_SCOPE
                if self.action is RemediationAction.MANUAL_REVIEW
                else None
            ),
        )


class ExceptionRepository:
    def __init__(self):
        self.values = []

    def create_exception(self, exception):
        self.values.append(exception)


def event(method, path, *, groups=("User",), body=None):
    return {
        "rawPath": path,
        "body": body,
        "requestContext": {
            "http": {"method": method},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "token_use": "access",
                        "sub": "subject-001",
                        "client_id": "client-001",
                        "custom:customer_id": "cust-001",
                        "cognito:groups": list(groups),
                    }
                }
            },
        },
    }


def handler(action, remediation_exceptions=None):
    repository = Repository()
    outbox = OutboxDispatcher(repository=repository, dispatcher=Dispatcher())
    jobs = JobApiService(
        repository=repository,
        assessment_scope=Scope(),
        outbox_dispatcher=outbox,
        job_id_factory=lambda: "assessment-job",
        assessment_id_factory=lambda: "assessment-001",
    )
    remediations = RemediationApiService(
        contexts=Contexts(),
        targets=Targets(),
        exceptions=Exceptions(),
        decision_maker=DecisionMaker(action),
        repository=repository,
        outbox_dispatcher=outbox,
        now=lambda: NOW,
        job_id_factory=lambda: "job-001",
        remediation_id_factory=lambda: "rem-001",
    )
    return (
        JobHttpHandler(
            jobs,
            remediations=remediations,
            remediation_exceptions=remediation_exceptions,
        ),
        repository,
    )


class RemediationHttpHandlerTest(unittest.TestCase):
    def test_actionable_decision_returns_202_with_job_and_decision(self):
        subject, _ = handler(RemediationAction.TERRAFORM_PATCH)

        response = subject.handle(event("POST", "/findings/finding-001/remediations"))
        body = json.loads(response["body"])

        self.assertEqual(response["statusCode"], 202)
        self.assertEqual(body["decision"]["action"], "TERRAFORM_PATCH")
        self.assertEqual(body["job"]["job_id"], "job-001")

    def test_manual_decision_returns_200_without_job(self):
        subject, repository = handler(RemediationAction.MANUAL_REVIEW)

        response = subject.handle(event("POST", "/findings/finding-001/remediations"))
        body = json.loads(response["body"])

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(body["decision"]["manual_review_code"], "RULE_NOT_IN_SCOPE")
        self.assertIsNone(body["job"])
        self.assertEqual(repository.workflows, [])

    def test_admin_registers_exception_and_user_is_denied(self):
        repository = ExceptionRepository()
        exception_service = RemediationExceptionApiService(
            repository=repository,
            exception_id_factory=lambda: "exception-001",
            now=lambda: NOW,
        )
        subject, _ = handler(
            RemediationAction.MANUAL_REVIEW,
            remediation_exceptions=exception_service,
        )
        body = json.dumps(
            {
                "rule_id": "rule-001",
                "rule_version": "v1",
                "reason": RemediationExceptionReason.ACCEPTED_RISK.value,
                "expires_at": "2026-09-02T08:00:00+00:00",
            }
        )

        denied = subject.handle(event("POST", "/remediation-exceptions", body=body))
        created = subject.handle(
            event("POST", "/remediation-exceptions", groups=("Admin",), body=body)
        )

        self.assertEqual(denied["statusCode"], 403)
        self.assertEqual(created["statusCode"], 201)
        self.assertEqual(repository.values[0].customer_id, "cust-001")


if __name__ == "__main__":
    unittest.main()
