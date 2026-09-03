"""End-to-end composition: DynamoDB reader → real B policy → A remediation service.

Proves `_remediation_components`-shaped wiring resolves a stored S3-PUBLIC FAIL to a
`TERRAFORM_PATCH` remediation using the committed eligibility and the real reader,
without any live AWS or GitHub adapter.
"""

import unittest
from datetime import UTC, datetime
from pathlib import Path

from apps.backend.api.remediations import RemediationApiService
from apps.backend.auth import Principal, Role
from apps.backend.jobs import OutboxDispatcher
from apps.backend.policy import load_rule_registry
from apps.backend.repositories.remediation_context import DynamoDbRemediationContextReader
from packages.contracts import EvaluationPerspective, EvaluationStatus, RemediationAction

CUSTOMER = "kosa-sandbox"
ASSESSMENT = "asm-001"
FINDING_ID = "finding-s3public"
COMMIT = "d6b2c119872e20a890e14cb6bc41017527e600e6"
EVALUATED_AT = "2026-09-03T06:27:46.374691+00:00"
NOW = datetime(2026, 9, 3, 7, 0, tzinfo=UTC)


def _finding_item():
    return {
        "PK": f"CUSTOMER#{CUSTOMER}",
        "SK": f"ASSESSMENT#{ASSESSMENT}#FINDING#{FINDING_ID}",
        "entity_type": "FINDING",
        "customer_id": CUSTOMER,
        "assessment_id": ASSESSMENT,
        "finding_id": FINDING_ID,
        "resource_id": "tfsbx-bucket",
        "rule_id": "S3-PUBLIC-001",
        "rule_version": "2026-08-31",
        "perspective": EvaluationPerspective.AWS_ACTUAL.value,
        "status": EvaluationStatus.FAIL.value,
        "severity": "HIGH",
        "score": 0,
        "rationale": "public access is not blocked",
        "evidence_references": ["aws:tfsbx-bucket:public_access_block"],
        "assessed_commit_sha": COMMIT,
        "evaluated_at": EVALUATED_AT,
    }


def _result_item(perspective, status):
    return {
        "PK": f"CUSTOMER#{CUSTOMER}",
        "SK": (
            f"ASSESSMENT#{ASSESSMENT}#RESULT#tfsbx-bucket"
            f"#RULE#S3-PUBLIC-001#PERSPECTIVE#{perspective.value}"
        ),
        "entity_type": "ASSESSMENT_RESULT",
        "customer_id": CUSTOMER,
        "assessment_id": ASSESSMENT,
        "resource_id": "tfsbx-bucket",
        "rule_id": "S3-PUBLIC-001",
        "perspective": perspective.value,
        "status": status.value,
        "severity": "HIGH",
        "score": 0,
        "rationale": "derived",
        "evidence_references": [f"{perspective.value}:tfsbx-bucket"],
        "rule_version": "2026-08-31",
        "rubric_version": "2026-08-31",
        "model_profile_id": "mp-nova-lite",
        "scoring_mode": "CONTINUOUS",
        "assessed_commit_sha": COMMIT,
        "evaluated_at": EVALUATED_AT,
    }


def _assessment_item():
    return {
        "PK": f"CUSTOMER#{CUSTOMER}",
        "SK": f"ASSESSMENT#{ASSESSMENT}",
        "entity_type": "ASSESSMENT",
        "customer_id": CUSTOMER,
        "assessment_id": ASSESSMENT,
        "repository_id": "test-s3-sandbox",
        "policy_profile_id": "profile-mvp-baseline",
        "job_id": "job-001",
        "phase": "INITIAL",
        "status": "QUEUED",
    }


class Table:
    def __init__(self, iac_status):
        self.findings = [_finding_item()]
        self.items = {
            f"ASSESSMENT#{ASSESSMENT}#RESULT#tfsbx-bucket#RULE#S3-PUBLIC-001#PERSPECTIVE#IAC": (
                _result_item(EvaluationPerspective.IAC, iac_status)
            ),
            (
                f"ASSESSMENT#{ASSESSMENT}#RESULT#tfsbx-bucket"
                "#RULE#S3-PUBLIC-001#PERSPECTIVE#AWS_ACTUAL"
            ): _result_item(EvaluationPerspective.AWS_ACTUAL, EvaluationStatus.FAIL),
            f"ASSESSMENT#{ASSESSMENT}": _assessment_item(),
        }

    def query(self, **kwargs):
        return {"Items": list(self.findings)}

    def get_item(self, **kwargs):
        item = self.items.get(kwargs["Key"]["SK"])
        return {"Item": item} if item is not None else {}


class NoExceptions:
    def list_exceptions(self, *, customer_id, finding):
        return ()


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


def _principal():
    return Principal(
        subject="e2e-admin",
        client_id="client",
        customer_id=CUSTOMER,
        roles=frozenset({Role.USER}),
    )


def _service(iac_status):
    reader = DynamoDbRemediationContextReader(Table(iac_status))
    policy = load_rule_registry(Path("fixtures/rules")).remediation
    repository = Repository()
    dispatcher = Dispatcher()
    service = RemediationApiService(
        contexts=reader,
        targets=reader,
        exceptions=NoExceptions(),
        decision_maker=policy,
        repository=repository,
        outbox_dispatcher=OutboxDispatcher(repository=repository, dispatcher=dispatcher),
        now=lambda: NOW,
        job_id_factory=lambda: "job-002",
        remediation_id_factory=lambda: "rem-002",
    )
    return service, repository, dispatcher


class RemediationCompositionTest(unittest.TestCase):
    def test_actual_fail_with_failing_iac_yields_terraform_patch(self):
        # AWS_ACTUAL FAIL + IAC FAIL on an AUTOMATIC rule → patch the IaC.
        service, repository, dispatcher = _service(EvaluationStatus.FAIL)

        response = service.create_remediation(_principal(), FINDING_ID)

        self.assertTrue(response.accepted)
        decision = repository.workflow_calls[0]["decision"]
        self.assertIs(decision.action, RemediationAction.TERRAFORM_PATCH)
        self.assertEqual(
            repository.workflow_calls[0]["outbox"].task.command.value, "GENERATE_REMEDIATION"
        )
        self.assertEqual(len(dispatcher.tasks), 1)

    def test_actual_fail_with_passing_iac_yields_actual_sync(self):
        # AWS_ACTUAL FAIL but IAC PASS on the same resource×rule → sync the drift.
        service, repository, _ = _service(EvaluationStatus.PASS)

        response = service.create_remediation(_principal(), FINDING_ID)

        self.assertTrue(response.accepted)
        decision = repository.workflow_calls[0]["decision"]
        self.assertIs(decision.action, RemediationAction.ACTUAL_SYNC)
        self.assertEqual(
            repository.workflow_calls[0]["outbox"].task.command.value, "SYNC_ACTUAL_STATE"
        )


if __name__ == "__main__":
    unittest.main()
