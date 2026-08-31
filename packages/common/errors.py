"""Provider-neutral persistence failures shared across boundaries.

이 모듈은 어떤 도메인 모듈도 import하지 않는다. `apps.backend.repositories`의 패키지
`__init__`이 concrete adapter를 통해 `apps.backend.assessment`를 import하므로, 예외가
그 패키지 안에 있으면 submodule만 import해도 패키지 초기화가 함께 일어나 순환이 생긴다.
정책 경계처럼 저장소 구현과 무관한 코드도 이 예외 타입을 쓸 수 있어야 한다.
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
