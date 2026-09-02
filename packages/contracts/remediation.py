"""M2 remediation-context and deployment-readiness contracts.

These contracts consume immutable findings, policy decisions, and D-produced
artifacts. They never represent a customer-workload write or an Apply request.
"""

from dataclasses import dataclass
from enum import StrEnum

from packages.contracts._validation import require_non_empty_string
from packages.contracts.assessments import Finding
from packages.contracts.deployments import IaCSnapshot, TerraformPlan, TerraformStateVersion
from packages.contracts.jobs import JobResponse
from packages.contracts.remediation_policy import RemediationDecision


class DeploymentReadinessStatus(StrEnum):
    READY_FOR_APPROVAL = "READY_FOR_APPROVAL"
    BLOCKED = "BLOCKED"
    MANUAL_REVIEW = "MANUAL_REVIEW"


@dataclass(frozen=True, slots=True, kw_only=True)
class RemediationContext:
    """C's immutable, evidence-preserving remediation handoff.

    Action authorization deliberately is not present. Only B's stored
    ``RemediationDecision`` may select a remediation action.
    """

    finding: Finding
    snapshot: IaCSnapshot
    evidence_references: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.finding, Finding):
            raise TypeError("finding must be a Finding")
        if not isinstance(self.snapshot, IaCSnapshot):
            raise TypeError("snapshot must be an IaCSnapshot")
        if not isinstance(self.evidence_references, tuple):
            raise TypeError("evidence_references must be a tuple")
        if not self.evidence_references:
            raise ValueError("evidence_references must not be empty")
        for reference in self.evidence_references:
            require_non_empty_string(reference, "evidence_references item")

    def to_dict(self) -> dict[str, object]:
        return {
            "finding": self.finding.to_dict(),
            "snapshot": self.snapshot.to_dict(),
            "evidence_references": list(self.evidence_references),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RemediationStartResponse:
    """Public result of one policy-gated remediation request.

    Non-actionable decisions are normal 200 responses with no Job. Actionable
    decisions are accepted 202 responses and always carry a Job projection.
    """

    decision: RemediationDecision
    job: JobResponse | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision, RemediationDecision):
            raise TypeError("decision must be a RemediationDecision")
        if self.job is not None and not isinstance(self.job, JobResponse):
            raise TypeError("job must be a JobResponse or None")
        if self.decision.is_actionable != (self.job is not None):
            raise ValueError("only actionable decisions carry a Job")

    @property
    def accepted(self) -> bool:
        return self.job is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision.to_dict(),
            "job": None if self.job is None else self.job.to_dict(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RemediationSyncTarget:
    """Validated current IaC commit that D may use as a later Plan input.

    This is the return type of D's `SyncAction` port, so it lives here rather than
    inside C's worker module: a role boundary type in one role's app package forces
    the other role to import across it.
    """

    finding_id: str
    customer_id: str
    repository_id: str
    commit_sha: str

    def __post_init__(self) -> None:
        for name in ("finding_id", "customer_id", "repository_id", "commit_sha"):
            require_non_empty_string(getattr(self, name), name)


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanReadinessInput:
    """D's bounded plan summary consumed by C's readiness evaluator."""

    plan: TerraformPlan
    refreshed: bool
    has_destructive_changes: bool
    mapped_resource_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.plan, TerraformPlan):
            raise TypeError("plan must be a TerraformPlan")
        if not isinstance(self.refreshed, bool):
            raise TypeError("refreshed must be a bool")
        if not isinstance(self.has_destructive_changes, bool):
            raise TypeError("has_destructive_changes must be a bool")
        if not isinstance(self.mapped_resource_ids, tuple):
            raise TypeError("mapped_resource_ids must be a tuple")
        for resource_id in self.mapped_resource_ids:
            require_non_empty_string(resource_id, "mapped_resource_ids item")


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanRequestOutcome:
    """D `PlanRequestPort.request_plan`의 반환형 (ADR-0019 section 1, section 2).

    D Deployment Worker가 refreshed plan을 만든 뒤 durable하게 기록·전달할 값을 하나로 묶는다.
    - `plan`은 immutable `TerraformPlan` artifact 참조로, `plan_hash`가 canonical 투영 바이트의
      digest다(ADR-0019 section 1).
    - `state_version`은 plan 시점의 state `lineage`·`serial`이며, apply 직전 재검증의 근거다
      (ADR-0019 section 2). Deployment record에 기록된다.
    - `readiness_input`은 C의 readiness 평가가 소비하는 bounded plan summary다. `plan`을 공유하며
      `has_destructive_changes`는 D가 같은 투영 함수로 산출한다.

    이 반환형은 승인·정책 판정을 담지 않는다. 판정은 A(승인)와 B(정책), C(readiness)가 소유한다.
    """

    plan: TerraformPlan
    state_version: TerraformStateVersion
    readiness_input: "PlanReadinessInput"

    def __post_init__(self) -> None:
        if not isinstance(self.plan, TerraformPlan):
            raise TypeError("plan must be a TerraformPlan")
        if not isinstance(self.state_version, TerraformStateVersion):
            raise TypeError("state_version must be a TerraformStateVersion")
        if not isinstance(self.readiness_input, PlanReadinessInput):
            raise TypeError("readiness_input must be a PlanReadinessInput")
        if self.readiness_input.plan is not self.plan and self.readiness_input.plan != self.plan:
            # readiness가 다른 plan을 가리키면 승인 재검증과 값이 어긋난다.
            raise ValueError("readiness_input must describe the same plan")

    def to_dict(self) -> dict[str, object]:
        return {
            "plan": self.plan.to_dict(),
            "state_version": self.state_version.to_dict(),
            "readiness_input": {
                "plan": self.readiness_input.plan.to_dict(),
                "refreshed": self.readiness_input.refreshed,
                "has_destructive_changes": self.readiness_input.has_destructive_changes,
                "mapped_resource_ids": list(self.readiness_input.mapped_resource_ids),
            },
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class DeploymentReadiness:
    """C's deterministic, non-authorizing verdict over a refreshed plan."""

    deployment_id: str
    finding_id: str
    commit_sha: str
    plan_hash: str
    status: DeploymentReadinessStatus
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("deployment_id", "finding_id", "commit_sha", "plan_hash"):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.status, DeploymentReadinessStatus):
            raise TypeError("status must be a DeploymentReadinessStatus")
        if not isinstance(self.reason_codes, tuple) or not self.reason_codes:
            raise ValueError("reason_codes must be a non-empty tuple")
        for reason in self.reason_codes:
            require_non_empty_string(reason, "reason_codes item")

    def to_dict(self) -> dict[str, object]:
        return {
            "deployment_id": self.deployment_id,
            "finding_id": self.finding_id,
            "commit_sha": self.commit_sha,
            "plan_hash": self.plan_hash,
            "status": self.status.value,
            "reason_codes": list(self.reason_codes),
        }
