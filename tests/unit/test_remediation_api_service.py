"""M2 A remediation start service tests with mockable B/D context input."""

import unittest

from apps.backend.api.remediations import RemediationApiService
from apps.backend.auth import Principal, Role
from apps.backend.jobs import OutboxDispatcher
from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    EvaluationPerspective,
    EvaluationStatus,
    Finding,
    IaCSnapshot,
)
from packages.contracts.remediation import RemediationContext, RemediationStrategy


class Contexts:
    def get_context(self, *, customer_id, finding_id):
        return context(customer_id=customer_id, finding_id=finding_id)


class Repository:
    def __init__(self):
        self.calls = []

    def create_remediation_workflow(self, **kwargs):
        self.calls.append(kwargs)

    def list_pending_outbox(self, *, limit):
        return ()

    def mark_outbox_dispatched(self, entry):
        pass

    def record_outbox_dispatch_failure(self, entry):
        pass


class Dispatcher:
    def dispatch(self, task):
        pass


def context(*, customer_id, finding_id):
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
        strategy=RemediationStrategy.PATCH_IAC,
        evidence_references=("aws:bucket",),
    )


class RemediationApiServiceTest(unittest.TestCase):
    def test_creates_job_and_durable_outbox_from_jwt_scope(self):
        repository = Repository()
        service = RemediationApiService(
            contexts=Contexts(),
            repository=repository,
            outbox_dispatcher=OutboxDispatcher(repository=repository, dispatcher=Dispatcher()),
            job_id_factory=lambda: "job-001",
            remediation_id_factory=lambda: "rem-001",
        )
        response = service.create_remediation(
            Principal(
                subject="user", client_id="client", customer_id="cust", roles=frozenset({Role.USER})
            ),
            "finding-001",
        )
        self.assertEqual(response.remediation_id, "rem-001")
        self.assertEqual(repository.calls[0]["outbox"].task.command.value, "GENERATE_REMEDIATION")
