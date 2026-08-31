"""Backward-compatible re-export of the shared persistence failures.

정본은 `packages/common/errors.py`다. 기존 import 경로를 깨지 않기 위해 여기서 재노출한다.
"""

from packages.common.errors import (
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
    "ArtifactStoreError",
    "DuplicateJobError",
    "InvalidJobMutationError",
    "RepositoryError",
    "RevisionConflictError",
    "StoredDataError",
]
