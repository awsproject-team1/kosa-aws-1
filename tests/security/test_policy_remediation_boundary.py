"""Security boundary tests for the remediation scope and exception policy.

다섯 가지를 고정한다. (1) 다른 고객의 예외는 어떤 형태로도 적용되지 않는다. (2) 허용 범위가
비어 있거나 모르는 Rule이 들어오면 자동 조치가 열리지 않는다. (3) 판정 결과에 Finding의
근거 문장이나 Evidence가 실려 나가지 않는다. (4) Finding이 평가된 뒤에 승인된 예외는 그 Finding을
소급해 억제하지 못한다. (5) 평가되지 않은 commit은 IaC 판정을 물려받아 배포 대상이 되지 못한다.
"""

import unittest
from datetime import UTC, datetime

from apps.backend.policy import RemediationPolicy
from packages.contracts import (
    EvaluationPerspective,
    EvaluationStatus,
    Finding,
    ManualReviewCode,
    RemediationAction,
    RemediationEligibility,
    RemediationException,
    RemediationExceptionReason,
    RemediationRuleScope,
    RemediationTarget,
)

CUSTOMER = "customer-001"
OTHER_CUSTOMER = "customer-002"
RESOURCE = "arn:aws:s3:::example-bucket"
RULE_ID = "S3-PUBLIC-001"
RULE_VERSION = "2026-08-31"
RATIONALE = "bucket policy grants s3:GetObject to a public principal"
EVIDENCE = "terraform:aws_s3_bucket_policy.example#statement/0"
COMMIT = "a" * 40
EVALUATED_AT = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

FINDING = Finding(
    finding_id="finding-001",
    resource_id=RESOURCE,
    rule_id=RULE_ID,
    rule_version=RULE_VERSION,
    perspective=EvaluationPerspective.IAC,
    status=EvaluationStatus.FAIL,
    severity="CRITICAL",
    score=0.0,
    rationale=RATIONALE,
    evidence_references=(EVIDENCE,),
)
TARGET = RemediationTarget(
    resource_id=RESOURCE,
    resource_type="AWS::S3::Bucket",
    rule_id=RULE_ID,
    rule_version=RULE_VERSION,
    terraform_managed=True,
)
POLICY = RemediationPolicy(
    [
        RemediationRuleScope(
            rule_id=RULE_ID, version=RULE_VERSION, eligibility=RemediationEligibility.AUTOMATIC
        )
    ]
)


def _decide(finding: Finding, **overrides: object):
    """Call `decide()` with the request-shaped defaults every test shares."""
    kwargs: dict[str, object] = {
        "customer_id": CUSTOMER,
        "target": TARGET,
        "commit_sha": COMMIT,
        "finding_evaluated_at": EVALUATED_AT,
        "at": NOW,
    }
    kwargs.update(overrides)
    return POLICY.decide(finding, **kwargs)  # type: ignore[arg-type]


def _exception(**overrides: object) -> RemediationException:
    fields: dict[str, object] = {
        "exception_id": "exception-001",
        "customer_id": CUSTOMER,
        "rule_id": RULE_ID,
        "rule_version": RULE_VERSION,
        "reason": RemediationExceptionReason.ACCEPTED_RISK,
        "approved_by": "security-owner",
        "approved_at": "2026-08-01T00:00:00+00:00",
        "expires_at": "2026-12-31T00:00:00+00:00",
    }
    fields.update(overrides)
    return RemediationException(**fields)  # type: ignore[arg-type]


class ExceptionTenantIsolationTests(unittest.TestCase):
    def test_a_rule_wide_exception_does_not_cross_customers(self):
        foreign = _exception(customer_id=OTHER_CUSTOMER)

        decision = _decide(FINDING, exceptions=[foreign])

        self.assertIs(decision.action, RemediationAction.TERRAFORM_PATCH)
        self.assertIsNone(decision.exception_id)

    def test_a_resource_scoped_exception_does_not_cross_customers(self):
        foreign = _exception(customer_id=OTHER_CUSTOMER, resource_id=RESOURCE)

        self.assertFalse(
            foreign.covers(
                customer_id=CUSTOMER,
                rule_id=RULE_ID,
                rule_version=RULE_VERSION,
                resource_id=RESOURCE,
            )
        )

    def test_a_foreign_exception_cannot_be_reused_by_omitting_the_customer(self):
        foreign = _exception(customer_id=OTHER_CUSTOMER)

        for customer_id in ("", "   "):
            with self.subTest(customer_id=repr(customer_id)):
                with self.assertRaises(ValueError):
                    _decide(FINDING, customer_id=customer_id, exceptions=[foreign])


class RemediationFailClosedTests(unittest.TestCase):
    def test_an_empty_scope_never_produces_an_actionable_decision(self):
        empty = RemediationPolicy([])

        for perspective in EvaluationPerspective:
            with self.subTest(perspective=perspective):
                decision = empty.decide(
                    Finding(
                        finding_id="finding-001",
                        resource_id=RESOURCE,
                        rule_id=RULE_ID,
                        rule_version=RULE_VERSION,
                        perspective=perspective,
                        status=EvaluationStatus.FAIL,
                        severity="CRITICAL",
                        score=0.0,
                        rationale=RATIONALE,
                        evidence_references=(EVIDENCE,),
                    ),
                    customer_id=CUSTOMER,
                    target=RemediationTarget(
                        resource_id=RESOURCE,
                        resource_type="AWS::S3::Bucket",
                        rule_id=RULE_ID,
                        rule_version=RULE_VERSION,
                        terraform_managed=True,
                        iac_status=EvaluationStatus.PASS,
                        iac_perspective=EvaluationPerspective.IAC,
                        iac_commit_sha=COMMIT,
                    ),
                    commit_sha=COMMIT,
                    finding_evaluated_at=EVALUATED_AT,
                    at=NOW,
                )

                self.assertFalse(decision.is_actionable)

    def test_an_exception_cannot_be_permanent(self):
        with self.assertRaises(TypeError):
            _exception(expires_at=None)

    def test_an_exception_is_not_active_after_it_expires(self):
        expired = _exception(expires_at="2026-08-31T23:59:59+00:00")

        self.assertFalse(expired.is_active_at(NOW))
        self.assertTrue(
            expired.covers(
                customer_id=CUSTOMER,
                rule_id=RULE_ID,
                rule_version=RULE_VERSION,
                resource_id=RESOURCE,
            )
        )

    def test_an_exception_approved_after_the_violation_cannot_suppress_it(self):
        """사후 승인으로 감사 기록을 지울 수 없다. 승인은 Finding 평가보다 앞서야 한다."""
        backdated = _exception(
            approved_at="2026-09-01T11:00:00+00:00", expires_at="2026-12-31T00:00:00+00:00"
        )

        self.assertTrue(backdated.is_active_at(NOW))

        decision = _decide(FINDING, exceptions=[backdated])

        self.assertIs(decision.action, RemediationAction.TERRAFORM_PATCH)
        self.assertIsNone(decision.exception_id)

    def test_a_verdict_from_an_unassessed_commit_cannot_open_a_sync(self):
        """평가되지 않은 commit을 배포 대상으로 삼는 경로가 열리지 않는다."""
        actual_finding = Finding(
            finding_id="finding-002",
            resource_id=RESOURCE,
            rule_id=RULE_ID,
            rule_version=RULE_VERSION,
            perspective=EvaluationPerspective.AWS_ACTUAL,
            status=EvaluationStatus.FAIL,
            severity="CRITICAL",
            score=0.0,
            rationale=RATIONALE,
            evidence_references=(EVIDENCE,),
        )

        decision = _decide(
            actual_finding,
            target=RemediationTarget(
                resource_id=RESOURCE,
                resource_type="AWS::S3::Bucket",
                rule_id=RULE_ID,
                rule_version=RULE_VERSION,
                terraform_managed=True,
                iac_status=EvaluationStatus.PASS,
                iac_perspective=EvaluationPerspective.IAC,
                iac_commit_sha=COMMIT,
            ),
            commit_sha="b" * 40,
        )

        self.assertFalse(decision.is_actionable)
        self.assertIs(decision.manual_review_code, ManualReviewCode.IAC_VERDICT_COMMIT_MISMATCH)


class RemediationDecisionExposureTests(unittest.TestCase):
    def test_a_decision_does_not_carry_the_finding_rationale_or_evidence(self):
        decision = _decide(FINDING)

        serialized = repr(decision.to_dict()) + repr(decision)

        self.assertNotIn(RATIONALE, serialized)
        self.assertNotIn(EVIDENCE, serialized)

    def test_a_suppressed_decision_names_the_exception_without_its_reasoning(self):
        decision = _decide(FINDING, exceptions=[_exception(ticket_reference="SEC-42")])

        payload = decision.to_dict()

        self.assertEqual(payload["exception_id"], "exception-001")
        self.assertNotIn("ticket_reference", payload)
        self.assertNotIn("reason", payload)


if __name__ == "__main__":
    unittest.main()
