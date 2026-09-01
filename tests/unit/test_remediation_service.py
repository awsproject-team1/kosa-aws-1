"""Generated remediation must remain bound to a requested Finding and IaC snapshot."""

import unittest

from apps.backend.remediation import RemediationContractError, RemediationService
from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    EvaluationPerspective,
    EvaluationStatus,
    Finding,
    IaCSnapshot,
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
    def __init__(self, patch: RemediationPatch) -> None:
        self.patch = patch

    def generate(self, *, context: RemediationContext) -> RemediationPatch:
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


def decision() -> RemediationDecision:
    return RemediationDecision(
        finding_id="finding-001",
        resource_id="bucket-001",
        rule_id="rule-001",
        rule_version="v1",
        perspective=EvaluationPerspective.IAC,
        action=RemediationAction.TERRAFORM_PATCH,
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
