"""AWS-independent persistence ports and injected AWS adapters."""

from apps.backend.repositories.deployment import DynamoDbDeploymentApprovalRepository
from apps.backend.repositories.dynamodb import (
    DynamoDbAssessmentWorkflowRepository,
    DynamoDbJobRepository,
)
from apps.backend.repositories.policy_approval import DynamoDbPolicyApprovalRepository
from apps.backend.repositories.ports import (
    ArtifactCollisionError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactReference,
    ArtifactStore,
    ArtifactStoreError,
    AssessmentWorkflowRepository,
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
    "AssessmentWorkflowRepository",
    "DuplicateJobError",
    "DynamoDbAssessmentWorkflowRepository",
    "DynamoDbDeploymentApprovalRepository",
    "DynamoDbPolicyApprovalRepository",
    "DynamoDbJobRepository",
    "InvalidJobMutationError",
    "JobRepository",
    "RepositoryError",
    "RevisionConflictError",
    "S3ArtifactStore",
    "StoredDataError",
]
