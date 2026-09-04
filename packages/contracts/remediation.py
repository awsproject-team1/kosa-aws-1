"""M2 remediation-context and deployment-readiness contracts.

These contracts consume immutable findings, policy decisions, and D-produced
artifacts. They never represent a customer-workload write or an Apply request.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from packages.contracts._validation import require_non_empty_string
from packages.contracts.assessments import Finding
from packages.contracts.deployments import IaCSnapshot, TerraformPlan, require_plan_evidence
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
    # The immutable Assessment that produced the Finding.  M3 deployment
    # creation must retain this identity so verification can be bound to the
    # exact before-state rather than attempting to infer it from a finding id.
    source_assessment_id: str | None = None

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
        if self.source_assessment_id is not None:
            require_non_empty_string(self.source_assessment_id, "source_assessment_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "finding": self.finding.to_dict(),
            "snapshot": self.snapshot.to_dict(),
            "evidence_references": list(self.evidence_references),
            "source_assessment_id": self.source_assessment_id,
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

    def to_dict(self) -> dict[str, object]:
        return {
            "finding_id": self.finding_id,
            "customer_id": self.customer_id,
            "repository_id": self.repository_id,
            "commit_sha": self.commit_sha,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanReadinessInput:
    """D's bounded plan summary consumed by C's readiness evaluator."""

    plan: TerraformPlan
    refreshed: bool
    has_destructive_changes: bool
    mapped_resource_ids: tuple[str, ...]
    #: `PlanSummary.plan_evidence`와 같은 모양. 비어 있으면 plan으로는 Finding 해소를 판정하지
    #: 않는다 — "해소됨"이 아니라 "판정 없음"이다.
    plan_evidence: Mapping[str, Mapping[str, tuple[object, ...]]] = field(default_factory=dict)

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
        require_plan_evidence(self.plan_evidence)


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
