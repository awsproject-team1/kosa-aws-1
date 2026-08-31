"""AWS-independent persistence ports and provider-neutral failures."""

import re
from dataclasses import dataclass
from typing import Protocol

from apps.backend.assessment import Assessment
from apps.backend.jobs.models import Job


class RepositoryError(RuntimeError):
    """Base failure for a persistence provider operation."""


class DuplicateJobError(RepositoryError):
    """Raised when creating an existing Job would replace it."""


class RevisionConflictError(RepositoryError):
    """Raised when a persisted Job revision no longer matches."""


class InvalidJobMutationError(RepositoryError):
    """Raised when a candidate Job bypasses the lifecycle policy."""


class StoredDataError(RepositoryError):
    """Raised when persisted data does not satisfy the domain model."""


class ArtifactStoreError(RepositoryError):
    """Base failure for artifact storage operations."""


class ArtifactNotFoundError(ArtifactStoreError):
    """Raised when an artifact reference does not exist."""


class ArtifactCollisionError(ArtifactStoreError):
    """Raised when a content-addressed key contains different bytes."""


class ArtifactIntegrityError(ArtifactStoreError):
    """Raised when stored bytes do not match their content digest."""


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


class AssessmentRepository(Protocol):
    """Persistence operation for the selectors that start an Assessment workflow."""

    def create_assessment(self, assessment: Assessment) -> None:
        """Persist a new Assessment record without replacing an existing record."""
        ...


class ArtifactStore(Protocol):
    """Immutable content-addressed byte storage operations."""

    def put(self, content: bytes) -> ArtifactReference:
        """Store bytes immutably and return their content digest reference."""
        ...

    def get(self, reference: ArtifactReference) -> bytes:
        """Load bytes and verify they match the content digest reference."""
        ...
