"""C-owned remediation orchestration and readiness boundaries."""

from apps.backend.remediation.context import RemediationContextError, build_remediation_context
from apps.backend.remediation.generator import FixturePatchGenerator
from apps.backend.remediation.patch_content import (
    InMemoryPatchContentStore,
    PatchContent,
    PatchContentError,
    PatchContentStore,
    decode_patch_content,
    encode_patch_content,
)
from apps.backend.remediation.pull_request import (
    PatchPullRequestAction,
    PullRequestActionError,
    PullRequestWriter,
)
from apps.backend.remediation.readiness import evaluate_deployment_readiness
from apps.backend.remediation.service import (
    RemediationContractError,
    RemediationNotAutomatableError,
    RemediationService,
)
from apps.backend.remediation.worker import (
    PatchAction,
    PullRequestAction,
    RemediationResultStore,
    RemediationSyncTarget,
    RemediationWork,
    RemediationWorker,
    RemediationWorkerError,
    RemediationWorkNotFoundError,
    RemediationWorkRepository,
    SyncAction,
)

__all__ = [
    "FixturePatchGenerator",
    "InMemoryPatchContentStore",
    "PatchAction",
    "PatchContent",
    "PatchContentError",
    "PatchContentStore",
    "PatchPullRequestAction",
    "PullRequestAction",
    "PullRequestActionError",
    "PullRequestWriter",
    "decode_patch_content",
    "encode_patch_content",
    "RemediationContextError",
    "RemediationContractError",
    "RemediationNotAutomatableError",
    "RemediationResultStore",
    "RemediationService",
    "RemediationSyncTarget",
    "RemediationWork",
    "RemediationWorker",
    "RemediationWorkerError",
    "RemediationWorkNotFoundError",
    "RemediationWorkRepository",
    "SyncAction",
    "build_remediation_context",
    "evaluate_deployment_readiness",
]
