"""C-owned remediation orchestration and readiness boundaries."""

from apps.backend.remediation.context import RemediationContextError, build_remediation_context
from apps.backend.remediation.generator import FixturePatchGenerator
from apps.backend.remediation.readiness import evaluate_deployment_readiness
from apps.backend.remediation.service import (
    RemediationContractError,
    RemediationNotAutomatableError,
    RemediationService,
)
from apps.backend.remediation.worker import (
    PatchAction,
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
    "PatchAction",
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
