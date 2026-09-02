"""M3 B: verification re-evaluation scope and read-time exception display (ADR-0020 §2·§6)."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from apps.backend.policy import (
    FindingSuppression,
    InMemoryPolicyCatalog,
    PolicyContextResolver,
    PolicyNotFoundError,
    RemediationPolicy,
    annotate_suppressed_findings,
    load_rule_registry,
)
from packages.contracts import (
    AssessmentPhase,
    EvaluationPerspective,
    EvaluationStatus,
    Finding,
    PolicyProfile,
    PolicyRule,
    PolicyRuleReference,
    RemediationAction,
    RemediationEligibility,
    RemediationException,
    RemediationExceptionReason,
    RemediationRuleScope,
    RemediationTarget,
    RuleSeverity,
    SourceReference,
)

CUSTOMER = "customer-001"
RESOURCE = "arn:aws:s3:::example-bucket"
OTHER_RESOURCE = "arn:aws:s3:::other-bucket"
RULE_ID = "S3-PUBLIC-001"
RULE_VERSION = "2026-08-31"
S3 = "AWS::S3::Bucket"
COMMIT = "a" * 40
PROFILE_ID = "profile-mvp-baseline"
PROFILE_VERSION = "v2"
REGISTRY = Path("fixtures/rules")

#: Finding이 평가된 시각. 조회 시각(`READ_AT`)보다 앞선다 — 조회는 평가보다 늦다.
EVALUATED_AT = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
READ_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _source_reference() -> SourceReference:
    return SourceReference(
        source_id="isms-p-2023",
        source_version="2023-10-31",
        locator="control/2.9.4",
        content_sha256="a" * 64,
    )


def _rule(*, phases: tuple[AssessmentPhase, ...]) -> PolicyRule:
    return PolicyRule(
        rule_id=RULE_ID,
        version=RULE_VERSION,
        title="block public access",
        severity=RuleSeverity.CRITICAL,
        applicable_phases=phases,
        resource_types=(S3,),
        source_references=(_source_reference(),),
    )


def _catalog(*, profile_version: str, phases: tuple[AssessmentPhase, ...]) -> InMemoryPolicyCatalog:
    return InMemoryPolicyCatalog(
        profiles=(
            PolicyProfile(
                policy_profile_id=PROFILE_ID,
                version=profile_version,
                rule_references=(PolicyRuleReference(rule_id=RULE_ID, version=RULE_VERSION),),
            ),
        ),
        rules=(_rule(phases=phases),),
    )


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
        "assessed_commit_sha": COMMIT,
        "evaluated_at": EVALUATED_AT.isoformat(),
    }
    fields.update(overrides)
    return Finding(**fields)  # type: ignore[arg-type]


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


def _annotate(findings: object, **overrides: object) -> tuple[FindingSuppression, ...]:
    kwargs: dict[str, object] = {
        "customer_id": CUSTOMER,
        "exceptions": (_exception(),),
        "at": READ_AT,
    }
    kwargs.update(overrides)
    return annotate_suppressed_findings(findings, **kwargs)  # type: ignore[arg-type]


class VerificationScopeTests(unittest.TestCase):
    """ADR-0020 §2: 검증은 원 Assessment와 같은 Profile version, 같은 allow-list로만 돈다."""

    def test_the_pinned_profile_version_resolves_the_source_rule_set(self) -> None:
        resolver = PolicyContextResolver(
            _catalog(
                profile_version=PROFILE_VERSION,
                phases=(AssessmentPhase.INITIAL, AssessmentPhase.POST_DEPLOY_VERIFICATION),
            )
        )

        context = resolver.resolve(
            policy_profile_id=PROFILE_ID,
            phase=AssessmentPhase.POST_DEPLOY_VERIFICATION,
            resource_type=S3,
            expected_profile_version=PROFILE_VERSION,
        )

        self.assertEqual(context.policy_profile_version, PROFILE_VERSION)
        self.assertEqual([rule.rule_id for rule in context.rules], [RULE_ID])
        self.assertIs(context.phase, AssessmentPhase.POST_DEPLOY_VERIFICATION)

    def test_a_replaced_profile_version_is_not_evaluated_under_the_old_pin(self) -> None:
        """교체된 Profile은 다른 allow-list다. 같은 축에서 비교할 수 없으므로 평가하지 않는다."""
        resolver = PolicyContextResolver(
            _catalog(
                profile_version="v3",
                phases=(AssessmentPhase.INITIAL, AssessmentPhase.POST_DEPLOY_VERIFICATION),
            )
        )

        with self.assertRaises(PolicyNotFoundError):
            resolver.resolve(
                policy_profile_id=PROFILE_ID,
                phase=AssessmentPhase.POST_DEPLOY_VERIFICATION,
                resource_type=S3,
                expected_profile_version=PROFILE_VERSION,
            )

    def test_an_unpinned_resolution_still_reports_the_current_version(self) -> None:
        """pin 없이 해석하면 실패하지 않는다 — 호출자가 대조할 수 있도록 값이 실려 나온다."""
        resolver = PolicyContextResolver(
            _catalog(
                profile_version="v3",
                phases=(AssessmentPhase.POST_DEPLOY_VERIFICATION,),
            )
        )

        context = resolver.resolve(
            policy_profile_id=PROFILE_ID,
            phase=AssessmentPhase.POST_DEPLOY_VERIFICATION,
            resource_type=S3,
        )

        self.assertEqual(context.policy_profile_version, "v3")

    def test_a_rule_outside_the_verification_phase_is_not_resolved(self) -> None:
        resolver = PolicyContextResolver(
            _catalog(profile_version=PROFILE_VERSION, phases=(AssessmentPhase.INITIAL,))
        )

        with self.assertRaises(PolicyNotFoundError):
            resolver.resolve(
                policy_profile_id=PROFILE_ID,
                phase=AssessmentPhase.POST_DEPLOY_VERIFICATION,
                resource_type=S3,
                expected_profile_version=PROFILE_VERSION,
            )

    def test_every_committed_s3_rule_is_applicable_to_the_verification_phase(self) -> None:
        """재사용된 계획을 검증 phase가 전부 덮지 못하면 Coverage가 완성되지 않는다."""
        registry = load_rule_registry(REGISTRY)
        s3_rules = [rule for rule in registry.rules if S3 in rule.resource_types]

        self.assertTrue(s3_rules)
        for rule in s3_rules:
            with self.subTest(rule=rule.rule_id):
                self.assertIn(AssessmentPhase.POST_DEPLOY_VERIFICATION, rule.applicable_phases)


class ReadTimeSuppressionTests(unittest.TestCase):
    """ADR-0020 §6: 예외는 평가 게이트가 아니다. 억제는 조회 시점 표시로만 존재한다."""

    def test_an_in_force_exception_annotates_the_finding_for_display(self) -> None:
        notes = _annotate([_finding()])

        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].finding_id, "finding-001")
        self.assertEqual(notes[0].exception_id, "exception-001")
        self.assertIs(notes[0].reason, RemediationExceptionReason.ACCEPTED_RISK)
        self.assertEqual(notes[0].expires_at, "2026-12-31T00:00:00+00:00")

    def test_the_annotation_neither_mutates_nor_replaces_the_finding(self) -> None:
        """§6은 억제를 저장하지 못하게 한다. 반환값은 Finding 사본이 아니라 별도 주석이다."""
        finding = _finding()
        before = replace(finding)

        notes = _annotate([finding])

        self.assertEqual(finding, before)
        self.assertNotIsInstance(notes[0], Finding)
        self.assertFalse(hasattr(finding, "suppressed"))

    def test_an_expired_exception_is_not_displayed(self) -> None:
        """만료는 조회 시각 기준이다. 평가 시점에 유효했다는 사실이 만료를 되살리지 않는다."""
        notes = _annotate(
            [_finding()], exceptions=(_exception(expires_at="2026-09-01T10:00:00+00:00"),)
        )

        self.assertEqual(notes, ())

    def test_an_exception_approved_after_the_finding_does_not_suppress_it(self) -> None:
        """사후 승인이 옛 위반을 덮으면, 아무도 면제를 승인한 적 없는 시점의 위반이 가려진다."""
        notes = _annotate(
            [_finding()],
            exceptions=(
                _exception(
                    approved_at="2026-09-01T11:00:00+00:00",
                    expires_at="2026-12-31T00:00:00+00:00",
                ),
            ),
        )

        self.assertEqual(notes, ())

    def test_an_exception_for_another_rule_version_does_not_carry_over(self) -> None:
        notes = _annotate([_finding()], exceptions=(_exception(rule_version="2026-07-01"),))

        self.assertEqual(notes, ())

    def test_an_exception_for_another_customer_or_resource_does_not_apply(self) -> None:
        for override in ({"customer_id": "customer-002"}, {"resource_id": OTHER_RESOURCE}):
            with self.subTest(override=sorted(override)):
                notes = _annotate([_finding()], exceptions=(_exception(**override),))

                self.assertEqual(notes, ())

    def test_a_finding_without_provenance_is_never_suppressed(self) -> None:
        """평가 시각이 없으면 두 시각 규칙을 적용할 수 없다. 위반이 보이는 쪽으로 닫는다."""
        legacy = _finding(assessed_commit_sha=None, evaluated_at=None)

        notes = _annotate([legacy])

        self.assertEqual(notes, ())

    def test_a_resource_scoped_exception_wins_over_a_rule_wide_one(self) -> None:
        notes = _annotate(
            [_finding()],
            exceptions=(
                _exception(exception_id="exception-002"),
                _exception(exception_id="exception-001", resource_id=RESOURCE),
            ),
        )

        self.assertEqual([note.exception_id for note in notes], ["exception-001"])

    def test_equally_narrow_exceptions_resolve_by_exception_id(self) -> None:
        """입력 순서를 기준으로 삼으면 저장소 조회 순서에 따라 같은 사실의 기록이 달라진다."""
        notes = _annotate(
            [_finding()],
            exceptions=(
                _exception(exception_id="exception-009"),
                _exception(exception_id="exception-002"),
            ),
        )

        self.assertEqual([note.exception_id for note in notes], ["exception-002"])

    def test_only_suppressed_findings_are_returned_in_input_order(self) -> None:
        covered = _finding(finding_id="finding-001")
        uncovered = _finding(finding_id="finding-002", rule_version="2026-07-01")
        also_covered = _finding(finding_id="finding-003")

        notes = _annotate([covered, uncovered, also_covered])

        self.assertEqual([note.finding_id for note in notes], ["finding-001", "finding-003"])

    def test_no_exceptions_means_no_annotations(self) -> None:
        self.assertEqual(_annotate([_finding()], exceptions=()), ())

    def test_the_read_time_must_be_offset_aware(self) -> None:
        with self.assertRaisesRegex(ValueError, "at must be offset-aware"):
            _annotate([_finding()], at=datetime(2026, 9, 1, 12, 0))

    def test_display_and_verdict_agree_on_the_same_exception(self) -> None:
        """화면의 '억제됨'과 조치 판정의 `SUPPRESSED`가 갈리면 어느 쪽이 거짓인지 알 수 없다."""
        finding = _finding()
        exception = _exception()
        policy = RemediationPolicy(
            [
                RemediationRuleScope(
                    rule_id=RULE_ID,
                    version=RULE_VERSION,
                    eligibility=RemediationEligibility.AUTOMATIC,
                )
            ]
        )

        notes = _annotate([finding], exceptions=(exception,))
        decision = policy.decide(
            finding,
            customer_id=CUSTOMER,
            target=RemediationTarget(
                resource_id=RESOURCE,
                resource_type=S3,
                rule_id=RULE_ID,
                rule_version=RULE_VERSION,
                terraform_managed=True,
            ),
            commit_sha=COMMIT,
            finding_evaluated_at=EVALUATED_AT,
            at=READ_AT,
            exceptions=(exception,),
        )

        self.assertIs(decision.action, RemediationAction.SUPPRESSED)
        self.assertEqual([note.exception_id for note in notes], [decision.exception_id])


if __name__ == "__main__":
    unittest.main()
