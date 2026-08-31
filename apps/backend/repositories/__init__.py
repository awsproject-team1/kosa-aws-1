"""AWS-independent persistence ports and injected AWS adapters."""

from apps.backend.repositories.dynamodb import DynamoDbAssessmentRepository, DynamoDbJobRepository
from apps.backend.repositories.ports import (
    ArtifactCollisionError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactReference,
    ArtifactStore,
    ArtifactStoreError,
    AssessmentRepository,
    DuplicateJobError,
    InvalidJobMutationError,
    JobRepository,
    RepositoryError,
    RevisionConflictError,
    StoredDataError,
)
from apps.backend.repositories.s3 import S3ArtifactStore

__all__ = [
    "ArtifactCollisionError",
    "ArtifactIntegrityError",
    "ArtifactNotFoundError",
    "ArtifactReference",
    "ArtifactStore",
    "ArtifactStoreError",
    "AssessmentRepository",
    "DuplicateJobError",
    "DynamoDbAssessmentRepository",
    "DynamoDbJobRepository",
    "InvalidJobMutationError",
    "JobRepository",
    "RepositoryError",
    "RevisionConflictError",
    "S3ArtifactStore",
    "StoredDataError",
]
