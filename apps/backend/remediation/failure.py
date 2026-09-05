"""Turn a terminal remediation failure into a recorded, visible state.

Remediation Worker가 예외로 죽으면 SQS가 재시도하고, 마지막 재시도 뒤 message는 DLQ로 간다 —
그런데 remediation record와 Job은 QUEUED로 남는다. 화면은 "Worker 결과 대기 중"을 영영 보이고,
재시도마다 Bedrock을 다시 부르며 모델이 다른 patch를 내 branch·PR이 하나씩 늘었다(라이브: 한
finding에 PR 20개). 실패에는 두 종류가 있다.

- **다시 보내도 같은 실패**: 모델 출력 형식(`BedrockPatchError`), GitHub의 4xx 거부, 저장된
  work·decision과 어긋나는 task. 재시도는 비용만 쓴다. 기록하고 message를 소비한다.
- **다음에 다를 수 있는 실패**: 저장소·네트워크·5xx. 예외를 올려 SQS가 재시도하게 둔다.

기록은 두 곳에 남는다: remediation record(`status=FAILED`, `failure`)와 Job(`FAILED`, `error`).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Protocol

from agent.runtime.github_write_tool import GitHubWriteToolError
from apps.backend.jobs.lifecycle import transition_job
from apps.backend.jobs.models import Job
from apps.backend.remediation.bedrock import BedrockPatchError
from apps.backend.remediation.pull_request import PullRequestActionError
from apps.backend.remediation.worker import (
    RemediationWork,
    RemediationWorkerError,
    RemediationWorkNotFoundError,
)
from packages.contracts import ApiError, JobStatus, WorkflowTask

_LOGGER = logging.getLogger("governance.remediation")

#: Job이 더 이상 실패로 옮겨질 수 없는 상태. 이미 끝난 Job의 상태를 뒤집지 않는다.
_SETTLED = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})


class WorkReader(Protocol):
    def get_work(self, *, job_id: str, expected_revision: int) -> RemediationWork | None: ...


class FailureStore(Protocol):
    def put_failure_if_absent(self, *, work: RemediationWork, code: str, reason: str) -> None: ...


class JobStore(Protocol):
    def get_job(self, customer_id: str, job_id: str) -> Job | None: ...

    def update_job(self, job: Job, *, expected_revision: int) -> None: ...


def failure_code(error: BaseException) -> str | None:
    """The stable code for a failure that retrying cannot change, or None if it might."""
    if isinstance(error, BedrockPatchError):
        return "PATCH_GENERATION_FAILED"
    if isinstance(error, PullRequestActionError):
        return "PULL_REQUEST_FAILED"
    if isinstance(error, GitHubWriteToolError):
        status = getattr(error, "status", None)
        # 4xx는 같은 요청을 다시 보내도 같다. status가 없는 실패(네트워크)와 5xx는 재시도 대상.
        return "PULL_REQUEST_FAILED" if isinstance(status, int) and 400 <= status < 500 else None
    if isinstance(error, RemediationWorkerError):
        return "REMEDIATION_WORK_INVALID"
    return None


def is_terminal(error: BaseException) -> bool:
    return failure_code(error) is not None


def failure_reason(error: BaseException) -> str:
    """A reason a person can read: the exception type and its fixed message, bounded."""
    text = str(error).strip()
    return f"{type(error).__name__}: {text}"[:300] if text else type(error).__name__


class RemediationFailureRecorder:
    def __init__(
        self, *, work_repository: WorkReader, result_store: FailureStore, jobs: JobStore
    ) -> None:
        if work_repository is None or result_store is None or jobs is None:
            raise TypeError("work_repository, result_store, and jobs are required")
        self._work_repository = work_repository
        self._result_store = result_store
        self._jobs = jobs

    def record(self, task: WorkflowTask, error: BaseException) -> bool:
        """Record the failure on the remediation and its Job. Returns False when nothing was found."""
        if not isinstance(task, WorkflowTask):
            raise TypeError("task must be a WorkflowTask")
        code = failure_code(error) or "REMEDIATION_FAILED"
        reason = failure_reason(error)
        if isinstance(error, RemediationWorkNotFoundError):
            # 저장된 work가 없거나 revision이 지났다. 적을 record가 없고, 재시도해도 생기지 않는다.
            _LOGGER.warning("remediation task dropped: job=%s: %s", task.job_id, reason)
            return False
        work = self._work_repository.get_work(
            job_id=task.job_id, expected_revision=task.expected_revision
        )
        if work is None:
            _LOGGER.warning(
                "remediation failure not recorded (no work): job=%s: %s", task.job_id, reason
            )
            return False
        self._result_store.put_failure_if_absent(work=work, code=code, reason=reason)
        self._fail_job(work, task.job_id, code, reason)
        _LOGGER.error(
            "remediation failed terminally: remediation=%s job=%s code=%s: %s",
            work.remediation_id,
            task.job_id,
            code,
            reason,
        )
        return True

    def _fail_job(self, work: RemediationWork, job_id: str, code: str, reason: str) -> None:
        # Job 기록 실패가 remediation 기록까지 되돌리지는 않는다 — 둘 중 하나라도 남는 편이 낫다.
        try:
            job = self._jobs.get_job(work.customer_id, job_id)
            if job is None or job.status in _SETTLED:
                return
            failed = transition_job(
                job,
                expected_revision=job.revision,
                status=JobStatus.FAILED,
                error=ApiError(code=code, message=reason[:200]),
            )
            self._jobs.update_job(failed, expected_revision=job.revision)
        except Exception as job_error:  # noqa: BLE001 - 기록 실패는 로그로만 남긴다.
            _LOGGER.warning("job failure not recorded: job=%s: %s", job_id, job_error)


def describe(failure: Mapping[str, object]) -> str:
    return f"{failure.get('code')}: {failure.get('reason')}"
