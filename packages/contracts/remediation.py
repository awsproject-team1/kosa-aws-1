"""M2 remediation-context and deployment-readiness contracts.

These contracts intentionally consume immutable M1 findings and D-produced
artifacts.  They never represent a customer-workload write or an Apply request.
"""

from dataclasses import dataclass
from enum import StrEnum

from packages.contracts._validation import require_non_empty_string
from packages.contracts.assessments import Finding
from packages.contracts.deployments import IaCSnapshot, TerraformPlan


class RemediationStrategy(StrEnum):
    PATCH_IAC = "PATCH_IAC"
    SYNC_CURRENT_IAC = "SYNC_CURRENT_IAC"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class DeploymentReadinessStatus(StrEnum):
    READY_FOR_APPROVAL = "READY_FOR_APPROVAL"
    BLOCKED = "BLOCKED"
    MANUAL_REVIEW = "MANUAL_REVIEW"


@dataclass(frozen=True, slots=True, kw_only=True)
class RemediationContext:
    """C's immutable, evidence-preserving handoff from a Finding to D."""

    finding: Finding
    snapshot: IaCSnapshot
    strategy: RemediationStrategy
    evidence_references: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.finding, Finding):
            raise TypeError("finding must be a Finding")
        if not isinstance(self.snapshot, IaCSnapshot):
            raise TypeError("snapshot must be an IaCSnapshot")
        if not isinstance(self.strategy, RemediationStrategy):
            raise TypeError("strategy must be a RemediationStrategy")
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
            "strategy": self.strategy.value,
            "evidence_references": list(self.evidence_references),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanReadinessInput:
    """D's bounded plan summary consumed by C's readiness evaluator.

    The raw plan remains an immutable artifact; this shape carries only the
    review-relevant, non-secret facts needed for an M2 readiness verdict.
    """

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
