"""AWS-independent persistence ports and provider-neutral failures."""

import re
from dataclasses import dataclass
from typing import Protocol

from apps.backend.assessment import Assessment
from apps.backend.jobs.models import Job
from apps.backend.jobs.outbox import OutboxRepository, WorkflowOutboxEntry

# 예외는 순환 import를 피하려고 leaf 모듈에 정의하고 여기서 재노출한다.
# `ports`는 Job/Assessment/Outbox를 import하고 `apps.backend.jobs.errors`는 이 예외들을
# import하므로, 예외가 `ports`에 있으면 어떤 모듈을 먼저 import하느냐에 따라 깨진다.
from apps.backend.repositories.errors import (
    ArtifactCollisionError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStoreError,
    DuplicateJobError,
    InvalidJobMutationError,
    RepositoryError,
    RevisionConflictError,
    StoredDataError,
)

__all__ = [
    "ArtifactCollisionError",
    "ArtifactIntegrityError",
    "ArtifactNotFoundError",
    "ArtifactReference",
    "ArtifactStore",
    "ArtifactStoreError",
    "AssessmentWorkflowRepository",
    "DuplicateJobError",
    "InvalidJobMutationError",
    "JobRepository",
    "RepositoryError",
    "RevisionConflictError",
    "StoredDataError",
]


_SHA256_REFERENCE = re.compile(r"sha256:([0-9a-f]{64})")


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactReference:
    """Storage-neutral reference to immutable bytes by SHA-256 digest."""

    customer_id: str
    content_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.customer_id, str) or not self.customer_id.strip():
            raise ValueError("customer_id must be a non-empty string")
        if not isinstance(self.content_digest, str):
            raise TypeError("content_digest must be a string")
        if _SHA256_REFERENCE.fullmatch(self.content_digest) is None:
            raise ValueError("content_digest must use sha256:<64 lowercase hex characters>")

    @property
    def hex_digest(self) -> str:
        """Return the validated hexadecimal digest."""
        return self.content_digest.removeprefix("sha256:")


class JobRepository(Protocol):
    """Persistence operations required by the Job application boundary."""

    def create_job(self, job: Job) -> None:
        """Persist a new revision-zero Job without replacing an existing Job."""
        ...

    def get_job(self, customer_id: str, job_id: str) -> Job | None:
        """Return one customer-scoped Job by ID or None when absent."""
        ...

    def update_job(self, job: Job, *, expected_revision: int) -> None:
        """Persist a next Job state only when the stored revision matches."""
        ...


class AssessmentWorkflowRepository(JobRepository, OutboxRepository, Protocol):
    """Atomically persist the Assessment, Job, and its pending workflow dispatch."""

    def create_assessment_workflow(
        self, assessment: Assessment, job: Job, outbox: WorkflowOutboxEntry
    ) -> None:
        """Create all workflow-start state in one storage transaction."""
        ...


class ArtifactStore(Protocol):
    """Immutable content-addressed byte storage operations."""

    def put(self, content: bytes) -> ArtifactReference:
        """Store bytes immutably and return their content digest reference."""
        ...

    def get(self, reference: ArtifactReference) -> bytes:
        """Load bytes and verify they match the content digest reference."""
        ...
