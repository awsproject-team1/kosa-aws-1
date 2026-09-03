"""GitHub, read-only AWS, and approved Terraform deployment contracts."""

from dataclasses import dataclass
from enum import StrEnum

from packages.contracts._validation import require_non_empty_string
from packages.contracts.jobs import JobCurrentStep, JobStatus


class ArtifactType(StrEnum):
    POLICY_ORIGINAL = "POLICY_ORIGINAL"
    TERRAFORM_SNAPSHOT = "TERRAFORM_SNAPSHOT"
    AWS_SNAPSHOT = "AWS_SNAPSHOT"
    REMEDIATION_PATCH = "REMEDIATION_PATCH"
    # The `show -json` allow-list projection whose SHA-256 is `plan_hash` (ADR-0019 §1).
    TERRAFORM_PLAN = "TERRAFORM_PLAN"
    # The saved binary plan `terraform apply` consumes; never a hash target because
    # Terraform does not guarantee its byte stability (ADR-0019 §1).
    TERRAFORM_PLAN_BINARY = "TERRAFORM_PLAN_BINARY"
    GOLDEN_DATASET = "GOLDEN_DATASET"


class AwsResourceOperation(StrEnum):
    READ_RESOURCE = "READ_RESOURCE"
    LIST_RESOURCES = "LIST_RESOURCES"


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactReference:
    artifact_id: str
    artifact_type: ArtifactType
    content_sha256: str
    customer_id: str
    repository_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("artifact_id", "content_sha256", "customer_id"):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.artifact_type, ArtifactType):
            raise TypeError("artifact_type must be an ArtifactType")
        if self.repository_id is not None:
            require_non_empty_string(self.repository_id, "repository_id")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type.value,
            "content_sha256": self.content_sha256,
            "customer_id": self.customer_id,
            "repository_id": self.repository_id,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class IaCSnapshot:
    customer_id: str
    repository_id: str
    commit_sha: str
    artifact: ArtifactReference

    def __post_init__(self) -> None:
        for name in ("customer_id", "repository_id", "commit_sha"):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.artifact, ArtifactReference):
            raise TypeError("artifact must be an ArtifactReference")
        if self.artifact.artifact_type is not ArtifactType.TERRAFORM_SNAPSHOT:
            raise ValueError("artifact must be a TERRAFORM_SNAPSHOT")
        if self.artifact.customer_id != self.customer_id:
            raise ValueError("artifact customer_id must match snapshot customer_id")
        if self.artifact.repository_id != self.repository_id:
            raise ValueError("artifact repository_id must match snapshot repository_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "customer_id": self.customer_id,
            "repository_id": self.repository_id,
            "commit_sha": self.commit_sha,
            "artifact": self.artifact.to_dict(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RemediationPatch:
    finding_id: str
    base_commit_sha: str
    artifact: ArtifactReference
    changed_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("finding_id", "base_commit_sha"):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.artifact, ArtifactReference):
            raise TypeError("artifact must be an ArtifactReference")
        if self.artifact.artifact_type is not ArtifactType.REMEDIATION_PATCH:
            raise ValueError("artifact must be a REMEDIATION_PATCH")
        if not self.changed_paths:
            raise ValueError("changed_paths must not be empty")
        for path in self.changed_paths:
            require_non_empty_string(path, "changed_paths item")
            if path.startswith("/") or ".." in path.split("/"):
                raise ValueError("changed_paths items must be repository-relative paths")

    def to_dict(self) -> dict[str, object]:
        return {
            "finding_id": self.finding_id,
            "base_commit_sha": self.base_commit_sha,
            "artifact": self.artifact.to_dict(),
            "changed_paths": list(self.changed_paths),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class AwsResourceQuery:
    customer_id: str
    aws_account_id: str
    operation: AwsResourceOperation
    resource_type: str
    resource_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("customer_id", "aws_account_id", "resource_type"):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.operation, AwsResourceOperation):
            raise TypeError("operation must be an AwsResourceOperation")
        if self.resource_id is not None:
            require_non_empty_string(self.resource_id, "resource_id")
        if self.operation is AwsResourceOperation.READ_RESOURCE and self.resource_id is None:
            raise ValueError("READ_RESOURCE requires resource_id")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "customer_id": self.customer_id,
            "aws_account_id": self.aws_account_id,
            "operation": self.operation.value,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class TerraformStateVersion:
    """The plan-time Terraform state identity re-checked before apply (ADR-0019 §2).

    `serial` alone is insufficient: a re-created state gets a fresh `lineage` and a
    `serial` reset to a low value, so a different state can coincidentally match a
    `serial`. Both values are compared as a pair so that case is caught.
    """

    lineage: str
    serial: int

    def __post_init__(self) -> None:
        require_non_empty_string(self.lineage, "lineage")
        if isinstance(self.serial, bool) or not isinstance(self.serial, int):
            raise TypeError("serial must be an integer")
        if self.serial < 0:
            raise ValueError("serial must be zero or greater")

    def matches(self, other: object) -> bool:
        """Return whether two state versions are the same lineage and serial pair."""
        if not isinstance(other, TerraformStateVersion):
            raise TypeError("other must be a TerraformStateVersion")
        return self.lineage == other.lineage and self.serial == other.serial

    def to_dict(self) -> dict[str, object]:
        return {"lineage": self.lineage, "serial": self.serial}


@dataclass(frozen=True, slots=True, kw_only=True)
class TerraformPlan:
    deployment_id: str
    commit_sha: str
    plan_hash: str
    artifact: ArtifactReference

    def __post_init__(self) -> None:
        for name in ("deployment_id", "commit_sha", "plan_hash"):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.artifact, ArtifactReference):
            raise TypeError("artifact must be an ArtifactReference")
        if self.artifact.artifact_type is not ArtifactType.TERRAFORM_PLAN:
            raise ValueError("artifact must be a TERRAFORM_PLAN")
        if self.plan_hash != self.artifact.content_sha256:
            raise ValueError("plan_hash must match the Terraform plan artifact digest")

    def to_dict(self) -> dict[str, object]:
        return {
            "deployment_id": self.deployment_id,
            "commit_sha": self.commit_sha,
            "plan_hash": self.plan_hash,
            "artifact": self.artifact.to_dict(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkflowRunReference:
    """Locator for one GitHub Actions run tied to a deployment (ADR-0019 §7)."""

    deployment_id: str
    repository_id: str
    run_id: str

    def __post_init__(self) -> None:
        for name in ("deployment_id", "repository_id", "run_id"):
            require_non_empty_string(getattr(self, name), name)

    def to_dict(self) -> dict[str, str]:
        return {
            "deployment_id": self.deployment_id,
            "repository_id": self.repository_id,
            "run_id": self.run_id,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanSummary:
    """D's bounded description of what a refreshed plan does (ADR-0019 §1 addendum).

    C's readiness evaluator needs three facts about a plan that its `plan_hash` does not
    carry: whether it was refreshed, whether it destroys or replaces anything, and which
    AWS resources it touches. All three are properties of the plan D produced, so D is
    the only role that can state them — and they have to be durable, because approval
    happens in a later invocation than plan.

    `has_destructive_changes` and `mapped_resource_ids` are derived from the same
    canonical projection whose digest is `plan_hash`, using the shared functions in
    `packages.contracts.terraform_plan`. Deriving them anywhere else would let the
    approval gate read a different view of the plan than the one that was hashed.
    """

    refreshed: bool
    has_destructive_changes: bool
    mapped_resource_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("refreshed", "has_destructive_changes"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a bool")
        if not isinstance(self.mapped_resource_ids, tuple):
            raise TypeError("mapped_resource_ids must be a tuple")
        for resource_id in self.mapped_resource_ids:
            require_non_empty_string(resource_id, "mapped_resource_ids item")
        if len(set(self.mapped_resource_ids)) != len(self.mapped_resource_ids):
            raise ValueError("mapped_resource_ids must not contain duplicates")

    def to_dict(self) -> dict[str, object]:
        return {
            "refreshed": self.refreshed,
            "has_destructive_changes": self.has_destructive_changes,
            "mapped_resource_ids": list(self.mapped_resource_ids),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanExecutionResult:
    """D's `PlanRequestPort` output: the hashed plan, its saved binary, and state.

    `plan` carries the allow-listed projection whose digest is `plan_hash`.
    `binary_artifact` is the saved binary plan that `terraform apply` consumes and
    is never a hash target. `state_version` is the plan-time `(lineage, serial)`
    re-checked before apply. `plan_run` names the Actions run that produced the
    binary. All four refer to the same deployment (ADR-0019 §1, §2).

    `plan_run` exists because apply consumes a saved plan from a **different**
    run (§1): the apply workflow needs that run's id to download the artifact, and
    apply happens in a later invocation than plan, so the id has to survive in the
    durable plan result rather than in the dispatching process. Without it the
    apply workflow's required `plan_run_id` input has no source and the dispatch
    is rejected before apply starts.
    """

    plan: TerraformPlan
    binary_artifact: ArtifactReference
    state_version: TerraformStateVersion
    plan_run: WorkflowRunReference
    summary: PlanSummary

    def __post_init__(self) -> None:
        if not isinstance(self.plan, TerraformPlan):
            raise TypeError("plan must be a TerraformPlan")
        if not isinstance(self.summary, PlanSummary):
            raise TypeError("summary must be a PlanSummary")
        if not isinstance(self.binary_artifact, ArtifactReference):
            raise TypeError("binary_artifact must be an ArtifactReference")
        if self.binary_artifact.artifact_type is not ArtifactType.TERRAFORM_PLAN_BINARY:
            raise ValueError("binary_artifact must be a TERRAFORM_PLAN_BINARY")
        # The binary plan and its hashed projection must describe the same
        # deployment. Without this, a result could bundle one customer/repo's plan
        # with another's binary and still apply against the wrong account scope.
        if self.binary_artifact.customer_id != self.plan.artifact.customer_id:
            raise ValueError("binary_artifact customer_id must match the plan artifact")
        if self.binary_artifact.repository_id != self.plan.artifact.repository_id:
            raise ValueError("binary_artifact repository_id must match the plan artifact")
        if not isinstance(self.state_version, TerraformStateVersion):
            raise TypeError("state_version must be a TerraformStateVersion")
        if not isinstance(self.plan_run, WorkflowRunReference):
            raise TypeError("plan_run must be a WorkflowRunReference")
        # The run that produced the binary must be the run of *this* deployment.
        # An unchecked id would let apply download a plan artifact belonging to a
        # different deployment while every other approved value still matched.
        if self.plan_run.deployment_id != self.plan.deployment_id:
            raise ValueError("plan_run deployment_id must match the plan")
        if self.binary_artifact.repository_id is not None and (
            self.plan_run.repository_id != self.binary_artifact.repository_id
        ):
            raise ValueError("plan_run repository_id must match the plan artifact")

    def to_dict(self) -> dict[str, object]:
        return {
            "plan": self.plan.to_dict(),
            "binary_artifact": self.binary_artifact.to_dict(),
            "state_version": self.state_version.to_dict(),
            "plan_run": self.plan_run.to_dict(),
            "summary": self.summary.to_dict(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class DeploymentApproval:
    deployment_id: str
    approved_by: str
    commit_sha: str
    plan_hash: str

    def __post_init__(self) -> None:
        for name in ("deployment_id", "approved_by", "commit_sha", "plan_hash"):
            require_non_empty_string(getattr(self, name), name)

    def matches(self, plan: TerraformPlan) -> bool:
        """Return whether an approval is bound to this exact deployment plan."""
        if not isinstance(plan, TerraformPlan):
            raise TypeError("plan must be a TerraformPlan")
        return (
            self.deployment_id == plan.deployment_id
            and self.commit_sha == plan.commit_sha
            and self.plan_hash == plan.plan_hash
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "deployment_id": self.deployment_id,
            "approved_by": self.approved_by,
            "commit_sha": self.commit_sha,
            "plan_hash": self.plan_hash,
        }


class WorkflowConclusion(StrEnum):
    """GitHub Actions run conclusion re-read from the run, never trusted from an Event."""

    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkflowRunFacts:
    """Facts re-read from an Actions run so D can verify, not trust, an Event.

    D compares `workflow_path` against an allow-list, `repository_id`/`ref` against
    the approved commit, `conclusion`, and `plan_hash` against the approved plan.
    Any mismatch routes to MANUAL_REVIEW rather than a retry (ADR-0019 §7).
    """

    run_id: str
    repository_id: str
    workflow_path: str
    ref: str
    commit_sha: str
    conclusion: WorkflowConclusion
    plan_hash: str

    def __post_init__(self) -> None:
        for name in ("run_id", "repository_id", "workflow_path", "ref", "commit_sha", "plan_hash"):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.conclusion, WorkflowConclusion):
            raise TypeError("conclusion must be a WorkflowConclusion")

    def to_dict(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "repository_id": self.repository_id,
            "workflow_path": self.workflow_path,
            "ref": self.ref,
            "commit_sha": self.commit_sha,
            "conclusion": self.conclusion.value,
            "plan_hash": self.plan_hash,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplyDispatchReceipt:
    """The `workflow_dispatch` acknowledgment D obtains when it triggers apply.

    A dispatch confirms the run was requested; the authoritative apply facts still
    come from re-reading the run via `WorkflowRunReader` (ADR-0019 §5, §7).
    """

    deployment_id: str
    repository_id: str
    workflow_path: str

    def __post_init__(self) -> None:
        for name in ("deployment_id", "repository_id", "workflow_path"):
            require_non_empty_string(getattr(self, name), name)

    def to_dict(self) -> dict[str, str]:
        return {
            "deployment_id": self.deployment_id,
            "repository_id": self.repository_id,
            "workflow_path": self.workflow_path,
        }


class DeploymentStatus(StrEnum):
    """Presentation-only deployment lifecycle position (ADR-0019 §8).

    Never persisted. `derive_deployment_status()` computes it at read time from
    facts that are already durable (Job status/step, approval, rejection, apply
    run conclusion, verification result), so there is no second copy that can
    drift. Gates re-check facts directly; this value is for API/UI shape only.
    """

    PLAN_REQUESTED = "PLAN_REQUESTED"
    PLAN_COMPLETED = "PLAN_COMPLETED"
    READINESS_EVALUATED = "READINESS_EVALUATED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    APPROVED = "APPROVED"
    APPLYING = "APPLYING"
    APPLIED = "APPLIED"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    # Branches.
    BLOCKED = "BLOCKED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    REJECTED = "REJECTED"
    VERIFICATION_INDETERMINATE = "VERIFICATION_INDETERMINATE"


class DeploymentRejectionReason(StrEnum):
    """Enumerated reject reasons; free text is disallowed (ADR-0019 §8)."""

    NOT_APPROVED_BY_POLICY = "NOT_APPROVED_BY_POLICY"
    PLAN_OUTDATED = "PLAN_OUTDATED"
    RISK_TOO_HIGH = "RISK_TOO_HIGH"
    SUPERSEDED = "SUPERSEDED"
    OTHER = "OTHER"


class DeploymentReadinessSignal(StrEnum):
    """C readiness verdict as seen by the status derivation.

    Values match `DeploymentReadinessStatus` so a caller can pass its verdict
    through without this pure function importing across the C role boundary
    (which would create a contracts import cycle).
    """

    READY_FOR_APPROVAL = "READY_FOR_APPROVAL"
    BLOCKED = "BLOCKED"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class ApplyOutcome(StrEnum):
    """Apply run outcome as re-read from the Actions run (ADR-0019 §5, §7)."""

    NOT_STARTED = "NOT_STARTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class VerificationOutcome(StrEnum):
    """Post-Deploy Verification outcome as a status signal (ADR-0020)."""

    NOT_STARTED = "NOT_STARTED"
    RUNNING = "RUNNING"
    COMPARABLE = "COMPARABLE"
    INDETERMINATE = "INDETERMINATE"


@dataclass(frozen=True, slots=True, kw_only=True)
class DeploymentFacts:
    """The durable facts a deployment's status is derived from (ADR-0019 §8).

    None of these is a stored `DeploymentStatus`; they are the Job status/step,
    approval/rejection existence, the apply run outcome, and the verification
    outcome that already exist independently.
    """

    job_status: JobStatus
    current_step: JobCurrentStep
    readiness: DeploymentReadinessSignal | None = None
    is_approved: bool = False
    is_rejected: bool = False
    apply_outcome: ApplyOutcome = ApplyOutcome.NOT_STARTED
    verification_outcome: VerificationOutcome = VerificationOutcome.NOT_STARTED

    def __post_init__(self) -> None:
        if not isinstance(self.job_status, JobStatus):
            raise TypeError("job_status must be a JobStatus")
        if not isinstance(self.current_step, JobCurrentStep):
            raise TypeError("current_step must be a JobCurrentStep")
        if self.readiness is not None and not isinstance(self.readiness, DeploymentReadinessSignal):
            raise TypeError("readiness must be a DeploymentReadinessSignal or None")
        for name in ("is_approved", "is_rejected"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a bool")
        if not isinstance(self.apply_outcome, ApplyOutcome):
            raise TypeError("apply_outcome must be an ApplyOutcome")
        if not isinstance(self.verification_outcome, VerificationOutcome):
            raise TypeError("verification_outcome must be a VerificationOutcome")
        if self.is_approved and self.is_rejected:
            raise ValueError("a deployment cannot be both approved and rejected")


def derive_deployment_status(facts: DeploymentFacts) -> DeploymentStatus:
    """Return the presentation status derived from durable deployment facts.

    Precedence follows ADR-0019 §8: terminal reject first, then readiness
    branches, then the apply/verify progression. Gates never read this value.
    """
    if not isinstance(facts, DeploymentFacts):
        raise TypeError("facts must be a DeploymentFacts")

    # Terminal reject wins over everything else.
    if facts.is_rejected:
        return DeploymentStatus.REJECTED

    # A terminally failed or cancelled Job must never present as forward progress.
    # A successful apply is handled by the verification branch below; a Job that
    # failed or was cancelled before that routes to MANUAL_REVIEW so it is never
    # auto-retried and never shown as still advancing (ADR-0019 §8). Reject was
    # already handled above, so a CANCELLED here is a non-reject cancellation.
    if facts.apply_outcome is not ApplyOutcome.SUCCEEDED and facts.job_status in (
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    ):
        return DeploymentStatus.MANUAL_REVIEW

    # Verification stage (only reached after a successful apply).
    if facts.apply_outcome is ApplyOutcome.SUCCEEDED:
        if facts.verification_outcome is VerificationOutcome.INDETERMINATE:
            return DeploymentStatus.VERIFICATION_INDETERMINATE
        if facts.verification_outcome is VerificationOutcome.COMPARABLE:
            return DeploymentStatus.VERIFIED
        if facts.verification_outcome is VerificationOutcome.RUNNING:
            return DeploymentStatus.VERIFYING
        return DeploymentStatus.APPLIED

    # Apply failed or was ambiguous: never auto-retried (ADR-0019 §8).
    if facts.apply_outcome is ApplyOutcome.FAILED:
        return DeploymentStatus.MANUAL_REVIEW
    if facts.apply_outcome is ApplyOutcome.RUNNING:
        return DeploymentStatus.APPLYING

    # Approval granted but apply not yet started.
    if facts.is_approved:
        return DeploymentStatus.APPROVED

    # Readiness branches decide whether approval is even offered.
    if facts.readiness is DeploymentReadinessSignal.BLOCKED:
        return DeploymentStatus.BLOCKED
    if facts.readiness is DeploymentReadinessSignal.MANUAL_REVIEW:
        return DeploymentStatus.MANUAL_REVIEW
    if facts.readiness is DeploymentReadinessSignal.READY_FOR_APPROVAL:
        return DeploymentStatus.WAITING_APPROVAL

    # Pre-readiness progression is read from the Job's current step.
    if facts.current_step is JobCurrentStep.TERRAFORM_PLAN:
        return DeploymentStatus.PLAN_REQUESTED
    if facts.current_step is JobCurrentStep.PRE_DEPLOY_VALIDATION:
        return DeploymentStatus.READINESS_EVALUATED
    return DeploymentStatus.PLAN_COMPLETED
