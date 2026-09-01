"""Security boundary tests for the remediation scope and exception policy.

세 가지를 고정한다. (1) 다른 고객의 예외는 어떤 형태로도 적용되지 않는다. (2) 허용 범위가
비어 있거나 모르는 Rule이 들어오면 자동 조치가 열리지 않는다. (3) 판정 결과에 Finding의
근거 문장이나 Evidence가 실려 나가지 않는다.
"""

import unittest
from datetime import UTC, datetime

from apps.backend.policy import RemediationPolicy
from packages.contracts import (
    EvaluationPerspective,
    EvaluationStatus,
    Finding,
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

        decision = POLICY.decide(
            FINDING, customer_id=CUSTOMER, target=TARGET, at=NOW, exceptions=[foreign]
        )

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
                    POLICY.decide(
                        FINDING,
                        customer_id=customer_id,
                        target=TARGET,
                        at=NOW,
                        exceptions=[foreign],
                    )


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
                    ),
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


class RemediationDecisionExposureTests(unittest.TestCase):
    def test_a_decision_does_not_carry_the_finding_rationale_or_evidence(self):
        decision = POLICY.decide(FINDING, customer_id=CUSTOMER, target=TARGET, at=NOW)

        serialized = repr(decision.to_dict()) + repr(decision)

        self.assertNotIn(RATIONALE, serialized)
        self.assertNotIn(EVIDENCE, serialized)

    def test_a_suppressed_decision_names_the_exception_without_its_reasoning(self):
        decision = POLICY.decide(
            FINDING,
            customer_id=CUSTOMER,
            target=TARGET,
            at=NOW,
            exceptions=[_exception(ticket_reference="SEC-42")],
        )

        payload = decision.to_dict()

        self.assertEqual(payload["exception_id"], "exception-001")
        self.assertNotIn("ticket_reference", payload)
        self.assertNotIn("reason", payload)


if __name__ == "__main__":
    unittest.main()
