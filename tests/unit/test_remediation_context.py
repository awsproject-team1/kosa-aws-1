"""M2 C context and readiness use immutable evidence without choosing an action."""

import unittest

from apps.backend.remediation import (
    RemediationContextError,
    build_remediation_context,
    evaluate_deployment_readiness,
)
from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    DeploymentReadinessStatus,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    Finding,
    IaCSnapshot,
    PlanReadinessInput,
    TerraformPlan,
)


def result(*, perspective: EvaluationPerspective, status: EvaluationStatus) -> EvaluationResult:
    return EvaluationResult(
        resource_id="bucket-001",
        rule_id="rule-001",
        rule_version="1",
        perspective=perspective,
        status=status,
        severity="HIGH",
        score=0 if status is EvaluationStatus.FAIL else 100,
        rationale="fixed test rationale",
        evidence_references=(f"evidence:{perspective.value}",),
        rubric_version="1",
        model_profile_id="assessment-v1",
    )


def finding() -> Finding:
    source = result(perspective=EvaluationPerspective.DRIFT, status=EvaluationStatus.FAIL)
    return Finding(
        finding_id="finding-001",
        resource_id=source.resource_id,
        rule_id=source.rule_id,
        rule_version=source.rule_version,
        perspective=source.perspective,
        status=source.status,
        severity=source.severity,
        score=source.score,
        rationale=source.rationale,
        evidence_references=("evidence:finding",),
    )


def snapshot() -> IaCSnapshot:
    return IaCSnapshot(
        customer_id="cust-001",
        repository_id="repo-001",
        commit_sha="base-commit",
        artifact=ArtifactReference(
            artifact_id="snapshot-001",
            artifact_type=ArtifactType.TERRAFORM_SNAPSHOT,
            content_sha256="snapshot-hash",
            customer_id="cust-001",
            repository_id="repo-001",
        ),
    )


def context():
    return build_remediation_context(
        finding=finding(),
        snapshot=snapshot(),
        results=(
            result(perspective=EvaluationPerspective.IAC, status=EvaluationStatus.FAIL),
            result(perspective=EvaluationPerspective.AWS_ACTUAL, status=EvaluationStatus.FAIL),
        ),
    )


def plan_input(
    *,
    destructive: bool = False,
    refreshed: bool = True,
    plan_evidence: dict[str, dict[str, tuple[object, ...]]] | None = None,
) -> PlanReadinessInput:
    return PlanReadinessInput(
        plan=TerraformPlan(
            deployment_id="deployment-001",
            commit_sha="candidate-commit",
            plan_hash="plan-hash",
            artifact=ArtifactReference(
                artifact_id="plan-001",
                artifact_type=ArtifactType.TERRAFORM_PLAN,
                content_sha256="plan-hash",
                customer_id="cust-001",
                repository_id="repo-001",
            ),
        ),
        refreshed=refreshed,
        has_destructive_changes=destructive,
        mapped_resource_ids=("bucket-001",),
        plan_evidence={} if plan_evidence is None else plan_evidence,
    )


def _pab_evidence(**flags: bool) -> dict[str, dict[str, tuple[object, ...]]]:
    return {
        "bucket-001": {
            f"aws_s3_bucket_public_access_block:{name}": (value,) for name, value in flags.items()
        }
    }


def _s3_rule_control():
    from apps.backend.policy.control_catalog import MVP_CONTROL_CATALOG

    control = MVP_CONTROL_CATALOG.control("S3_BLOCK_PUBLIC_ACCESS")
    assert control is not None
    return control, control.baseline_required_evidence


class RemediationContextTest(unittest.TestCase):
    def test_context_preserves_combined_evidence_without_strategy(self) -> None:
        value = context()

        self.assertFalse(hasattr(value, "strategy"))
        self.assertEqual(
            value.evidence_references,
            ("evidence:finding", "evidence:IAC", "evidence:AWS_ACTUAL"),
        )
        self.assertNotIn("strategy", value.to_dict())

    def test_context_rejects_evaluation_for_another_rule(self) -> None:
        mismatched = result(perspective=EvaluationPerspective.IAC, status=EvaluationStatus.FAIL)
        object.__setattr__(mismatched, "rule_id", "other-rule")
        with self.assertRaisesRegex(RemediationContextError, "outside"):
            build_remediation_context(
                finding=finding(),
                snapshot=snapshot(),
                results=(
                    mismatched,
                    result(
                        perspective=EvaluationPerspective.AWS_ACTUAL,
                        status=EvaluationStatus.FAIL,
                    ),
                ),
            )

    def test_safe_refreshed_plan_is_ready_for_approval(self) -> None:
        readiness = evaluate_deployment_readiness(context=context(), plan_input=plan_input())
        self.assertIs(readiness.status, DeploymentReadinessStatus.READY_FOR_APPROVAL)

    def test_destructive_plan_requires_manual_review(self) -> None:
        readiness = evaluate_deployment_readiness(
            context=context(), plan_input=plan_input(destructive=True)
        )
        self.assertIs(readiness.status, DeploymentReadinessStatus.MANUAL_REVIEW)

    def test_unrefreshed_plan_is_blocked(self) -> None:
        readiness = evaluate_deployment_readiness(
            context=context(), plan_input=plan_input(refreshed=False)
        )
        self.assertIs(readiness.status, DeploymentReadinessStatus.BLOCKED)


class PlanResolvesFindingTest(unittest.TestCase):
    """The patch's plan is judged by the same predicate the Assessment used (ADR-0024 §E)."""

    def test_a_plan_that_still_violates_the_rule_blocks_approval(self) -> None:
        """네 플래그 중 셋만 true인 patch를 승인하면 위반을 배포한다."""
        readiness = evaluate_deployment_readiness(
            context=context(),
            plan_input=plan_input(
                plan_evidence=_pab_evidence(
                    block_public_acls=True,
                    ignore_public_acls=True,
                    block_public_policy=True,
                    restrict_public_buckets=False,
                )
            ),
            rule_control=_s3_rule_control(),
            resource_type="AWS::S3::Bucket",
        )
        self.assertIs(readiness.status, DeploymentReadinessStatus.BLOCKED)
        self.assertIn("FINDING_UNRESOLVED_IN_PLAN", readiness.reason_codes)

    def test_a_plan_that_satisfies_the_rule_says_so(self) -> None:
        readiness = evaluate_deployment_readiness(
            context=context(),
            plan_input=plan_input(
                plan_evidence=_pab_evidence(
                    block_public_acls=True,
                    ignore_public_acls=True,
                    block_public_policy=True,
                    restrict_public_buckets=True,
                )
            ),
            rule_control=_s3_rule_control(),
            resource_type="AWS::S3::Bucket",
        )
        self.assertIs(readiness.status, DeploymentReadinessStatus.READY_FOR_APPROVAL)
        self.assertEqual(
            readiness.reason_codes,
            ("REFRESHED_PLAN_BOUND_TO_REMEDIATION_CONTEXT", "FINDING_RESOLVED_IN_PLAN"),
        )

    def test_no_plan_evidence_means_no_claim_either_way(self) -> None:
        """ "판정 없음"은 "해소됨"이 아니다 — 어느 code도 남지 않는다."""
        readiness = evaluate_deployment_readiness(
            context=context(),
            plan_input=plan_input(),
            rule_control=_s3_rule_control(),
            resource_type="AWS::S3::Bucket",
        )
        self.assertIs(readiness.status, DeploymentReadinessStatus.READY_FOR_APPROVAL)
        self.assertEqual(readiness.reason_codes, ("REFRESHED_PLAN_BOUND_TO_REMEDIATION_CONTEXT",))

    def test_without_the_rule_control_the_plan_is_not_judged(self) -> None:
        readiness = evaluate_deployment_readiness(
            context=context(),
            plan_input=plan_input(plan_evidence=_pab_evidence(block_public_acls=False)),
        )
        self.assertIs(readiness.status, DeploymentReadinessStatus.READY_FOR_APPROVAL)
        self.assertNotIn("FINDING_UNRESOLVED_IN_PLAN", readiness.reason_codes)


class SinglePerspectiveContextTest(unittest.TestCase):
    """authoring이 만든 Rule은 `evaluation_type` 하나를 선언하므로 관점 하나만 평가된다.

    두 관점을 모두 요구하면 고객이 업로드한 정책에서 나온 Finding은 조치 경로에 들어가지도
    못한다 — 라이브에서 그 요구가 503으로 나타났다. 어느 관점이 있고 없는지로 조치 유형을 가르는
    것은 `RemediationPolicy.decide()`의 일이다.
    """

    @staticmethod
    def _finding(perspective: EvaluationPerspective) -> Finding:
        source = result(perspective=perspective, status=EvaluationStatus.FAIL)
        return Finding(
            finding_id="finding-001",
            resource_id=source.resource_id,
            rule_id=source.rule_id,
            rule_version=source.rule_version,
            perspective=perspective,
            status=source.status,
            severity=source.severity,
            score=source.score,
            rationale=source.rationale,
            evidence_references=("evidence:finding",),
        )

    def test_one_perspective_is_enough_when_it_is_the_findings_own(self) -> None:
        """`IAC` Rule에는 Actual 판정이 애초에 없다."""
        built = build_remediation_context(
            finding=self._finding(EvaluationPerspective.IAC),
            snapshot=snapshot(),
            results=(result(perspective=EvaluationPerspective.IAC, status=EvaluationStatus.FAIL),),
        )

        self.assertIn("evidence:finding", built.evidence_references)

    def test_results_without_the_findings_perspective_are_refused(self) -> None:
        """Finding이 나온 그 관점의 결과가 없으면 저장된 증거가 서로 어긋난 것이다."""
        with self.assertRaisesRegex(RemediationContextError, "finding's perspective"):
            build_remediation_context(
                finding=self._finding(EvaluationPerspective.IAC),
                snapshot=snapshot(),
                results=(
                    result(
                        perspective=EvaluationPerspective.AWS_ACTUAL,
                        status=EvaluationStatus.FAIL,
                    ),
                ),
            )

    def test_a_drift_finding_still_requires_both_perspectives(self) -> None:
        """DRIFT는 저장된 판정이 아니라 두 관점의 비교다. 하나만으로는 뒷받침되지 않는다."""
        with self.assertRaisesRegex(RemediationContextError, "both the IAC and AWS_ACTUAL"):
            build_remediation_context(
                finding=finding(),
                snapshot=snapshot(),
                results=(
                    result(perspective=EvaluationPerspective.IAC, status=EvaluationStatus.FAIL),
                ),
            )

    def test_no_results_at_all_are_refused(self) -> None:
        with self.assertRaises(RemediationContextError):
            build_remediation_context(finding=finding(), snapshot=snapshot(), results=())

    def test_a_repeated_perspective_is_refused(self) -> None:
        with self.assertRaisesRegex(RemediationContextError, "repeat a perspective"):
            build_remediation_context(
                finding=finding(),
                snapshot=snapshot(),
                results=(
                    result(perspective=EvaluationPerspective.IAC, status=EvaluationStatus.FAIL),
                    result(perspective=EvaluationPerspective.IAC, status=EvaluationStatus.FAIL),
                ),
            )


if __name__ == "__main__":
    unittest.main()
