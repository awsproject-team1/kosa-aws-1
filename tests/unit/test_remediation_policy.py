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
COMMIT = "a" * 40
NEWER_COMMIT = "b" * 40
#: Finding이 평가된 시각. 조치 요청(`NOW`)보다 앞선다 — 사람이 결과를 보고 고르는 흐름이다.
EVALUATED_AT = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
REGISTRY = Path("fixtures/rules")


def _decide(policy: RemediationPolicy, finding: Finding, **overrides: object):
    """Call `decide()` with the request-shaped defaults every test shares."""
    kwargs: dict[str, object] = {
        "customer_id": CUSTOMER,
        "commit_sha": COMMIT,
        "finding_evaluated_at": EVALUATED_AT,
        "at": NOW,
    }
    kwargs.update(overrides)
    return policy.decide(finding, **kwargs)  # type: ignore[arg-type]


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
        "rule_id": RULE_ID,
        "rule_version": RULE_VERSION,
        "terraform_managed": True,
    }
    if overrides.get("iac_status") is not None:
        fields.setdefault("iac_perspective", EvaluationPerspective.IAC)
        fields.setdefault("iac_commit_sha", COMMIT)
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
        decision = _decide(_policy(), _finding(), target=_target())

        self.assertIs(decision.action, RemediationAction.TERRAFORM_PATCH)
        self.assertIsNone(decision.manual_review_code)
        self.assertTrue(decision.is_actionable)

    def test_a_manual_only_rule_is_never_patched(self):
        decision = _decide(
            _policy(RemediationEligibility.MANUAL_ONLY), _finding(), target=_target()
        )

        self.assertIs(decision.action, RemediationAction.MANUAL_REVIEW)
        self.assertIs(decision.manual_review_code, ManualReviewCode.RULE_MANUAL_ONLY)

    def test_a_manual_only_rule_still_syncs_a_drifted_actual(self):
        """허용 범위는 Patch 합성에 대한 판단이다. 동기화는 새 변경을 만들지 않는다."""
        for perspective in (EvaluationPerspective.AWS_ACTUAL, EvaluationPerspective.DRIFT):
            with self.subTest(perspective=perspective):
                decision = _decide(
                    _policy(RemediationEligibility.MANUAL_ONLY),
                    _finding(perspective=perspective),
                    target=_target(iac_status=EvaluationStatus.PASS),
                )

                self.assertIs(decision.action, RemediationAction.ACTUAL_SYNC)

    def test_a_manual_only_rule_over_unsafe_iac_is_still_refused(self):
        decision = _decide(
            _policy(RemediationEligibility.MANUAL_ONLY),
            _finding(perspective=EvaluationPerspective.DRIFT),
            target=_target(iac_status=EvaluationStatus.FAIL),
        )

        self.assertIs(decision.manual_review_code, ManualReviewCode.RULE_MANUAL_ONLY)

    def test_an_unregistered_rule_is_not_synced_either(self):
        """판단의 부재는 `MANUAL_ONLY`라는 판단과 다르다. 전자는 동기화도 막는다."""
        decision = _decide(
            RemediationPolicy([]),
            _finding(perspective=EvaluationPerspective.AWS_ACTUAL),
            target=_target(iac_status=EvaluationStatus.PASS),
        )

        self.assertIs(decision.manual_review_code, ManualReviewCode.RULE_NOT_IN_SCOPE)

    def test_an_unregistered_rule_falls_closed_to_manual_review(self):
        decision = _decide(RemediationPolicy([]), _finding(), target=_target())

        self.assertIs(decision.action, RemediationAction.MANUAL_REVIEW)
        self.assertIs(decision.manual_review_code, ManualReviewCode.RULE_NOT_IN_SCOPE)

    def test_scope_is_pinned_to_the_exact_rule_version(self):
        decision = _decide(
            _policy(),
            _finding(rule_version="2026-09-30"),
            target=_target(rule_version="2026-09-30"),
        )

        self.assertIs(decision.manual_review_code, ManualReviewCode.RULE_NOT_IN_SCOPE)

    def test_a_resource_outside_terraform_is_manual_review(self):
        decision = _decide(_policy(), _finding(), target=_target(terraform_managed=False))

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
        decision = _decide(
            _policy(),
            _finding(perspective=EvaluationPerspective.AWS_ACTUAL),
            target=_target(iac_status=EvaluationStatus.PASS),
        )

        self.assertIs(decision.action, RemediationAction.ACTUAL_SYNC)

    def test_a_drift_finding_over_unsafe_iac_is_patched(self):
        decision = _decide(
            _policy(),
            _finding(perspective=EvaluationPerspective.DRIFT),
            target=_target(iac_status=EvaluationStatus.FAIL),
        )

        self.assertIs(decision.action, RemediationAction.TERRAFORM_PATCH)

    def test_an_actual_finding_without_an_iac_outcome_is_manual_review(self):
        decision = _decide(
            _policy(), _finding(perspective=EvaluationPerspective.AWS_ACTUAL), target=_target()
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
                decision = _decide(
                    _policy(),
                    _finding(perspective=EvaluationPerspective.DRIFT),
                    target=_target(iac_status=status),
                )

                self.assertIs(decision.manual_review_code, ManualReviewCode.IAC_OUTCOME_UNKNOWN)

    def test_an_iac_finding_does_not_need_an_iac_outcome(self):
        decision = _decide(_policy(), _finding(), target=_target())

        self.assertIs(decision.action, RemediationAction.TERRAFORM_PATCH)

    def test_unevaluated_findings_are_not_remediated(self):
        cases = {
            EvaluationStatus.INSUFFICIENT_EVIDENCE: ManualReviewCode.INSUFFICIENT_EVIDENCE,
            EvaluationStatus.MANUAL_REVIEW: ManualReviewCode.EVALUATION_REQUIRES_REVIEW,
        }
        for status, code in cases.items():
            with self.subTest(status=status):
                decision = _decide(_policy(), _finding(status=status), target=_target())

                self.assertIs(decision.action, RemediationAction.MANUAL_REVIEW)
                self.assertIs(decision.manual_review_code, code)

    def test_a_target_for_another_resource_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            _decide(_policy(), _finding(), target=_target(resource_id="arn:aws:s3:::other-bucket"))

    def test_a_target_for_another_rule_version_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            _decide(_policy(), _finding(), target=_target(rule_version="2026-07-01"))

    def test_an_iac_outcome_from_another_rule_cannot_open_a_sync(self):
        """같은 리소스라도 다른 Rule의 `PASS`는 이 Rule의 IaC가 안전하다는 근거가 아니다.

        대조하지 않으면 안전하지 않은 IaC를 현재 commit 그대로 배포 대상으로 삼게 된다.
        """
        with self.assertRaises(ValueError):
            _decide(
                _policy(),
                _finding(perspective=EvaluationPerspective.AWS_ACTUAL),
                target=_target(rule_id="S3-ENCRYPT-001", iac_status=EvaluationStatus.PASS),
            )

    def test_an_actual_outcome_cannot_masquerade_as_the_iac_outcome(self):
        with self.assertRaises(ValueError):
            _target(
                iac_status=EvaluationStatus.PASS,
                iac_perspective=EvaluationPerspective.AWS_ACTUAL,
            )


class IacVerdictCommitBindingTests(unittest.TestCase):
    """`ACTUAL_SYNC`는 `IAC` 관점이 통과시킨 **그** commit만 배포 대상으로 삼는다 (ADR-0017)."""

    def test_a_verdict_from_another_commit_cannot_open_a_sync(self):
        """평가 뒤 Repository가 진행하면 옛 `PASS`는 새 commit에 대해 아무것도 말하지 않는다."""
        for perspective in (EvaluationPerspective.AWS_ACTUAL, EvaluationPerspective.DRIFT):
            with self.subTest(perspective=perspective):
                decision = _decide(
                    _policy(),
                    _finding(perspective=perspective),
                    target=_target(iac_status=EvaluationStatus.PASS, iac_commit_sha=NEWER_COMMIT),
                )

                self.assertIs(decision.action, RemediationAction.MANUAL_REVIEW)
                self.assertIs(
                    decision.manual_review_code, ManualReviewCode.IAC_VERDICT_COMMIT_MISMATCH
                )

    def test_a_stale_failing_verdict_does_not_open_a_patch_either(self):
        """옛 commit의 `FAIL`은 이미 고쳐졌을 수 있다. 그 위에 Patch를 합성하면 수정을 되돌린다."""
        decision = _decide(
            _policy(),
            _finding(perspective=EvaluationPerspective.DRIFT),
            target=_target(iac_status=EvaluationStatus.FAIL, iac_commit_sha=NEWER_COMMIT),
        )

        self.assertIs(decision.manual_review_code, ManualReviewCode.IAC_VERDICT_COMMIT_MISMATCH)

    def test_a_verdict_from_the_remediated_commit_is_used(self):
        decision = _decide(
            _policy(),
            _finding(perspective=EvaluationPerspective.AWS_ACTUAL),
            target=_target(iac_status=EvaluationStatus.PASS, iac_commit_sha=COMMIT),
            commit_sha=COMMIT,
        )

        self.assertIs(decision.action, RemediationAction.ACTUAL_SYNC)

    def test_an_iac_finding_does_not_consult_the_verdict_commit(self):
        """`IAC` Finding은 자기 관점의 판정을 다시 읽지 않는다. Patch는 Snapshot에 바인딩된다."""
        decision = _decide(
            _policy(),
            _finding(),
            target=_target(iac_status=EvaluationStatus.FAIL, iac_commit_sha=NEWER_COMMIT),
        )

        self.assertIs(decision.action, RemediationAction.TERRAFORM_PATCH)

    def test_a_verdict_without_its_source_commit_is_not_representable(self):
        with self.assertRaises(ValueError):
            RemediationTarget(
                resource_id=RESOURCE,
                resource_type=S3,
                rule_id=RULE_ID,
                rule_version=RULE_VERSION,
                terraform_managed=True,
                iac_status=EvaluationStatus.PASS,
                iac_perspective=EvaluationPerspective.IAC,
            )

    def test_the_remediated_commit_must_be_declared(self):
        for commit_sha in ("", "   "):
            with self.subTest(commit_sha=repr(commit_sha)):
                with self.assertRaises(ValueError):
                    _decide(_policy(), _finding(), target=_target(), commit_sha=commit_sha)


class RemediationExceptionTests(unittest.TestCase):
    def test_an_active_exception_suppresses_the_finding(self):
        decision = _decide(_policy(), _finding(), target=_target(), exceptions=[_exception()])

        self.assertIs(decision.action, RemediationAction.SUPPRESSED)
        self.assertEqual(decision.exception_id, "exception-001")
        self.assertFalse(decision.is_actionable)

    def test_an_expired_exception_no_longer_covers_the_finding(self):
        expired = _exception(expires_at="2026-08-15T00:00:00+00:00")

        decision = _decide(_policy(), _finding(), target=_target(), exceptions=[expired])

        self.assertIs(decision.action, RemediationAction.TERRAFORM_PATCH)

    def test_an_exception_expires_at_its_exact_boundary(self):
        exception = _exception(expires_at=NOW.isoformat())

        self.assertFalse(exception.is_active_at(NOW))
        self.assertTrue(exception.is_active_at(NOW - timedelta(seconds=1)))

    def test_an_exception_does_not_follow_a_rule_to_a_new_version(self):
        decision = _decide(
            _policy(),
            _finding(),
            target=_target(),
            exceptions=[_exception(rule_version="2026-07-01")],
        )

        self.assertIs(decision.action, RemediationAction.TERRAFORM_PATCH)

    def test_another_customers_exception_never_applies(self):
        decision = _decide(
            _policy(),
            _finding(),
            target=_target(),
            exceptions=[_exception(customer_id="customer-002")],
        )

        self.assertIs(decision.action, RemediationAction.TERRAFORM_PATCH)

    def test_a_resource_scoped_exception_covers_only_that_resource(self):
        scoped = _exception(resource_id="arn:aws:s3:::other-bucket")

        decision = _decide(_policy(), _finding(), target=_target(), exceptions=[scoped])

        self.assertIs(decision.action, RemediationAction.TERRAFORM_PATCH)

    def test_a_rule_wide_exception_covers_every_resource(self):
        decision = _decide(_policy(), _finding(), target=_target(), exceptions=[_exception()])

        self.assertIs(decision.action, RemediationAction.SUPPRESSED)

    def test_the_narrowest_exception_wins_regardless_of_order(self):
        rule_wide = _exception(exception_id="exception-wide")
        resource_scoped = _exception(exception_id="exception-narrow", resource_id=RESOURCE)

        for order in ((rule_wide, resource_scoped), (resource_scoped, rule_wide)):
            with self.subTest(order=[exception.exception_id for exception in order]):
                decision = _decide(_policy(), _finding(), target=_target(), exceptions=list(order))

                self.assertEqual(decision.exception_id, "exception-narrow")

    def test_an_exception_approved_after_the_finding_does_not_suppress_it(self):
        """소급 억제 금지. 판정 시각에 유효해도, 위반 시점에는 아무도 면제를 승인하지 않았다.

        조치 요청이 승인보다 늦게 들어오는 것은 정상 흐름이므로, 만료만 판정 시각으로 보고
        승인까지 같은 시각으로 보면 이 경로가 열린다 (ADR-0017).
        """
        approved_later = _exception(
            approved_at="2026-09-01T10:00:00+00:00",
            expires_at="2026-12-31T00:00:00+00:00",
        )

        decision = _decide(_policy(), _finding(), target=_target(), exceptions=[approved_later])

        self.assertIs(decision.action, RemediationAction.TERRAFORM_PATCH)
        self.assertIsNone(decision.exception_id)

    def test_an_exception_approved_before_the_finding_still_expires_at_decision_time(self):
        """평가 시점에 유효했다는 사실이 이미 만료된 예외를 되살리지는 않는다."""
        expired_since = _exception(
            approved_at="2026-08-01T00:00:00+00:00",
            expires_at="2026-09-01T10:00:00+00:00",
        )

        self.assertTrue(expired_since.is_active_at(EVALUATED_AT))

        decision = _decide(_policy(), _finding(), target=_target(), exceptions=[expired_since])

        self.assertIs(decision.action, RemediationAction.TERRAFORM_PATCH)

    def test_an_exception_applies_at_the_exact_evaluation_moment(self):
        exception = _exception(approved_at=EVALUATED_AT.isoformat())

        decision = _decide(_policy(), _finding(), target=_target(), exceptions=[exception])

        self.assertIs(decision.action, RemediationAction.SUPPRESSED)

    def test_an_exception_does_not_apply_before_it_was_approved(self):
        """나중에 승인된 예외가 그 이전에 평가된 Finding을 소급해 덮지 않는다."""
        decision = _decide(
            _policy(),
            _finding(),
            target=_target(),
            exceptions=[
                _exception(
                    approved_at="2026-10-01T00:00:00+00:00",
                    expires_at="2026-12-31T00:00:00+00:00",
                )
            ],
        )

        self.assertIs(decision.action, RemediationAction.TERRAFORM_PATCH)

    def test_an_exception_activates_at_its_exact_approval_moment(self):
        exception = _exception(approved_at=NOW.isoformat())

        self.assertTrue(exception.is_active_at(NOW))
        self.assertFalse(exception.is_active_at(NOW - timedelta(seconds=1)))

    def test_equally_narrow_exceptions_are_chosen_independently_of_input_order(self):
        """저장소 조회 순서가 달라져도 감사 기록에 남는 `exception_id`는 같아야 한다."""
        first = _exception(exception_id="exception-a", resource_id=RESOURCE)
        second = _exception(exception_id="exception-b", resource_id=RESOURCE)

        for order in ((first, second), (second, first)):
            with self.subTest(order=[exception.exception_id for exception in order]):
                decision = _decide(_policy(), _finding(), target=_target(), exceptions=list(order))

                self.assertEqual(decision.exception_id, "exception-a")

    def test_an_exception_suppresses_a_manual_only_rule_too(self):
        """예외는 '조치하지 않는다'는 결정이므로 조치 유형보다 앞선다."""
        decision = _decide(
            _policy(RemediationEligibility.MANUAL_ONLY),
            _finding(),
            target=_target(),
            exceptions=[_exception()],
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

        for rule_id in (
            "S3-POLICY-001",
            "S3-ENCRYPT-001",
            "S3-LOGGING-001",
            "EC2-EBS-ENCRYPT-001",
        ):
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
