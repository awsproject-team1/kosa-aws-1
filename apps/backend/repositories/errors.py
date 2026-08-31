"""Provider-neutral persistence failures.

이 모듈은 어떤 도메인 모듈도 import하지 않는다. `ports`가 Job/Assessment/Outbox를 import하고
`apps.backend.jobs.errors`가 다시 이 예외들을 import하므로, 예외를 leaf로 분리하지 않으면
먼저 import되는 진입점에 따라 순환 import가 발생한다.
"""


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
