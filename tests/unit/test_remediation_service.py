"""생성된 remediation은 요청한 Finding·IaC snapshot에 묶여야 하고, patch 판정에서만 만들어진다."""

import unittest

from apps.backend.remediation import RemediationContractError, RemediationService
from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    EvaluationPerspective,
    EvaluationStatus,
    Finding,
    IaCSnapshot,
    ManualReviewCode,
    RemediationAction,
    RemediationContext,
    RemediationDecision,
    RemediationPatch,
)


def snapshot() -> IaCSnapshot:
    return IaCSnapshot(
        customer_id="cust-001",
        repository_id="repo-001",
        commit_sha="abc123",
        artifact=ArtifactReference(
            artifact_id="art-snapshot",
            artifact_type=ArtifactType.TERRAFORM_SNAPSHOT,
            content_sha256="snapshot-digest",
            customer_id="cust-001",
            repository_id="repo-001",
        ),
    )


class Generator:
    """주입된 patch를 그대로 반환하는 테스트 stub. PatchGenerator port(context)를 만족한다."""

    def __init__(self, patch: RemediationPatch) -> None:
        self.patch = patch
        self.called = False

    def generate(self, *, context: RemediationContext) -> RemediationPatch:
        self.called = True
        return self.patch


def patch(*, commit: str = "abc123", finding: str = "finding-001") -> RemediationPatch:
    return RemediationPatch(
        finding_id=finding,
        base_commit_sha=commit,
        artifact=ArtifactReference(
            artifact_id="art-patch",
            artifact_type=ArtifactType.REMEDIATION_PATCH,
            content_sha256="patch-digest",
            customer_id="cust-001",
            repository_id="repo-001",
        ),
        changed_paths=("main.tf",),
    )


def context() -> RemediationContext:
    finding = Finding(
        finding_id="finding-001",
        resource_id="bucket-001",
        rule_id="rule-001",
        rule_version="v1",
        perspective=EvaluationPerspective.IAC,
        status=EvaluationStatus.FAIL,
        severity="HIGH",
        score=10,
        rationale="unsafe",
        evidence_references=("terraform:bucket-001",),
    )
    return RemediationContext(
        finding=finding,
        snapshot=snapshot(),
        evidence_references=finding.evidence_references,
    )


def decision(
    *, action: RemediationAction = RemediationAction.TERRAFORM_PATCH
) -> RemediationDecision:
    manual_code = (
        ManualReviewCode.RULE_NOT_IN_SCOPE
        if action is RemediationAction.MANUAL_REVIEW
        else None
    )
    exception_id = "exc-001" if action is RemediationAction.SUPPRESSED else None
    return RemediationDecision(
        finding_id="finding-001",
        resource_id="bucket-001",
        rule_id="rule-001",
        rule_version="v1",
        perspective=EvaluationPerspective.IAC,
        action=action,
        manual_review_code=manual_code,
        exception_id=exception_id,
    )


class RemediationServiceTest(unittest.TestCase):
    def test_returns_patch_bound_to_finding_and_snapshot(self) -> None:
        result = RemediationService(Generator(patch())).generate(
            context=context(), decision=decision()
        )
        self.assertEqual(result.base_commit_sha, "abc123")

    def test_rejects_patch_for_another_commit(self) -> None:
        with self.assertRaises(RemediationContractError):
            RemediationService(Generator(patch(commit="other"))).generate(
                context=context(), decision=decision()
            )

    # --- 정책 우회 불가 (ADR-0018) ---

    def test_rejects_manual_review_decision(self) -> None:
        gen = Generator(patch())
        with self.assertRaises(RemediationContractError):
            RemediationService(gen).generate(
                context=context(), decision=decision(action=RemediationAction.MANUAL_REVIEW)
            )
        # 게이트에서 막혀 generator까지 도달하지 않아야 한다.
        self.assertFalse(gen.called)

    def test_rejects_suppressed_decision(self) -> None:
        gen = Generator(patch())
        with self.assertRaises(RemediationContractError):
            RemediationService(gen).generate(
                context=context(), decision=decision(action=RemediationAction.SUPPRESSED)
            )
        self.assertFalse(gen.called)

    def test_rejects_actual_sync_decision(self) -> None:
        # ACTUAL_SYNC는 patch 합성 경로가 아니다(현재 commit을 배포 대상으로 삼는 별도 경로).
        gen = Generator(patch())
        with self.assertRaises(RemediationContractError):
            RemediationService(gen).generate(
                context=context(), decision=decision(action=RemediationAction.ACTUAL_SYNC)
            )
        self.assertFalse(gen.called)

    def test_rejects_decision_for_another_finding(self) -> None:
        # 판정과 context가 다른 finding을 가리키면 거부한다.
        other = RemediationDecision(
            finding_id="finding-999",
            resource_id="bucket-001",
            rule_id="rule-001",
            rule_version="v1",
            perspective=EvaluationPerspective.IAC,
            action=RemediationAction.TERRAFORM_PATCH,
        )
        gen = Generator(patch())
        with self.assertRaises(RemediationContractError):
            RemediationService(gen).generate(context=context(), decision=other)
        self.assertFalse(gen.called)


if __name__ == "__main__":
    unittest.main()
