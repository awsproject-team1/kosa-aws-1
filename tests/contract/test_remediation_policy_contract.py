"""Contract tests for the remediation scope, exception, and decision values."""

import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

from packages.contracts import (
    EvaluationPerspective,
    EvaluationStatus,
    ManualReviewCode,
    RemediationAction,
    RemediationDecision,
    RemediationEligibility,
    RemediationException,
    RemediationExceptionReason,
    RemediationRuleScope,
    RemediationTarget,
)

CUSTOMER = "customer-001"
RESOURCE = "arn:aws:s3:::example-bucket"
RULE_ID = "S3-PUBLIC-001"
RULE_VERSION = "2026-08-31"


def _exception(**overrides: object) -> RemediationException:
    fields: dict[str, object] = {
        "exception_id": "exception-001",
        "customer_id": CUSTOMER,
        "rule_id": RULE_ID,
        "rule_version": RULE_VERSION,
        "reason": RemediationExceptionReason.COMPENSATING_CONTROL,
        "approved_by": "security-owner",
        "approved_at": "2026-08-01T00:00:00+00:00",
        "expires_at": "2026-12-31T00:00:00+00:00",
    }
    fields.update(overrides)
    return RemediationException(**fields)  # type: ignore[arg-type]


def _decision(**overrides: object) -> RemediationDecision:
    fields: dict[str, object] = {
        "finding_id": "finding-001",
        "resource_id": RESOURCE,
        "rule_id": RULE_ID,
        "rule_version": RULE_VERSION,
        "perspective": EvaluationPerspective.IAC,
        "action": RemediationAction.TERRAFORM_PATCH,
    }
    fields.update(overrides)
    return RemediationDecision(**fields)  # type: ignore[arg-type]


class RemediationEnumContractTests(unittest.TestCase):
    def test_the_action_set_is_fixed(self):
        self.assertEqual(
            {action.value for action in RemediationAction},
            {"TERRAFORM_PATCH", "ACTUAL_SYNC", "MANUAL_REVIEW", "SUPPRESSED"},
        )

    def test_eligibility_has_exactly_two_states(self):
        self.assertEqual(
            {value.value for value in RemediationEligibility}, {"AUTOMATIC", "MANUAL_ONLY"}
        )

    def test_every_manual_review_reason_is_an_enumerated_code(self):
        self.assertEqual(
            {code.value for code in ManualReviewCode},
            {
                "RULE_NOT_IN_SCOPE",
                "RULE_MANUAL_ONLY",
                "RESOURCE_NOT_IAC_MANAGED",
                "IAC_OUTCOME_UNKNOWN",
                "INSUFFICIENT_EVIDENCE",
                "EVALUATION_REQUIRES_REVIEW",
            },
        )

    def test_exception_reasons_are_enumerated(self):
        self.assertEqual(
            {reason.value for reason in RemediationExceptionReason},
            {"ACCEPTED_RISK", "COMPENSATING_CONTROL", "NOT_APPLICABLE", "PLANNED_CHANGE"},
        )


class RemediationRuleScopeContractTests(unittest.TestCase):
    def test_a_scope_is_immutable(self):
        scope = RemediationRuleScope(
            rule_id=RULE_ID, version=RULE_VERSION, eligibility=RemediationEligibility.AUTOMATIC
        )

        with self.assertRaises(FrozenInstanceError):
            scope.eligibility = RemediationEligibility.MANUAL_ONLY  # type: ignore[misc]

    def test_eligibility_must_be_an_enum_value(self):
        with self.assertRaises(TypeError):
            RemediationRuleScope(rule_id=RULE_ID, version=RULE_VERSION, eligibility="AUTOMATIC")  # type: ignore[arg-type]

    def test_a_scope_serializes_to_identifiers_only(self):
        scope = RemediationRuleScope(
            rule_id=RULE_ID, version=RULE_VERSION, eligibility=RemediationEligibility.MANUAL_ONLY
        )

        self.assertEqual(
            scope.to_dict(),
            {"rule_id": RULE_ID, "version": RULE_VERSION, "eligibility": "MANUAL_ONLY"},
        )


class RemediationExceptionContractTests(unittest.TestCase):
    def test_an_exception_is_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            _exception().expires_at = "2027-01-01T00:00:00+00:00"  # type: ignore[misc]

    def test_an_exception_must_expire_after_it_was_approved(self):
        with self.assertRaises(ValueError):
            _exception(expires_at="2026-07-01T00:00:00+00:00")

    def test_a_naive_timestamp_is_refused(self):
        for field in ("approved_at", "expires_at"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    _exception(**{field: "2026-08-01T00:00:00"})

    def test_a_malformed_timestamp_is_refused(self):
        with self.assertRaises(ValueError):
            _exception(expires_at="not-a-timestamp")

    def test_an_offset_other_than_utc_is_compared_correctly(self):
        exception = _exception(expires_at="2026-09-01T09:00:00+09:00")

        self.assertFalse(exception.is_active_at(datetime(2026, 9, 1, 0, 0, tzinfo=UTC)))
        self.assertTrue(exception.is_active_at(datetime(2026, 8, 31, 23, 0, tzinfo=UTC)))

    def test_activity_requires_an_offset_aware_moment(self):
        with self.assertRaises(ValueError):
            _exception().is_active_at(datetime(2026, 9, 1, 0, 0))

    def test_an_exception_serializes_its_full_scope(self):
        payload = _exception(resource_id=RESOURCE, ticket_reference="SEC-42").to_dict()

        self.assertEqual(
            payload,
            {
                "exception_id": "exception-001",
                "customer_id": CUSTOMER,
                "rule_id": RULE_ID,
                "rule_version": RULE_VERSION,
                "resource_id": RESOURCE,
                "reason": "COMPENSATING_CONTROL",
                "approved_by": "security-owner",
                "approved_at": "2026-08-01T00:00:00+00:00",
                "expires_at": "2026-12-31T00:00:00+00:00",
                "ticket_reference": "SEC-42",
            },
        )


class RemediationTargetContractTests(unittest.TestCase):
    def test_terraform_management_must_be_declared_as_a_bool(self):
        with self.assertRaises(TypeError):
            RemediationTarget(
                resource_id=RESOURCE,
                resource_type="AWS::S3::Bucket",
                terraform_managed="yes",  # type: ignore[arg-type]
            )

    def test_an_unknown_iac_outcome_is_representable(self):
        target = RemediationTarget(
            resource_id=RESOURCE, resource_type="AWS::S3::Bucket", terraform_managed=True
        )

        self.assertIsNone(target.to_dict()["iac_status"])

    def test_an_iac_outcome_serializes_as_its_status_value(self):
        target = RemediationTarget(
            resource_id=RESOURCE,
            resource_type="AWS::S3::Bucket",
            terraform_managed=True,
            iac_status=EvaluationStatus.PASS,
        )

        self.assertEqual(target.to_dict()["iac_status"], "PASS")


class RemediationDecisionContractTests(unittest.TestCase):
    def test_a_manual_review_decision_must_carry_a_code(self):
        with self.assertRaises(ValueError):
            _decision(action=RemediationAction.MANUAL_REVIEW)

    def test_only_a_manual_review_decision_may_carry_a_code(self):
        with self.assertRaises(ValueError):
            _decision(manual_review_code=ManualReviewCode.RULE_MANUAL_ONLY)

    def test_a_suppressed_decision_must_name_the_exception(self):
        with self.assertRaises(ValueError):
            _decision(action=RemediationAction.SUPPRESSED)

    def test_only_a_suppressed_decision_may_name_an_exception(self):
        with self.assertRaises(ValueError):
            _decision(exception_id="exception-001")

    def test_only_patch_and_sync_are_actionable(self):
        actionable = {
            RemediationAction.TERRAFORM_PATCH: _decision(),
            RemediationAction.ACTUAL_SYNC: _decision(action=RemediationAction.ACTUAL_SYNC),
        }
        for action, decision in actionable.items():
            with self.subTest(action=action):
                self.assertTrue(decision.is_actionable)

        self.assertFalse(
            _decision(
                action=RemediationAction.MANUAL_REVIEW,
                manual_review_code=ManualReviewCode.RULE_MANUAL_ONLY,
            ).is_actionable
        )
        self.assertFalse(
            _decision(
                action=RemediationAction.SUPPRESSED, exception_id="exception-001"
            ).is_actionable
        )

    def test_a_decision_serializes_the_rule_version_it_judged(self):
        payload = _decision().to_dict()

        self.assertEqual(payload["rule_version"], RULE_VERSION)
        self.assertEqual(payload["action"], "TERRAFORM_PATCH")
        self.assertIsNone(payload["manual_review_code"])
        self.assertIsNone(payload["exception_id"])


if __name__ == "__main__":
    unittest.main()
