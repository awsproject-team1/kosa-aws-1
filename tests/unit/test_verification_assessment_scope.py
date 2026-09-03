"""Verification Assessment creation reuses the source scope and fails closed otherwise."""

import unittest
from dataclasses import replace

from apps.backend.assessment import (
    VerificationAssessmentScope,
    VerificationRejectionCode,
    VerificationScopeError,
    VerificationSource,
    plan_verification_assessment,
)
from apps.backend.policy import PolicyContext
from packages.contracts import (
    AssessmentPhase,
    EvaluationPerspective,
    PlannedEvaluation,
    PolicyRule,
    RuleSeverity,
    SourceReference,
)

SOURCE_REFERENCE = SourceReference(
    source_id="isms-p",
    source_version="2023-10-31",
    locator="5.2.1",
    content_sha256="digest",
)


def _rule(rule_id: str) -> PolicyRule:
    return PolicyRule(
        rule_id=rule_id,
        version="2026-08-31",
        title=f"{rule_id} title",
        severity=RuleSeverity.HIGH,
        applicable_phases=(
            AssessmentPhase.INITIAL,
            AssessmentPhase.POST_DEPLOY_VERIFICATION,
        ),
        resource_types=("AWS::S3::Bucket",),
        source_references=(SOURCE_REFERENCE,),
    )


PLANNED = (
    PlannedEvaluation(
        resource_id="bucket-001", rule_id="S3-PUBLIC-001", perspective=EvaluationPerspective.IAC
    ),
    PlannedEvaluation(
        resource_id="bucket-001",
        rule_id="S3-PUBLIC-001",
        perspective=EvaluationPerspective.AWS_ACTUAL,
    ),
    PlannedEvaluation(
        resource_id="bucket-001", rule_id="S3-PUBLIC-001", perspective=EvaluationPerspective.DRIFT
    ),
)


def _source(**overrides: object) -> VerificationSource:
    values: dict[str, object] = {
        "assessment_id": "asm-001",
        "customer_id": "cust-001",
        "repository_id": "repo-001",
        "policy_profile_id": "profile-mvp-baseline",
        "policy_profile_version": "v2",
        "model_profile_id": "assessment-nova-lite-m1-v2",
        "rubric_version": "m1-v2",
        "phase": AssessmentPhase.INITIAL,
        "planned_coordinates": PLANNED,
    }
    values.update(overrides)
    return VerificationSource(**values)  # type: ignore[arg-type]


def _context(**overrides: object) -> PolicyContext:
    values: dict[str, object] = {
        "policy_profile_id": "profile-mvp-baseline",
        "policy_profile_version": "v2",
        "phase": AssessmentPhase.POST_DEPLOY_VERIFICATION,
        "resource_type": "AWS::S3::Bucket",
        "rules": (_rule("S3-PUBLIC-001"),),
    }
    values.update(overrides)
    return PolicyContext(**values)  # type: ignore[arg-type]


def _plan(**overrides: object) -> VerificationAssessmentScope:
    values: dict[str, object] = {
        "source": _source(),
        "context": _context(),
        "deployment_id": "dep-001",
        "assessment_id": "asm-002",
        "job_id": "job-002",
    }
    values.update(overrides)
    return plan_verification_assessment(**values)  # type: ignore[arg-type]


class VerificationScopeReuseTest(unittest.TestCase):
    def test_verification_assessment_reuses_the_source_selectors(self) -> None:
        scope = _plan()

        assessment = scope.assessment
        self.assertEqual(assessment.assessment_id, "asm-002")
        self.assertEqual(assessment.customer_id, "cust-001")
        self.assertEqual(assessment.job_id, "job-002")
        self.assertEqual(assessment.repository_id, "repo-001")
        self.assertEqual(assessment.policy_profile_id, "profile-mvp-baseline")
        self.assertIs(assessment.phase, AssessmentPhase.POST_DEPLOY_VERIFICATION)
        self.assertEqual(assessment.source_assessment_id, "asm-001")
        self.assertEqual(assessment.deployment_id, "dep-001")

    def test_verification_reuses_the_source_plan_profile_and_rubric(self) -> None:
        scope = _plan()

        self.assertEqual(scope.planned_coordinates, PLANNED)
        self.assertEqual(scope.assessment.policy_profile_version, "v2")
        self.assertEqual(scope.assessment.model_profile_id, "assessment-nova-lite-m1-v2")
        self.assertEqual(scope.assessment.rubric_version, "m1-v2")

    def test_a_verification_can_verify_an_earlier_verification(self) -> None:
        scope = _plan(
            source=_source(assessment_id="asm-002", phase=AssessmentPhase.POST_DEPLOY_VERIFICATION),
            assessment_id="asm-003",
        )

        self.assertEqual(scope.assessment.source_assessment_id, "asm-002")
        self.assertEqual(scope.planned_coordinates, PLANNED)

    def test_verification_cannot_reuse_the_source_assessment_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "must differ from source assessment"):
            _plan(assessment_id="asm-001")

    def test_replaced_policy_profile_version_is_not_a_verification(self) -> None:
        with self.assertRaises(VerificationScopeError) as caught:
            _plan(context=_context(policy_profile_version="v3"))

        self.assertIs(
            caught.exception.code, VerificationRejectionCode.POLICY_PROFILE_VERSION_REPLACED
        )

    def test_a_different_policy_profile_is_rejected_separately(self) -> None:
        with self.assertRaises(VerificationScopeError) as caught:
            _plan(context=_context(policy_profile_id="profile-other"))

        self.assertIs(caught.exception.code, VerificationRejectionCode.POLICY_PROFILE_MISMATCH)

    def test_context_must_be_resolved_for_the_verification_phase(self) -> None:
        for phase in (AssessmentPhase.INITIAL, AssessmentPhase.DEPLOYMENT_READINESS):
            with self.subTest(phase=phase):
                with self.assertRaises(VerificationScopeError) as caught:
                    _plan(context=_context(phase=phase))

                self.assertIs(
                    caught.exception.code, VerificationRejectionCode.CONTEXT_PHASE_MISMATCH
                )

    def test_planned_rule_outside_the_verification_allow_list_fails_closed(self) -> None:
        with self.assertRaises(VerificationScopeError) as caught:
            _plan(context=_context(rules=(_rule("S3-LOGGING-001"),)))

        self.assertIs(caught.exception.code, VerificationRejectionCode.PLANNED_RULE_NOT_APPLICABLE)

    def test_a_wider_allow_list_does_not_widen_the_reused_plan(self) -> None:
        scope = _plan(context=_context(rules=(_rule("S3-PUBLIC-001"), _rule("S3-LOGGING-001"))))

        self.assertEqual(scope.planned_coordinates, PLANNED)

    def test_identifiers_must_be_non_empty(self) -> None:
        for field_name in ("deployment_id", "assessment_id", "job_id"):
            with self.subTest(field=field_name):
                with self.assertRaisesRegex(ValueError, f"{field_name} must be a non-empty"):
                    _plan(**{field_name: "  "})

    def test_inputs_must_be_the_declared_types(self) -> None:
        with self.assertRaisesRegex(TypeError, "source must be a VerificationSource"):
            _plan(source=object())
        with self.assertRaisesRegex(TypeError, "context must be a PolicyContext"):
            _plan(context=object())


class VerificationSourceValidationTest(unittest.TestCase):
    def test_every_pinned_selector_is_required(self) -> None:
        for field_name in (
            "assessment_id",
            "customer_id",
            "repository_id",
            "policy_profile_id",
            "policy_profile_version",
            "model_profile_id",
            "rubric_version",
        ):
            with self.subTest(field=field_name):
                with self.assertRaisesRegex(ValueError, f"{field_name} must be a non-empty"):
                    _source(**{field_name: ""})

    def test_phase_must_be_a_contract_enum_member(self) -> None:
        with self.assertRaisesRegex(TypeError, "phase must be an AssessmentPhase"):
            _source(phase="INITIAL")

    def test_planned_coordinates_must_be_a_unique_non_empty_set(self) -> None:
        with self.assertRaisesRegex(ValueError, "planned_coordinates must be a non-empty tuple"):
            _source(planned_coordinates=())
        with self.assertRaisesRegex(ValueError, "must not contain duplicates"):
            _source(planned_coordinates=(PLANNED[0], PLANNED[0]))
        with self.assertRaisesRegex(TypeError, "must contain PlannedEvaluation values"):
            _source(planned_coordinates=("S3-PUBLIC-001",))


class VerificationScopeValueTest(unittest.TestCase):
    def test_scope_rejects_a_non_verification_assessment(self) -> None:
        scope = _plan()
        initial = replace(
            scope.assessment,
            phase=AssessmentPhase.INITIAL,
            source_assessment_id=None,
            deployment_id=None,
            model_profile_id=None,
            rubric_version=None,
        )

        with self.assertRaisesRegex(ValueError, "requires a POST_DEPLOY_VERIFICATION assessment"):
            replace(scope, assessment=initial)

    def test_rejection_code_must_be_a_rejection_code(self) -> None:
        with self.assertRaisesRegex(TypeError, "code must be a VerificationRejectionCode"):
            raise VerificationScopeError("boom", code="POLICY_PROFILE_MISMATCH")


if __name__ == "__main__":
    unittest.main()
