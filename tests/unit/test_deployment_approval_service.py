"""M2 A approval gate tests."""

import unittest

from apps.backend.auth import AuthorizationDenied, Principal, Role
from apps.backend.deployment import DeploymentApprovalError, DeploymentApprovalService
from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    DeploymentReadiness,
    DeploymentReadinessStatus,
    TerraformPlan,
)


class Repository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, object]] = []

    def record_approval(self, *, customer_id, approval, readiness) -> None:
        self.calls.append((customer_id, approval, readiness))


def plan() -> TerraformPlan:
    return TerraformPlan(
        deployment_id="deployment-001",
        commit_sha="commit-001",
        plan_hash="plan-hash-001",
        artifact=ArtifactReference(
            artifact_id="plan-001",
            artifact_type=ArtifactType.TERRAFORM_PLAN,
            content_sha256="plan-hash-001",
            customer_id="cust-001",
            repository_id="repo-001",
        ),
    )


def readiness(*, status=DeploymentReadinessStatus.READY_FOR_APPROVAL) -> DeploymentReadiness:
    return DeploymentReadiness(
        deployment_id="deployment-001",
        finding_id="finding-001",
        commit_sha="commit-001",
        plan_hash="plan-hash-001",
        status=status,
        reason_codes=("REFRESHED_PLAN_BOUND_TO_REMEDIATION_CONTEXT",),
    )


def principal(*, role: Role) -> Principal:
    return Principal(
        subject="admin-001",
        client_id="client-001",
        customer_id="cust-001",
        roles=frozenset({role}),
    )


class DeploymentApprovalServiceTest(unittest.TestCase):
    def test_admin_approval_is_bound_and_audited(self) -> None:
        repository = Repository()
        approval = DeploymentApprovalService(repository).approve(
            principal=principal(role=Role.ADMIN), plan=plan(), readiness=readiness()
        )
        self.assertTrue(approval.matches(plan()))
        self.assertEqual(repository.calls[0][0], "cust-001")

    def test_user_cannot_approve(self) -> None:
        with self.assertRaises(AuthorizationDenied):
            DeploymentApprovalService(Repository()).approve(
                principal=principal(role=Role.USER), plan=plan(), readiness=readiness()
            )

    def test_blocked_readiness_never_writes_an_approval(self) -> None:
        repository = Repository()
        with self.assertRaises(DeploymentApprovalError):
            DeploymentApprovalService(repository).approve(
                principal=principal(role=Role.ADMIN),
                plan=plan(),
                readiness=readiness(status=DeploymentReadinessStatus.BLOCKED),
            )
        self.assertEqual(repository.calls, [])
