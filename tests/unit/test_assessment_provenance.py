"""Verification Assessment phase and correlation are persisted and restored fail-closed."""

import json
import unittest

from apps.backend.assessment import Assessment
from apps.backend.assessment.runtime import DynamoFixtureWorkRepository, DynamoM1WorkRepository
from apps.backend.assessment.runtime_config import M1RuntimeConfiguration
from packages.contracts import AssessmentPhase

TARGET = {
    "customer_id": "cust-001",
    "repository_id": "repo-001",
    "policy_profile_id": "profile-mvp-baseline",
    "commit_sha": "a" * 40,
    "github_repository": "customer/iac",
    "github_token_secret_id": "github-token",
    "aws_account_id": "123456789012",
    "aws_read_role_arn": "arn:aws:iam::123456789012:role/Read",
    "aws_external_id_secret_id": "external-id",
    "s3_bucket_id": "customer-test-bucket",
}

SNAPSHOT = {
    "resource_id": "customer-test-bucket",
    "resource_type": "AWS::S3::Bucket",
    "perspective": "AWS_ACTUAL",
}

# ADR-0020 §3: a verification pins the scope it reuses, so these travel with the
# correlation on every POST_DEPLOY_VERIFICATION Assessment.
PINS = {
    "model_profile_id": "assessment-nova-lite-m1-v2",
    "rubric_version": "m1-v2",
    "policy_profile_version": "v2",
}


class StubTable:
    """Return one Job row and the stored Assessment item under test."""

    def __init__(self, assessment: dict[str, object]) -> None:
        self._assessment = assessment

    def query(self, **kwargs: object) -> dict[str, object]:
        return {
            "Items": [
                {"customer_id": "cust-001", "assessment_id": "asm-002", "revision": 0},
            ]
        }

    def get_item(self, **kwargs: object) -> dict[str, object]:
        return {"Item": self._assessment}


def _stored(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "repository_id": "repo-001",
        "policy_profile_id": "profile-mvp-baseline",
    }
    item.update(overrides)
    return item


def _m1_repository(assessment: dict[str, object]) -> DynamoM1WorkRepository:
    return DynamoM1WorkRepository(
        StubTable(assessment),
        M1RuntimeConfiguration.from_json(json.dumps([TARGET])),
        model_profile_id="assessment-nova-lite-m1-v2",
    )


class AssessmentProvenanceTest(unittest.TestCase):
    def test_initial_assessment_defaults_to_initial_without_correlation(self) -> None:
        assessment = Assessment(
            assessment_id="asm-001",
            customer_id="cust-001",
            job_id="job-001",
            repository_id="repo-001",
            policy_profile_id="profile-001",
        )

        self.assertIs(assessment.phase, AssessmentPhase.INITIAL)
        self.assertIsNone(assessment.source_assessment_id)
        self.assertIsNone(assessment.deployment_id)
        self.assertIsNone(assessment.model_profile_id)
        self.assertIsNone(assessment.rubric_version)
        self.assertIsNone(assessment.policy_profile_version)

    def test_verification_assessment_carries_both_correlation_values(self) -> None:
        assessment = Assessment(
            assessment_id="asm-002",
            customer_id="cust-001",
            job_id="job-002",
            repository_id="repo-001",
            policy_profile_id="profile-001",
            phase=AssessmentPhase.POST_DEPLOY_VERIFICATION,
            source_assessment_id="asm-001",
            deployment_id="dep-001",
            **PINS,
        )

        self.assertEqual(assessment.source_assessment_id, "asm-001")
        self.assertEqual(assessment.deployment_id, "dep-001")
        self.assertEqual(assessment.model_profile_id, "assessment-nova-lite-m1-v2")
        self.assertEqual(assessment.rubric_version, "m1-v2")
        self.assertEqual(assessment.policy_profile_version, "v2")

    def test_verification_requires_a_complete_correlation(self) -> None:
        for correlation in ({"source_assessment_id": "asm-001"}, {"deployment_id": "dep-001"}, {}):
            with self.subTest(correlation=sorted(correlation)):
                with self.assertRaisesRegex(
                    ValueError, "requires source_assessment_id and deployment_id"
                ):
                    Assessment(
                        assessment_id="asm-002",
                        customer_id="cust-001",
                        job_id="job-002",
                        repository_id="repo-001",
                        policy_profile_id="profile-001",
                        phase=AssessmentPhase.POST_DEPLOY_VERIFICATION,
                        **PINS,
                        **correlation,
                    )

    def test_verification_requires_the_reused_scope_pin(self) -> None:
        for omitted in PINS:
            with self.subTest(omitted=omitted):
                pins = {name: value for name, value in PINS.items() if name != omitted}
                with self.assertRaisesRegex(
                    ValueError, "POST_DEPLOY_VERIFICATION requires the source"
                ):
                    Assessment(
                        assessment_id="asm-002",
                        customer_id="cust-001",
                        job_id="job-002",
                        repository_id="repo-001",
                        policy_profile_id="profile-001",
                        phase=AssessmentPhase.POST_DEPLOY_VERIFICATION,
                        source_assessment_id="asm-001",
                        deployment_id="dep-001",
                        **pins,
                    )

    def test_other_phases_cannot_carry_the_reused_scope_pin(self) -> None:
        for phase in (AssessmentPhase.INITIAL, AssessmentPhase.DEPLOYMENT_READINESS):
            for pinned in PINS:
                with self.subTest(phase=phase, pinned=pinned):
                    with self.assertRaisesRegex(
                        ValueError, "only valid for post-deploy verification"
                    ):
                        Assessment(
                            assessment_id="asm-002",
                            customer_id="cust-001",
                            job_id="job-002",
                            repository_id="repo-001",
                            policy_profile_id="profile-001",
                            phase=phase,
                            **{pinned: PINS[pinned]},
                        )

    def test_verification_cannot_reference_itself(self) -> None:
        with self.assertRaisesRegex(ValueError, "must differ from source assessment"):
            Assessment(
                assessment_id="asm-002",
                customer_id="cust-001",
                job_id="job-002",
                repository_id="repo-001",
                policy_profile_id="profile-001",
                phase=AssessmentPhase.POST_DEPLOY_VERIFICATION,
                source_assessment_id="asm-002",
                deployment_id="dep-001",
                **PINS,
            )

    def test_other_phases_cannot_carry_verification_correlation(self) -> None:
        for phase in (AssessmentPhase.INITIAL, AssessmentPhase.DEPLOYMENT_READINESS):
            with self.subTest(phase=phase):
                with self.assertRaisesRegex(ValueError, "only valid for post-deploy verification"):
                    Assessment(
                        assessment_id="asm-002",
                        customer_id="cust-001",
                        job_id="job-002",
                        repository_id="repo-001",
                        policy_profile_id="profile-001",
                        phase=phase,
                        source_assessment_id="asm-001",
                        deployment_id="dep-001",
                    )

    def test_phase_must_be_a_contract_enum_member(self) -> None:
        with self.assertRaisesRegex(TypeError, "phase must be an AssessmentPhase"):
            Assessment(
                assessment_id="asm-001",
                customer_id="cust-001",
                job_id="job-001",
                repository_id="repo-001",
                policy_profile_id="profile-001",
                phase="INITIAL",
            )

    def test_blank_correlation_values_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "source_assessment_id must be a non-empty string"):
            Assessment(
                assessment_id="asm-002",
                customer_id="cust-001",
                job_id="job-002",
                repository_id="repo-001",
                policy_profile_id="profile-001",
                phase=AssessmentPhase.POST_DEPLOY_VERIFICATION,
                source_assessment_id="  ",
                deployment_id="dep-001",
                **PINS,
            )

    def test_blank_pinned_scope_values_are_rejected(self) -> None:
        for pinned in PINS:
            with self.subTest(pinned=pinned):
                pins = {**PINS, pinned: "   "}
                with self.assertRaisesRegex(ValueError, f"{pinned} must be a non-empty string"):
                    Assessment(
                        assessment_id="asm-002",
                        customer_id="cust-001",
                        job_id="job-002",
                        repository_id="repo-001",
                        policy_profile_id="profile-001",
                        phase=AssessmentPhase.POST_DEPLOY_VERIFICATION,
                        source_assessment_id="asm-001",
                        deployment_id="dep-001",
                        **pins,
                    )


class StoredAssessmentPhaseTest(unittest.TestCase):
    def test_worker_restores_the_persisted_verification_phase(self) -> None:
        repository = _m1_repository(
            _stored(
                phase="POST_DEPLOY_VERIFICATION",
                source_assessment_id="asm-001",
                deployment_id="dep-001",
            )
        )

        work = repository.get_resource_work(job_id="job-002", expected_revision=0)

        assert work is not None
        self.assertIs(work.phase, AssessmentPhase.POST_DEPLOY_VERIFICATION)

    def test_fixture_worker_restores_the_persisted_verification_phase(self) -> None:
        repository = DynamoFixtureWorkRepository(
            StubTable(
                _stored(
                    phase="POST_DEPLOY_VERIFICATION",
                    source_assessment_id="asm-001",
                    deployment_id="dep-001",
                )
            ),
            SNAPSHOT,
        )

        work = repository.get_resource_work(job_id="job-002", expected_revision=0)

        assert work is not None
        self.assertIs(work.phase, AssessmentPhase.POST_DEPLOY_VERIFICATION)

    def test_legacy_record_without_a_phase_reads_as_initial(self) -> None:
        repository = _m1_repository(_stored())

        work = repository.get_resource_work(job_id="job-002", expected_revision=0)

        assert work is not None
        self.assertIs(work.phase, AssessmentPhase.INITIAL)

    def test_legacy_record_with_correlation_fails_closed(self) -> None:
        repository = _m1_repository(_stored(source_assessment_id="asm-001"))

        with self.assertRaisesRegex(ValueError, "legacy Assessment cannot contain"):
            repository.get_resource_work(job_id="job-002", expected_revision=0)

    def test_explicit_null_or_unknown_phase_fails_closed(self) -> None:
        for phase in (None, "", "INITIAL_PHASE", 1):
            with self.subTest(phase=phase):
                repository = _m1_repository(_stored(phase=phase))
                with self.assertRaisesRegex(ValueError, "stored Assessment phase is invalid"):
                    repository.get_resource_work(job_id="job-002", expected_revision=0)

    def test_partial_stored_verification_correlation_fails_closed(self) -> None:
        for correlation in (
            {"source_assessment_id": "asm-001"},
            {"deployment_id": "dep-001"},
            {},
        ):
            with self.subTest(correlation=sorted(correlation)):
                repository = _m1_repository(
                    _stored(phase="POST_DEPLOY_VERIFICATION", **correlation)
                )
                with self.assertRaisesRegex(ValueError, "correlation is incomplete"):
                    repository.get_resource_work(job_id="job-002", expected_revision=0)

    def test_stored_self_reference_fails_closed(self) -> None:
        repository = _m1_repository(
            _stored(
                phase="POST_DEPLOY_VERIFICATION",
                source_assessment_id="asm-002",
                deployment_id="dep-001",
            )
        )

        with self.assertRaisesRegex(ValueError, "cannot reference itself"):
            repository.get_resource_work(job_id="job-002", expected_revision=0)

    def test_stored_non_verification_phase_with_correlation_fails_closed(self) -> None:
        repository = _m1_repository(
            _stored(phase="INITIAL", source_assessment_id="asm-001", deployment_id="dep-001")
        )

        with self.assertRaisesRegex(ValueError, "has verification correlation"):
            repository.get_resource_work(job_id="job-002", expected_revision=0)

    def test_blank_stored_correlation_value_fails_closed(self) -> None:
        repository = _m1_repository(
            _stored(
                phase="POST_DEPLOY_VERIFICATION",
                source_assessment_id="   ",
                deployment_id="dep-001",
            )
        )

        with self.assertRaisesRegex(ValueError, "source_assessment_id is invalid"):
            repository.get_resource_work(job_id="job-002", expected_revision=0)


if __name__ == "__main__":
    unittest.main()
