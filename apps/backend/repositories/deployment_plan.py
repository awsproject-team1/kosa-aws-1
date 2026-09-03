"""A-owned reader assembling the approval input for one deployment (ADR-0019 §8).

승인 화면은 `commit_sha`와 `plan_hash`를 보내기 전에 그 값을 읽어야 하고, 승인 record는 C의
readiness 판정과 함께 기록된다. 이 reader가 그 두 값을 durable한 사실에서 만든다:

- `TerraformPlan`은 `DEPLOYMENT#{deployment_id}` item의 plan facts에서 복원한다.
- `DeploymentReadiness`는 저장하지 않고 read 시 파생한다(`DeploymentStatus`와 같은 원칙).
  파생 입력은 D가 plan과 함께 저장한 `PlanSummary`(`refreshed`/destructive/mapped resource ids)와
  C Worker가 쓴 `RemediationContext`이며, 판정은 C의 `evaluate_deployment_readiness()`가 한다.

readiness를 저장하지 않는 이유는 M1 Readiness Score와 같다 — 저장된 두 번째 사본은 입력이 바뀌면
조용히 낡는다. 여기서는 그 위험이 더 크다: 낡은 `READY_FOR_APPROVAL`은 C가 막았을 plan을 승인
가능한 것으로 보여준다.
"""

from collections.abc import Mapping

from apps.backend.remediation.readiness import evaluate_deployment_readiness
from apps.backend.repositories.deployment import DynamoDbDeploymentRepository
from apps.backend.repositories.dynamodb import DynamoTable
from apps.backend.repositories.ports import RepositoryError, StoredDataError
from apps.backend.repositories.remediation_work import remediation_context_from_item
from packages.contracts import (
    DeploymentReadinessSignal,
    PlanReadinessInput,
    TerraformPlan,
)
from packages.contracts.remediation import DeploymentReadiness


class DeploymentPlanNotReadyError(LookupError):
    """The deployment has no plan yet, so there is nothing to approve."""


class DynamoDbDeploymentPlanReader:
    """Return the stored plan and C's readiness verdict over it."""

    def __init__(self, table: DynamoTable, *, deployments: DynamoDbDeploymentRepository) -> None:
        if table is None:
            raise TypeError("table is required")
        if deployments is None:
            raise TypeError("deployments repository is required")
        self._table = table
        self._deployments = deployments

    def get_approval_input(
        self, *, customer_id: str, deployment_id: str
    ) -> tuple[TerraformPlan, DeploymentReadiness]:
        plan, readiness = self._plan_and_readiness(
            customer_id=customer_id, deployment_id=deployment_id
        )
        return plan, readiness

    def get_readiness_signal(
        self, *, customer_id: str, deployment_id: str
    ) -> DeploymentReadinessSignal | None:
        """Return the readiness signal for the status projection, or `None` before a plan.

        Status reads must never be the reason an approval is offered, so a deployment
        without plan facts returns `None` rather than a verdict over nothing.
        """
        try:
            _, readiness = self._plan_and_readiness(
                customer_id=customer_id, deployment_id=deployment_id
            )
        except DeploymentPlanNotReadyError:
            return None
        return DeploymentReadinessSignal(readiness.status.value)

    def _plan_and_readiness(
        self, *, customer_id: str, deployment_id: str
    ) -> tuple[TerraformPlan, DeploymentReadiness]:
        for value, name in ((customer_id, "customer_id"), (deployment_id, "deployment_id")):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        record = self._deployments.get_deployment(
            customer_id=customer_id, deployment_id=deployment_id
        )
        if record is None:
            raise DeploymentPlanNotReadyError("deployment not found")
        if record.plan_hash is None:
            # Plan facts are all-present-or-all-absent, so one missing value means the
            # RUN_DEPLOYMENT plan has not completed yet.
            raise DeploymentPlanNotReadyError("deployment has no plan yet")
        if record.plan_artifact is None or record.plan_summary is None:
            raise StoredDataError("stored deployment plan facts are incomplete")
        plan = TerraformPlan(
            deployment_id=record.deployment_id,
            commit_sha=record.commit_sha,
            plan_hash=record.plan_hash,
            artifact=record.plan_artifact,
        )
        context = remediation_context_from_item(
            self._remediation_context(customer_id, record.remediation_id)
        )
        try:
            readiness = evaluate_deployment_readiness(
                context=context,
                plan_input=PlanReadinessInput(
                    plan=plan,
                    refreshed=record.plan_summary.refreshed,
                    has_destructive_changes=record.plan_summary.has_destructive_changes,
                    mapped_resource_ids=record.plan_summary.mapped_resource_ids,
                ),
            )
        except (TypeError, ValueError) as error:
            raise StoredDataError("stored readiness inputs are invalid") from error
        return plan, readiness

    def _remediation_context(self, customer_id: str, remediation_id: str) -> Mapping[str, object]:
        try:
            item = self._table.get_item(
                Key={"PK": f"CUSTOMER#{customer_id}", "SK": f"REMEDIATION#{remediation_id}"},
                ConsistentRead=True,
            ).get("Item")
        except Exception:
            raise RepositoryError("remediation context read failed") from None
        if not isinstance(item, Mapping):
            raise StoredDataError("deployment remediation context not found")
        # The deployment names its remediation, but a stored item that belongs to another
        # customer must not become the basis of an approval decision.
        if item.get("customer_id") != customer_id or item.get("entity_type") != "REMEDIATION":
            raise StoredDataError("stored remediation is outside the customer scope")
        context = item.get("context")
        if not isinstance(context, Mapping):
            raise StoredDataError("stored remediation context is invalid")
        return context
