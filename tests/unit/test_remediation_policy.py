"""Tests for the remediation scope, exception, and manual-review policy boundary."""

import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from apps.backend.policy import (
    PolicyRegistryError,
    RemediationPolicy,
    RemediationPolicyError,
    load_rule_registry,
)
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
RESOURCE = "arn:aws:s3:::example-bucket"
RULE_ID = "S3-PUBLIC-001"
RULE_VERSION = "2026-08-31"
S3 = "AWS::S3::Bucket"
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
REGISTRY = Path("fixtures/rules")


def _finding(**overrides: object) -> Finding:
    fields: dict[str, object] = {
        "finding_id": "finding-001",
        "resource_id": RESOURCE,
        "rule_id": RULE_ID,
        "rule_version": RULE_VERSION,
        "perspective": EvaluationPerspective.IAC,
        "status": EvaluationStatus.FAIL,
        "severity": "CRITICAL",
        "score": 0.0,
        "rationale": "block public access is not configured",
        "evidence_references": ("terraform:aws_s3_bucket.example",),
    }
    fields.update(overrides)
    return Finding(**fields)  # type: ignore[arg-type]


def _target(**overrides: object) -> RemediationTarget:
    fields: dict[str, object] = {
        "resource_id": RESOURCE,
        "resource_type": S3,
        "terraform_managed": True,
    }
    fields.update(overrides)
    return RemediationTarget(**fields)  # type: ignore[arg-type]


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


def _policy(eligibility: RemediationEligibility = RemediationEligibility.AUTOMATIC):
    return RemediationPolicy(
        [RemediationRuleScope(rule_id=RULE_ID, version=RULE_VERSION, eligibility=eligibility)]
    )


class RemediationScopeTests(unittest.TestCase):
    def test_an_automatic_iac_finding_becomes_a_terraform_patch(self):
        decision = _policy().decide(_finding(), customer_id=CUSTOMER, target=_target(), at=NOW)

        self.assertIs(decision.action, RemediationAction.TERRAFORM_PATCH)
        self.assertIsNone(decision.manual_review_code)
        self.assertTrue(decision.is_actionable)

    def test_a_manual_only_rule_is_never_patched(self):
        decision = _policy(RemediationEligibility.MANUAL_ONLY).decide(
            _finding(), customer_id=CUSTOMER, target=_target(), at=NOW
        )

        self.assertIs(decision.action, RemediationAction.MANUAL_REVIEW)
        self.assertIs(decision.manual_review_code, ManualReviewCode.RULE_MANUAL_ONLY)

    def test_a_manual_only_rule_still_syncs_a_drifted_actual(self):
        """허용 범위는 Patch 합성에 대한 판단이다. 동기화는 새 변경을 만들지 않는다."""
        for perspective in (EvaluationPerspective.AWS_ACTUAL, EvaluationPerspective.DRIFT):
            with self.subTest(perspective=perspective):
                decision = _policy(RemediationEligibility.MANUAL_ONLY).decide(
                    _finding(perspective=perspective),
                    customer_id=CUSTOMER,
                    target=_target(iac_status=EvaluationStatus.PASS),
                    at=NOW,
                )

                self.assertIs(decision.action, RemediationAction.ACTUAL_SYNC)

    def test_a_manual_only_rule_over_unsafe_iac_is_still_refused(self):
        decision = _policy(RemediationEligibility.MANUAL_ONLY).decide(
            _finding(perspective=EvaluationPerspective.DRIFT),
            customer_id=CUSTOMER,
            target=_target(iac_status=EvaluationStatus.FAIL),
            at=NOW,
        )

        self.assertIs(decision.manual_review_code, ManualReviewCode.RULE_MANUAL_ONLY)

    def test_an_unregistered_rule_is_not_synced_either(self):
        """판단의 부재는 `MANUAL_ONLY`라는 판단과 다르다. 전자는 동기화도 막는다."""
        decision = RemediationPolicy([]).decide(
            _finding(perspective=EvaluationPerspective.AWS_ACTUAL),
            customer_id=CUSTOMER,
            target=_target(iac_status=EvaluationStatus.PASS),
            at=NOW,
        )

        self.assertIs(decision.manual_review_code, ManualReviewCode.RULE_NOT_IN_SCOPE)

    def test_an_unregistered_rule_falls_closed_to_manual_review(self):
        decision = RemediationPolicy([]).decide(
            _finding(), customer_id=CUSTOMER, target=_target(), at=NOW
        )

        self.assertIs(decision.action, RemediationAction.MANUAL_REVIEW)
        self.assertIs(decision.manual_review_code, ManualReviewCode.RULE_NOT_IN_SCOPE)

    def test_scope_is_pinned_to_the_exact_rule_version(self):
        decision = _policy().decide(
            _finding(rule_version="2026-09-30"),
            customer_id=CUSTOMER,
            target=_target(),
            at=NOW,
        )

        self.assertIs(decision.manual_review_code, ManualReviewCode.RULE_NOT_IN_SCOPE)

    def test_a_resource_outside_terraform_is_manual_review(self):
        decision = _policy().decide(
            _finding(),
            customer_id=CUSTOMER,
            target=_target(terraform_managed=False),
            at=NOW,
        )

        self.assertIs(decision.manual_review_code, ManualReviewCode.RESOURCE_NOT_IAC_MANAGED)

    def test_duplicate_scope_entries_are_refused(self):
        scope = RemediationRuleScope(
            rule_id=RULE_ID, version=RULE_VERSION, eligibility=RemediationEligibility.AUTOMATIC
        )

        with self.assertRaises(RemediationPolicyError):
            RemediationPolicy([scope, scope])


class ActionSelectionTests(unittest.TestCase):
    """`docs/PRD.md` Assessment stages: 안전한 IaC를 다시 고치지 않는다."""

    def test_a_drifted_actual_over_safe_iac_is_synced_not_patched(self):
        decision = _policy().decide(
            _finding(perspective=EvaluationPerspective.AWS_ACTUAL),
            customer_id=CUSTOMER,
            target=_target(iac_status=EvaluationStatus.PASS),
            at=NOW,
        )

        self.assertIs(decision.action, RemediationAction.ACTUAL_SYNC)

    def test_a_drift_finding_over_unsafe_iac_is_patched(self):
        decision = _policy().decide(
            _finding(perspective=EvaluationPerspective.DRIFT),
            customer_id=CUSTOMER,
            target=_target(iac_status=EvaluationStatus.FAIL),
            at=NOW,
        )

        self.assertIs(decision.action, RemediationAction.TERRAFORM_PATCH)

    def test_an_actual_finding_without_an_iac_outcome_is_manual_review(self):
        decision = _policy().decide(
            _finding(perspective=EvaluationPerspective.AWS_ACTUAL),
            customer_id=CUSTOMER,
            target=_target(),
            at=NOW,
        )

        self.assertIs(decision.manual_review_code, ManualReviewCode.IAC_OUTCOME_UNKNOWN)

    def test_an_unevaluated_iac_outcome_is_not_read_as_safe(self):
        for status in (
            EvaluationStatus.OUT_OF_SCOPE,
            EvaluationStatus.EXECUTION_ERROR,
            EvaluationStatus.MANUAL_REVIEW,
            EvaluationStatus.INSUFFICIENT_EVIDENCE,
        ):
            with self.subTest(status=status):
                decision = _policy().decide(
                    _finding(perspective=EvaluationPerspective.DRIFT),
                    customer_id=CUSTOMER,
                    target=_target(iac_status=status),
                    at=NOW,
                )

                self.assertIs(decision.manual_review_code, ManualReviewCode.IAC_OUTCOME_UNKNOWN)

    def test_an_iac_finding_does_not_need_an_iac_outcome(self):
        decision = _policy().decide(_finding(), customer_id=CUSTOMER, target=_target(), at=NOW)

        self.assertIs(decision.action, RemediationAction.TERRAFORM_PATCH)

    def test_unevaluated_findings_are_not_remediated(self):
        cases = {
            EvaluationStatus.INSUFFICIENT_EVIDENCE: ManualReviewCode.INSUFFICIENT_EVIDENCE,
            EvaluationStatus.MANUAL_REVIEW: ManualReviewCode.EVALUATION_REQUIRES_REVIEW,
        }
        for status, code in cases.items():
            with self.subTest(status=status):
                decision = _policy().decide(
                    _finding(status=status),
                    customer_id=CUSTOMER,
                    target=_target(),
                    at=NOW,
                )

                self.assertIs(decision.action, RemediationAction.MANUAL_REVIEW)
                self.assertIs(decision.manual_review_code, code)

    def test_a_target_for_another_resource_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            _policy().decide(
                _finding(),
                customer_id=CUSTOMER,
                target=_target(resource_id="arn:aws:s3:::other-bucket"),
                at=NOW,
            )


class RemediationExceptionTests(unittest.TestCase):
    def test_an_active_exception_suppresses_the_finding(self):
        decision = _policy().decide(
            _finding(),
            customer_id=CUSTOMER,
            target=_target(),
            at=NOW,
            exceptions=[_exception()],
        )

        self.assertIs(decision.action, RemediationAction.SUPPRESSED)
        self.assertEqual(decision.exception_id, "exception-001")
        self.assertFalse(decision.is_actionable)

    def test_an_expired_exception_no_longer_covers_the_finding(self):
        expired = _exception(expires_at="2026-08-15T00:00:00+00:00")

        decision = _policy().decide(
            _finding(), customer_id=CUSTOMER, target=_target(), at=NOW, exceptions=[expired]
        )

        self.assertIs(decision.action, RemediationAction.TERRAFORM_PATCH)

    def test_an_exception_expires_at_its_exact_boundary(self):
        exception = _exception(expires_at=NOW.isoformat())

        self.assertFalse(exception.is_active_at(NOW))
        self.assertTrue(exception.is_active_at(NOW - timedelta(seconds=1)))

    def test_an_exception_does_not_follow_a_rule_to_a_new_version(self):
        decision = _policy().decide(
            _finding(),
            customer_id=CUSTOMER,
            target=_target(),
            at=NOW,
            exceptions=[_exception(rule_version="2026-07-01")],
        )

        self.assertIs(decision.action, RemediationAction.TERRAFORM_PATCH)

    def test_another_customers_exception_never_applies(self):
        decision = _policy().decide(
            _finding(),
            customer_id=CUSTOMER,
            target=_target(),
            at=NOW,
            exceptions=[_exception(customer_id="customer-002")],
        )

        self.assertIs(decision.action, RemediationAction.TERRAFORM_PATCH)

    def test_a_resource_scoped_exception_covers_only_that_resource(self):
        scoped = _exception(resource_id="arn:aws:s3:::other-bucket")

        decision = _policy().decide(
            _finding(), customer_id=CUSTOMER, target=_target(), at=NOW, exceptions=[scoped]
        )

        self.assertIs(decision.action, RemediationAction.TERRAFORM_PATCH)

    def test_a_rule_wide_exception_covers_every_resource(self):
        decision = _policy().decide(
            _finding(), customer_id=CUSTOMER, target=_target(), at=NOW, exceptions=[_exception()]
        )

        self.assertIs(decision.action, RemediationAction.SUPPRESSED)

    def test_the_narrowest_exception_wins_regardless_of_order(self):
        rule_wide = _exception(exception_id="exception-wide")
        resource_scoped = _exception(exception_id="exception-narrow", resource_id=RESOURCE)

        for order in ((rule_wide, resource_scoped), (resource_scoped, rule_wide)):
            with self.subTest(order=[exception.exception_id for exception in order]):
                decision = _policy().decide(
                    _finding(),
                    customer_id=CUSTOMER,
                    target=_target(),
                    at=NOW,
                    exceptions=list(order),
                )

                self.assertEqual(decision.exception_id, "exception-narrow")

    def test_an_exception_suppresses_a_manual_only_rule_too(self):
        """예외는 '조치하지 않는다'는 결정이므로 조치 유형보다 앞선다."""
        decision = _policy(RemediationEligibility.MANUAL_ONLY).decide(
            _finding(), customer_id=CUSTOMER, target=_target(), at=NOW, exceptions=[_exception()]
        )

        self.assertIs(decision.action, RemediationAction.SUPPRESSED)


def _registry_without_remediation(target: Path) -> Path:
    """Copy the committed registry into `target` with the remediation scope left out."""
    for path in REGISTRY.glob("*.json"):
        if path.name == "remediation.json":
            continue
        (target / path.name).write_bytes(path.read_bytes())
    return target


class CommittedRemediationRegistryTests(unittest.TestCase):
    def test_the_committed_registry_classifies_every_rule(self):
        registry = load_rule_registry(REGISTRY)

        uncovered = [
            f"{rule.rule_id}@{rule.version}"
            for rule in registry.rules
            if registry.remediation.eligibility(rule_id=rule.rule_id, version=rule.version) is None
        ]

        self.assertEqual(uncovered, [])

    def test_rules_without_a_uniquely_determined_safe_state_are_manual_only(self):
        registry = load_rule_registry(REGISTRY)

        for rule_id in ("S3-POLICY-001", "S3-LOGGING-001", "EC2-EBS-ENCRYPT-001"):
            with self.subTest(rule_id=rule_id):
                self.assertIs(
                    registry.remediation.eligibility(rule_id=rule_id, version=RULE_VERSION),
                    RemediationEligibility.MANUAL_ONLY,
                )

    def test_a_registry_without_a_remediation_file_opens_nothing(self):
        with TemporaryDirectory() as directory:
            target = _registry_without_remediation(Path(directory))

            registry = load_rule_registry(target)

            self.assertEqual(registry.remediation.scopes, ())
            self.assertIsNone(
                registry.remediation.eligibility(rule_id=RULE_ID, version=RULE_VERSION)
            )

    def test_a_scope_for_an_unknown_rule_is_refused(self):
        with TemporaryDirectory() as directory:
            target = _registry_without_remediation(Path(directory))
            (target / "remediation.json").write_text(
                json.dumps(
                    [
                        {
                            "rule_id": "S3-TYPO-001",
                            "version": RULE_VERSION,
                            "eligibility": "AUTOMATIC",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaises(PolicyRegistryError):
                load_rule_registry(target)


if __name__ == "__main__":
    unittest.main()
