"""A의 apply 완료 Event 처리 경계 (ADR-0019 §7, DATABASE.md "완료 Event 경계").

GitHub Actions의 apply run 완료 Event를 받아 D Worker가 재조회할 좌표를 예약한다. Event에서
읽는 값은 **`deployment_id`와 `run_id` 두 좌표뿐**이다. conclusion, commit, plan_hash 같은 사실은
읽지도 저장하지도 않는다 — Event는 신호이지 정본이 아니고(§7), 저장하는 순간 검증되지 않은 주장이
`derive_deployment_status()`의 입력이 된다.

Job과 Deployment는 저장된 사실에서 다시 읽는다. Event가 말하는 customer/job을 믿으면, 그 Event를
만들 수 있는 누구든 남의 Job을 재개시킬 수 있다.
"""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol

from apps.backend.deployment.record import DeploymentRecord
from apps.backend.jobs.lifecycle import transition_job
from apps.backend.jobs.models import Job
from apps.backend.jobs.outbox import OutboxDispatcher, WorkflowOutboxEntry
from apps.backend.repositories.errors import DuplicateJobError
from packages.contracts import JobCurrentStep, JobStatus, WorkflowCommand, WorkflowTask


class ApplyCompletionError(RuntimeError):
    """Apply 완료 Event를 예약으로 바꿀 수 없다."""


class DeploymentLookup(Protocol):
    def resolve_customer_id(self, *, deployment_id: str) -> str | None:
        """Return the customer that owns a deployment, resolved from storage.

        The Event names a deployment, not an owner. Taking the owner from the payload
        would let anyone able to emit an Event resume another tenant's Job.
        """
        ...

    def get_deployment(
        self, *, customer_id: str, deployment_id: str
    ) -> DeploymentRecord | None: ...


class DeploymentJobLookup(Protocol):
    def get_job(self, customer_id: str, job_id: str) -> Job | None: ...


class CompletionReservationStore(Protocol):
    def reserve_completion_event(
        self,
        *,
        deployment_id: str,
        run_id: str,
        resumed_job: Job,
        expected_revision: int,
        outbox: WorkflowOutboxEntry,
        reserved_at: str,
    ) -> None: ...


class ApplyCompletionService:
    """Turn one apply run completion Event into a reserved verification coordinate."""

    def __init__(
        self,
        *,
        deployments: DeploymentLookup,
        jobs: DeploymentJobLookup,
        reservations: CompletionReservationStore,
        outbox_dispatcher: OutboxDispatcher,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        for value, name in (
            (deployments, "deployments"),
            (jobs, "jobs"),
            (reservations, "reservations"),
            (outbox_dispatcher, "outbox_dispatcher"),
        ):
            if value is None:
                raise TypeError(f"{name} is required")
        self._deployments = deployments
        self._jobs = jobs
        self._reservations = reservations
        self._outbox_dispatcher = outbox_dispatcher
        self._now = now or (lambda: datetime.now(UTC))

    def record_completion(self, *, deployment_id: str, run_id: str) -> None:
        for value, name in ((deployment_id, "deployment_id"), (run_id, "run_id")):
            if not isinstance(value, str) or not value.strip():
                raise ApplyCompletionError(f"{name} must be a non-empty string")
        customer_id = self._deployments.resolve_customer_id(deployment_id=deployment_id)
        if not isinstance(customer_id, str) or not customer_id.strip():
            raise ApplyCompletionError("deployment owner could not be resolved")
        record = self._deployments.get_deployment(
            customer_id=customer_id, deployment_id=deployment_id
        )
        if record is None:
            raise ApplyCompletionError("deployment not found")
        job = self._jobs.get_job(customer_id, record.job_id)
        if job is None:
            raise ApplyCompletionError("deployment job not found")
        if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
            # 거절되었거나 이미 끝난 배포의 run 완료는 재개 대상이 아니다. 되살리면 사람이
            # 끝낸 결정을 Event가 뒤집는다.
            raise ApplyCompletionError("deployment job is already terminal")
        expected_revision = job.revision
        resumed = transition_job(
            job,
            expected_revision=expected_revision,
            status=JobStatus.RUNNING,
            current_step=JobCurrentStep.POST_DEPLOY_VERIFICATION,
        )
        outbox = WorkflowOutboxEntry(
            customer_id=customer_id,
            job_id=job.job_id,
            task=WorkflowTask(
                job_id=job.job_id,
                expected_revision=resumed.revision,
                command=WorkflowCommand.APPLY_COMPLETED,
            ),
        )
        try:
            self._reservations.reserve_completion_event(
                deployment_id=deployment_id,
                run_id=run_id,
                resumed_job=resumed,
                expected_revision=expected_revision,
                outbox=outbox,
                reserved_at=self._now().astimezone(UTC).isoformat().replace("+00:00", "Z"),
            )
        except DuplicateJobError:
            # 같은 run의 재전달이다. EventBridge는 at-least-once이므로 정상 경로다.
            return
        self._outbox_dispatcher.dispatch_entry(outbox)


def parse_completion_event(event: Mapping[str, object]) -> tuple[str, str]:
    """Return `(deployment_id, run_id)` from an apply run completion Event.

    Only the two coordinates are read. A payload field naming a conclusion, commit, or
    plan digest is ignored by omission — D re-reads all of those from the run itself.
    """
    if not isinstance(event, Mapping):
        raise ApplyCompletionError("completion event must be a mapping")
    detail = event.get("detail")
    if not isinstance(detail, Mapping):
        raise ApplyCompletionError("completion event detail is invalid")
    return (
        _coordinate(detail.get("deployment_id"), "deployment_id"),
        _coordinate(detail.get("run_id"), "run_id"),
    )


def _coordinate(value: object, name: str) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        # GitHub run ids arrive as numbers in some Event shapes; the stored coordinate
        # is a string, so normalize once here rather than at every reader.
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        raise ApplyCompletionError(f"completion event {name} is invalid")
    return value
