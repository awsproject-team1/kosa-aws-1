"""AWS-independent persistence ports and injected AWS adapters."""

from apps.backend.repositories.audit import DynamoDbAuditEventRepository
from apps.backend.repositories.comparison_input import DynamoDbComparisonInputReader
from apps.backend.repositories.deployment import (
    DynamoDbDeploymentApprovalRepository,
    DynamoDbDeploymentPlanStore,
    DynamoDbDeploymentRepository,
    DynamoDbDeploymentRunStore,
    DynamoDbDeploymentVerificationStore,
)
from apps.backend.repositories.deployment_verification import (
    DynamoDbPostDeployVerificationStore,
    DynamoDbVerificationSourceReader,
)
from apps.backend.repositories.deployment_work import DynamoDbDeploymentWorkRepository
from apps.backend.repositories.dynamodb import (
    DynamoDbAssessmentWorkflowRepository,
    DynamoDbJobRepository,
)
from apps.backend.repositories.observability import assemble_audit_trail_metric
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
from apps.backend.repositories.remediation import DynamoDbRemediationExceptionRepository
from apps.backend.repositories.remediation_result import DynamoDbRemediationResultStore
from apps.backend.repositories.remediation_work import DynamoDbRemediationWorkRepository
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
    "DynamoDbAuditEventRepository",
    "DynamoDbComparisonInputReader",
    "DynamoDbDeploymentApprovalRepository",
    "DynamoDbDeploymentPlanStore",
    "DynamoDbDeploymentRepository",
    "DynamoDbDeploymentRunStore",
    "DynamoDbDeploymentVerificationStore",
    "DynamoDbDeploymentWorkRepository",
    "DynamoDbPolicyApprovalRepository",
    "DynamoDbPostDeployVerificationStore",
    "DynamoDbVerificationSourceReader",
    "DynamoDbRemediationExceptionRepository",
    "DynamoDbRemediationResultStore",
    "DynamoDbRemediationWorkRepository",
    "DynamoDbJobRepository",
    "InvalidJobMutationError",
    "JobRepository",
    "RepositoryError",
    "RevisionConflictError",
    "S3ArtifactStore",
    "StoredDataError",
    "assemble_audit_trail_metric",
]
