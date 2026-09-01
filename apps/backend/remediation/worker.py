"""C-owned, revision-bound remediation orchestration.

The worker owns command/action validation and authoritative reload. GitHub,
Terraform, branch, PR, plan, and apply behavior remain behind injected D ports.
"""

from dataclasses import dataclass
from typing import Protocol

from packages.contracts import (
    RemediationAction,
    RemediationContext,
    RemediationDecision,
    RemediationPatch,
    WorkflowCommand,
    WorkflowTask,
)


class RemediationWorkerError(ValueError):
    """Raised when a task cannot safely enter an action port."""


class RemediationWorkNotFoundError(RemediationWorkerError):
    """The authoritative work item is absent or not at the expected revision."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RemediationWork:
    """Authoritative A-owned work reloaded by C for one exact revision."""

    customer_id: str
    remediation_id: str
    job_id: str
    revision: int
    context: RemediationContext
    decision: RemediationDecision

    def __post_init__(self) -> None:
        for name in ("customer_id", "remediation_id", "job_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise TypeError("revision must be an integer")
        if self.revision < 0:
            raise ValueError("revision must be non-negative")
        if not isinstance(self.context, RemediationContext):
            raise TypeError("context must be a RemediationContext")
        if not isinstance(self.decision, RemediationDecision):
            raise TypeError("decision must be a RemediationDecision")
        if self.context.snapshot.customer_id != self.customer_id:
            raise ValueError("context customer scope does not match work")
        _require_decision_binding(self.context, self.decision)


@dataclass(frozen=True, slots=True, kw_only=True)
class RemediationSyncTarget:
    """Validated current IaC commit that D may use as a later Plan input."""

    finding_id: str
    customer_id: str
    repository_id: str
    commit_sha: str

    def __post_init__(self) -> None:
        for name in ("finding_id", "customer_id", "repository_id", "commit_sha"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")


class RemediationWorkRepository(Protocol):
    def get_work(self, *, job_id: str, expected_revision: int) -> RemediationWork | None: ...


class PatchAction(Protocol):
    def generate(
        self, *, context: RemediationContext, decision: RemediationDecision
    ) -> RemediationPatch: ...


class SyncAction(Protocol):
    def prepare(
        self, *, context: RemediationContext, decision: RemediationDecision
    ) -> RemediationSyncTarget: ...


class RemediationResultStore(Protocol):
    def put_result_if_absent(
        self,
        *,
        work: RemediationWork,
        result: RemediationPatch | RemediationSyncTarget,
    ) -> None: ...


class RemediationWorker:
    """Dispatch one stored policy decision through exactly one injected D port."""

    _COMMAND_ACTION = {
        WorkflowCommand.GENERATE_REMEDIATION: RemediationAction.TERRAFORM_PATCH,
        WorkflowCommand.SYNC_ACTUAL_STATE: RemediationAction.ACTUAL_SYNC,
    }

    def __init__(
        self,
        *,
        work_repository: RemediationWorkRepository,
        patch_action: PatchAction,
        sync_action: SyncAction,
        result_store: RemediationResultStore,
    ) -> None:
        if any(
            dependency is None
            for dependency in (work_repository, patch_action, sync_action, result_store)
        ):
            raise TypeError("all remediation worker dependencies are required")
        self._work_repository = work_repository
        self._patch_action = patch_action
        self._sync_action = sync_action
        self._result_store = result_store

    def handle(self, task: WorkflowTask) -> RemediationPatch | RemediationSyncTarget:
        if not isinstance(task, WorkflowTask):
            raise TypeError("task must be a WorkflowTask")
        required_action = self._COMMAND_ACTION.get(task.command)
        if required_action is None:
            raise RemediationWorkerError("unsupported remediation command")
        work = self._work_repository.get_work(
            job_id=task.job_id, expected_revision=task.expected_revision
        )
        if work is None:
            raise RemediationWorkNotFoundError(
                "remediation work is missing or stale for the expected revision"
            )
        if not isinstance(work, RemediationWork):
            raise RemediationWorkerError("repository returned invalid remediation work")
        if work.job_id != task.job_id or work.revision != task.expected_revision:
            raise RemediationWorkerError("remediation work does not match the task")
        _require_decision_binding(work.context, work.decision)
        if not work.decision.is_actionable or work.decision.action is not required_action:
            raise RemediationWorkerError("stored decision does not match the task command")

        if required_action is RemediationAction.TERRAFORM_PATCH:
            result = self._patch_action.generate(context=work.context, decision=work.decision)
            self._require_patch_result(work, result)
        else:
            result = self._sync_action.prepare(context=work.context, decision=work.decision)
            self._require_sync_result(work, result)
        self._result_store.put_result_if_absent(work=work, result=result)
        return result

    @staticmethod
    def _require_patch_result(work: RemediationWork, result: object) -> None:
        if not isinstance(result, RemediationPatch):
            raise RemediationWorkerError("patch action must return a RemediationPatch")
        context = work.context
        if (
            result.finding_id != context.finding.finding_id
            or result.base_commit_sha != context.snapshot.commit_sha
            or result.artifact.customer_id != work.customer_id
            or result.artifact.repository_id != context.snapshot.repository_id
        ):
            raise RemediationWorkerError("patch result is outside remediation work")

    @staticmethod
    def _require_sync_result(work: RemediationWork, result: object) -> None:
        if not isinstance(result, RemediationSyncTarget):
            raise RemediationWorkerError("sync action must return a RemediationSyncTarget")
        context = work.context
        if (
            result.finding_id != context.finding.finding_id
            or result.customer_id != work.customer_id
            or result.repository_id != context.snapshot.repository_id
            or result.commit_sha != context.snapshot.commit_sha
        ):
            raise RemediationWorkerError("sync target is outside remediation work")


def _require_decision_binding(context: RemediationContext, decision: RemediationDecision) -> None:
    finding = context.finding
    if (
        decision.finding_id,
        decision.resource_id,
        decision.rule_id,
        decision.rule_version,
        decision.perspective,
    ) != (
        finding.finding_id,
        finding.resource_id,
        finding.rule_id,
        finding.rule_version,
        finding.perspective,
    ):
        raise RemediationWorkerError("remediation decision is outside the context identity")
