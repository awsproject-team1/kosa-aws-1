"""derive_deployment_status is a pure read-time projection (ADR-0019 §8, 불변식 #9)."""

import unittest

from packages.contracts import (
    ApplyOutcome,
    DeploymentFacts,
    DeploymentReadinessSignal,
    DeploymentStatus,
    JobCurrentStep,
    JobStatus,
    VerificationOutcome,
    derive_deployment_status,
)


def facts(**overrides: object) -> DeploymentFacts:
    base: dict[str, object] = {
        "job_status": JobStatus.RUNNING,
        "current_step": JobCurrentStep.TERRAFORM_PLAN,
    }
    base.update(overrides)
    return DeploymentFacts(**base)  # type: ignore[arg-type]


class DeriveDeploymentStatusTest(unittest.TestCase):
    def test_rejected_wins_over_every_other_fact(self) -> None:
        status = derive_deployment_status(
            facts(
                is_rejected=True,
                is_approved=False,
                apply_outcome=ApplyOutcome.RUNNING,
                readiness=DeploymentReadinessSignal.READY_FOR_APPROVAL,
            )
        )
        self.assertIs(status, DeploymentStatus.REJECTED)

    def test_plan_requested_before_readiness(self) -> None:
        self.assertIs(
            derive_deployment_status(facts(current_step=JobCurrentStep.TERRAFORM_PLAN)),
            DeploymentStatus.PLAN_REQUESTED,
        )

    def test_readiness_evaluated_step(self) -> None:
        self.assertIs(
            derive_deployment_status(facts(current_step=JobCurrentStep.PRE_DEPLOY_VALIDATION)),
            DeploymentStatus.READINESS_EVALUATED,
        )

    def test_readiness_ready_maps_to_waiting_approval(self) -> None:
        self.assertIs(
            derive_deployment_status(facts(readiness=DeploymentReadinessSignal.READY_FOR_APPROVAL)),
            DeploymentStatus.WAITING_APPROVAL,
        )

    def test_readiness_blocked_branch(self) -> None:
        self.assertIs(
            derive_deployment_status(facts(readiness=DeploymentReadinessSignal.BLOCKED)),
            DeploymentStatus.BLOCKED,
        )

    def test_readiness_manual_review_branch(self) -> None:
        self.assertIs(
            derive_deployment_status(facts(readiness=DeploymentReadinessSignal.MANUAL_REVIEW)),
            DeploymentStatus.MANUAL_REVIEW,
        )

    def test_approved_before_apply(self) -> None:
        self.assertIs(
            derive_deployment_status(
                facts(is_approved=True, readiness=DeploymentReadinessSignal.READY_FOR_APPROVAL)
            ),
            DeploymentStatus.APPROVED,
        )

    def test_applying_while_run_in_progress(self) -> None:
        self.assertIs(
            derive_deployment_status(facts(is_approved=True, apply_outcome=ApplyOutcome.RUNNING)),
            DeploymentStatus.APPLYING,
        )

    def test_apply_failure_routes_to_manual_review_not_retry(self) -> None:
        self.assertIs(
            derive_deployment_status(facts(is_approved=True, apply_outcome=ApplyOutcome.FAILED)),
            DeploymentStatus.MANUAL_REVIEW,
        )

    def test_applied_before_verification_starts(self) -> None:
        self.assertIs(
            derive_deployment_status(
                facts(
                    is_approved=True,
                    apply_outcome=ApplyOutcome.SUCCEEDED,
                    verification_outcome=VerificationOutcome.NOT_STARTED,
                )
            ),
            DeploymentStatus.APPLIED,
        )

    def test_verifying_while_verification_runs(self) -> None:
        self.assertIs(
            derive_deployment_status(
                facts(
                    is_approved=True,
                    apply_outcome=ApplyOutcome.SUCCEEDED,
                    verification_outcome=VerificationOutcome.RUNNING,
                )
            ),
            DeploymentStatus.VERIFYING,
        )

    def test_verified_when_comparison_is_comparable(self) -> None:
        self.assertIs(
            derive_deployment_status(
                facts(
                    is_approved=True,
                    apply_outcome=ApplyOutcome.SUCCEEDED,
                    verification_outcome=VerificationOutcome.COMPARABLE,
                )
            ),
            DeploymentStatus.VERIFIED,
        )

    def test_verification_indeterminate_is_not_a_violation(self) -> None:
        self.assertIs(
            derive_deployment_status(
                facts(
                    is_approved=True,
                    apply_outcome=ApplyOutcome.SUCCEEDED,
                    verification_outcome=VerificationOutcome.INDETERMINATE,
                )
            ),
            DeploymentStatus.VERIFICATION_INDETERMINATE,
        )

    def test_facts_reject_approved_and_rejected_together(self) -> None:
        with self.assertRaisesRegex(ValueError, "approved and rejected"):
            facts(is_approved=True, is_rejected=True)

    def test_facts_reject_wrong_types(self) -> None:
        with self.assertRaises(TypeError):
            DeploymentFacts(job_status="RUNNING", current_step=JobCurrentStep.APPLY)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
