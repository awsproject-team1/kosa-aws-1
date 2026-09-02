"""Durable deployment record and its read projection (ADR-0019 §4, §8).

The `DEPLOYMENT#{deployment_id}` item binds the approved default-branch commit,
its `plan_hash`, the plan/binary artifact references, the plan-time Terraform
state `(lineage, serial)`, and the correlation ids (`remediation_id`,
`source_assessment_id`, later `verification_assessment_id`). `DeploymentStatus`
is never stored here; it is derived at read time from durable facts.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    DeploymentRejectionReason,
    TerraformStateVersion,
)
from packages.contracts._validation import (
    require_non_empty_string,
    require_optional_non_empty_string,
)

if TYPE_CHECKING:
    from apps.backend.jobs.models import Job
    from apps.backend.jobs.outbox import WorkflowOutboxEntry


@dataclass(frozen=True, slots=True, kw_only=True)
class DeploymentRecord:
    """A deployment's durable facts (ADR-0019 §4).

    At creation only the target commit and correlation ids exist; the plan is run
    later by the D Deployment Worker consuming RUN_DEPLOYMENT. The plan facts
    (`plan_hash`, plan/binary artifacts, state version) are therefore optional and
    are filled in on PLAN_COMPLETED. They are all-present-or-all-absent so a record
    never carries a half-written plan.
    """

    deployment_id: str
    customer_id: str
    repository_id: str
    job_id: str
    remediation_id: str
    commit_sha: str
    source_assessment_id: str
    plan_hash: str | None = None
    plan_artifact: ArtifactReference | None = None
    binary_artifact: ArtifactReference | None = None
    state_version: TerraformStateVersion | None = None
    verification_assessment_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "deployment_id",
            "customer_id",
            "repository_id",
            "job_id",
            "remediation_id",
            "commit_sha",
            "source_assessment_id",
        ):
            require_non_empty_string(getattr(self, name), name)
        require_optional_non_empty_string(self.plan_hash, "plan_hash")
        require_optional_non_empty_string(
            self.verification_assessment_id, "verification_assessment_id"
        )
        plan_fields = (self.plan_hash, self.plan_artifact, self.binary_artifact, self.state_version)
        if any(value is not None for value in plan_fields) and not all(
            value is not None for value in plan_fields
        ):
            raise ValueError("plan facts must be all present or all absent")
        if self.plan_artifact is not None:
            if not isinstance(self.plan_artifact, ArtifactReference):
                raise TypeError("plan_artifact must be an ArtifactReference")
            if self.plan_artifact.artifact_type is not ArtifactType.TERRAFORM_PLAN:
                raise ValueError("plan_artifact must be a TERRAFORM_PLAN")
            if self.plan_hash != self.plan_artifact.content_sha256:
                raise ValueError("plan_hash must match the plan artifact digest")
            self._require_artifact_scope(self.plan_artifact, "plan_artifact")
        if self.binary_artifact is not None:
            if not isinstance(self.binary_artifact, ArtifactReference):
                raise TypeError("binary_artifact must be an ArtifactReference")
            if self.binary_artifact.artifact_type is not ArtifactType.TERRAFORM_PLAN_BINARY:
                raise ValueError("binary_artifact must be a TERRAFORM_PLAN_BINARY")
            self._require_artifact_scope(self.binary_artifact, "binary_artifact")
        if self.state_version is not None and not isinstance(
            self.state_version, TerraformStateVersion
        ):
            raise TypeError("state_version must be a TerraformStateVersion")

    def _require_artifact_scope(self, artifact: ArtifactReference, name: str) -> None:
        # An artifact must belong to the deployment's own customer and repository;
        # a type-only check would let a plan/binary from another tenant or repo bind
        # to this deployment (PR #48 review [P1]).
        if artifact.customer_id != self.customer_id:
            raise ValueError(f"{name} customer_id must match the deployment customer_id")
        if artifact.repository_id != self.repository_id:
            raise ValueError(f"{name} repository_id must match the deployment repository_id")

    @property
    def has_plan(self) -> bool:
        """Return whether the plan facts have been filled in after PLAN_COMPLETED."""
        return self.plan_hash is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "deployment_id": self.deployment_id,
            "customer_id": self.customer_id,
            "repository_id": self.repository_id,
            "job_id": self.job_id,
            "remediation_id": self.remediation_id,
            "commit_sha": self.commit_sha,
            "source_assessment_id": self.source_assessment_id,
            "plan_hash": self.plan_hash,
            "plan_artifact": None if self.plan_artifact is None else self.plan_artifact.to_dict(),
            "binary_artifact": (
                None if self.binary_artifact is None else self.binary_artifact.to_dict()
            ),
            "state_version": None if self.state_version is None else self.state_version.to_dict(),
            "verification_assessment_id": self.verification_assessment_id,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class DeploymentRejection:
    """An Admin's terminal reject decision (ADR-0019 §8)."""

    deployment_id: str
    rejected_by: str
    reason: DeploymentRejectionReason
    rejected_at: str
    ticket_reference: str | None = None

    def __post_init__(self) -> None:
        for name in ("deployment_id", "rejected_by", "rejected_at"):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.reason, DeploymentRejectionReason):
            raise TypeError("reason must be a DeploymentRejectionReason")
        require_optional_non_empty_string(self.ticket_reference, "ticket_reference")

    def to_dict(self) -> dict[str, object]:
        return {
            "deployment_id": self.deployment_id,
            "rejected_by": self.rejected_by,
            "reason": self.reason.value,
            "rejected_at": self.rejected_at,
            "ticket_reference": self.ticket_reference,
        }


class DeploymentRecordRepository(Protocol):
    """Persist a new deployment and read a stored deployment record."""

    def create_deployment(
        self, record: "DeploymentRecord", *, job: "Job", outbox: "WorkflowOutboxEntry"
    ) -> None:
        """Atomically write the deployment, its Job, outbox, and requested audit."""
        ...

    def get_deployment(self, *, customer_id: str, deployment_id: str) -> "DeploymentRecord | None":
        """Return the stored deployment record or None when it is absent."""
        ...

    def reject_deployment(
        self, *, rejection: "DeploymentRejection", cancelled_job: "Job", expected_revision: int
    ) -> None:
        """Write a terminal rejection, cancel the Job, and audit it atomically."""
        ...
