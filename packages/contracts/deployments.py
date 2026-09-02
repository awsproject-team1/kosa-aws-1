"""GitHub, read-only AWS, and approved Terraform deployment contracts."""

from dataclasses import dataclass
from enum import StrEnum

from packages.contracts._validation import require_non_empty_string


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
class PlanExecutionResult:
    """D's `PlanRequestPort` output: the hashed plan, its saved binary, and state.

    `plan` carries the allow-listed projection whose digest is `plan_hash`.
    `binary_artifact` is the saved binary plan that `terraform apply` consumes and
    is never a hash target. `state_version` is the plan-time `(lineage, serial)`
    re-checked before apply. All three refer to the same deployment (ADR-0019 §1, §2).
    """

    plan: TerraformPlan
    binary_artifact: ArtifactReference
    state_version: TerraformStateVersion

    def __post_init__(self) -> None:
        if not isinstance(self.plan, TerraformPlan):
            raise TypeError("plan must be a TerraformPlan")
        if not isinstance(self.binary_artifact, ArtifactReference):
            raise TypeError("binary_artifact must be an ArtifactReference")
        if self.binary_artifact.artifact_type is not ArtifactType.TERRAFORM_PLAN_BINARY:
            raise ValueError("binary_artifact must be a TERRAFORM_PLAN_BINARY")
        if not isinstance(self.state_version, TerraformStateVersion):
            raise TypeError("state_version must be a TerraformStateVersion")

    def to_dict(self) -> dict[str, object]:
        return {
            "plan": self.plan.to_dict(),
            "binary_artifact": self.binary_artifact.to_dict(),
            "state_version": self.state_version.to_dict(),
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
