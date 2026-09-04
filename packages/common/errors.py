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


class AuthoringRunNotFound(LookupError):
    """No authoring run has been recorded for this policy source version.

    `RepositoryError`가 아니다. 저장소가 고장난 것이 아니라 아직 만들어진 실행이 없다는
    사실이고, 그 둘을 같은 예외로 올리면 호출자는 "잠시 후 다시"와 "아직 시작 안 됨"을
    구별할 수 없다 — 실제로 후보 조회가 그 이유로 503을 돌려주고 있었다.
    """


class PolicyProfileNotFound(LookupError):
    """The requested policy profile version does not exist in the caller's partition.

    게시 요청이 고른 기준선 Profile이 없다는 뜻이며, 저장소 오류가 아니라 잘못된 선택이다.
    """


class PolicySourceNotFound(LookupError):
    """Raised when a policy source version does not exist in the caller's partition."""


class PolicySourceDeleteForbidden(ValueError):
    """Raised when deleting a policy source is not allowed (e.g. it is approved).

    Not a `RepositoryError`: the write did not fail, it was refused by a domain rule, and the
    public mapping owes the caller a 409 rather than a 503.
    """
