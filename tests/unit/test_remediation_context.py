"""M2 C context and readiness behavior uses only immutable M1/D handoffs."""

import unittest

from apps.backend.remediation import build_remediation_context, evaluate_deployment_readiness
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
    RemediationStrategy,
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
        evidence_references=("aws:s3:bucket-001",),
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
        evidence_references=source.evidence_references,
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


def plan_input(*, destructive: bool = False, refreshed: bool = True) -> PlanReadinessInput:
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
    )


class RemediationContextTest(unittest.TestCase):
    def test_iac_failure_requires_a_patch(self) -> None:
        context = build_remediation_context(
            finding=finding(),
            snapshot=snapshot(),
            iac_result=result(perspective=EvaluationPerspective.IAC, status=EvaluationStatus.FAIL),
            actual_result=result(
                perspective=EvaluationPerspective.AWS_ACTUAL, status=EvaluationStatus.FAIL
            ),
        )
        self.assertIs(context.strategy, RemediationStrategy.PATCH_IAC)

    def test_safe_iac_and_unsafe_actual_syncs_current_commit(self) -> None:
        context = build_remediation_context(
            finding=finding(),
            snapshot=snapshot(),
            iac_result=result(perspective=EvaluationPerspective.IAC, status=EvaluationStatus.PASS),
            actual_result=result(
                perspective=EvaluationPerspective.AWS_ACTUAL, status=EvaluationStatus.FAIL
            ),
        )
        self.assertIs(context.strategy, RemediationStrategy.SYNC_CURRENT_IAC)
        readiness = evaluate_deployment_readiness(context=context, plan_input=plan_input())
        self.assertIs(readiness.status, DeploymentReadinessStatus.READY_FOR_APPROVAL)

    def test_destructive_plan_requires_manual_review(self) -> None:
        context = build_remediation_context(
            finding=finding(),
            snapshot=snapshot(),
            iac_result=result(perspective=EvaluationPerspective.IAC, status=EvaluationStatus.FAIL),
            actual_result=result(
                perspective=EvaluationPerspective.AWS_ACTUAL, status=EvaluationStatus.FAIL
            ),
        )
        readiness = evaluate_deployment_readiness(
            context=context, plan_input=plan_input(destructive=True)
        )
        self.assertIs(readiness.status, DeploymentReadinessStatus.MANUAL_REVIEW)

    def test_unrefreshed_plan_is_blocked(self) -> None:
        context = build_remediation_context(
            finding=finding(),
            snapshot=snapshot(),
            iac_result=result(perspective=EvaluationPerspective.IAC, status=EvaluationStatus.PASS),
            actual_result=result(
                perspective=EvaluationPerspective.AWS_ACTUAL, status=EvaluationStatus.FAIL
            ),
        )
        readiness = evaluate_deployment_readiness(
            context=context, plan_input=plan_input(refreshed=False)
        )
        self.assertIs(readiness.status, DeploymentReadinessStatus.BLOCKED)
