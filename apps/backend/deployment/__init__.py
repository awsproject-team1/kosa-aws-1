"""M2 A deployment approval boundary, M3 D execution ports/worker, and deployment record.

`apps.backend.deployment.completion`과 `apps.backend.deployment.verification`은 여기서 재노출하지
않는다. 둘 다 `apps.backend.jobs`를 통해 repositories 계층을 끌어오므로, package surface에 올리면
`agent.runtime → deployment → jobs → repositories → assessment → agent.runtime` 순환 import가 생긴다.
composition root(`runtime.py`)가 모듈 경로로 직접 import한다.
"""

from apps.backend.deployment.approval import (
    DeploymentApprovalError,
    DeploymentApprovalRepository,
    DeploymentApprovalService,
    DeploymentConflictError,
)
from apps.backend.deployment.ports import (
    ActualRereadPort,
    ApplyDispatchPort,
    DeploymentCommitResolver,
    PlanRequestPort,
    WorkflowRunReader,
)
from apps.backend.deployment.record import (
    DeploymentRecord,
    DeploymentRecordRepository,
    DeploymentRejection,
)
from apps.backend.deployment.worker import (
    DeploymentApplyBlockedError,
    DeploymentPlanStore,
    DeploymentRunStore,
    DeploymentVerificationStore,
    DeploymentWork,
    DeploymentWorker,
    DeploymentWorkerError,
    DeploymentWorkNotFoundError,
    DeploymentWorkRepository,
    VerificationStarter,
)

__all__ = [
    "ActualRereadPort",
    "ApplyDispatchPort",
    "DeploymentApplyBlockedError",
    "DeploymentApprovalError",
    "DeploymentApprovalRepository",
    "DeploymentApprovalService",
    "DeploymentCommitResolver",
    "DeploymentConflictError",
    "DeploymentPlanStore",
    "DeploymentRecord",
    "DeploymentRecordRepository",
    "DeploymentRejection",
    "DeploymentRunStore",
    "DeploymentVerificationStore",
    "DeploymentWork",
    "DeploymentWorker",
    "DeploymentWorkerError",
    "DeploymentWorkNotFoundError",
    "DeploymentWorkRepository",
    "PlanRequestPort",
    "VerificationStarter",
    "WorkflowRunReader",
]
