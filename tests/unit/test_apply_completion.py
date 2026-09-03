"""A의 apply 완료 Event 예약 경계 테스트 (ADR-0019 §7, DATABASE.md "완료 Event 경계").

고정하는 불변식:
- Event에서 읽는 값은 `deployment_id`/`run_id` 두 좌표뿐이다. conclusion 같은 주장은 무시한다.
- 소유 고객은 Event가 아니라 저장에서 해석한다.
- 예약 item은 `PENDING_VERIFICATION`이고 `run_id`만 담는다 — 검증된 사실은 D가 채운다.
- 같은 run의 재전달은 조용히 흡수하고 task를 두 번 넣지 않는다.
- 이미 terminal인 Job은 Event로 되살아나지 않는다.
"""

import unittest
from datetime import UTC, datetime

from apps.backend.deployment.completion import (
    ApplyCompletionError,
    ApplyCompletionService,
    parse_completion_event,
)
from apps.backend.deployment.record import DeploymentRecord
from apps.backend.jobs.models import Job
from apps.backend.jobs.outbox import OutboxDispatcher
from apps.backend.repositories.deployment_completion import DynamoDbDeploymentCompletionStore
from apps.backend.repositories.errors import DuplicateJobError
from packages.contracts import JobCurrentStep, JobStatus, WorkflowCommand

CUSTOMER_ID = "cust-001"
DEPLOYMENT_ID = "dep-001"
JOB_ID = "job-001"
RUN_ID = "1234567890"
NOW = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)


def _record() -> DeploymentRecord:
    return DeploymentRecord(
        deployment_id=DEPLOYMENT_ID,
        customer_id=CUSTOMER_ID,
        repository_id="repo-001",
        job_id=JOB_ID,
        remediation_id="rem-001",
        commit_sha="a" * 40,
        source_assessment_id="asm-source",
    )


def _job(status: JobStatus = JobStatus.RUNNING, revision: int = 3) -> Job:
    return Job(
        job_id=JOB_ID,
        customer_id=CUSTOMER_ID,
        job_type="DEPLOYMENT",
        status=status,
        current_step=JobCurrentStep.APPLY,
        requested_by="subject-001",
        revision=revision,
        deployment_id=DEPLOYMENT_ID,
    )


_DEFAULT = object()


class FakeDeployments:
    def __init__(self, record: object = _DEFAULT, customer_id: object = CUSTOMER_ID) -> None:
        self.record = _record() if record is _DEFAULT else record
        self.customer_id = customer_id

    def resolve_customer_id(self, *, deployment_id: str):
        return self.customer_id

    def get_deployment(self, *, customer_id: str, deployment_id: str):
        return self.record


class FakeJobs:
    def __init__(self, job: object) -> None:
        self.job = job

    def get_job(self, customer_id: str, job_id: str):
        return self.job


class FakeReservations:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    def reserve_completion_event(self, **kwargs: object) -> None:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error


class FakeOutboxRepository:
    def mark_outbox_dispatched(self, entry):
        return None

    def record_outbox_dispatch_failure(self, entry):
        return None

    def list_pending_outbox(self, *, limit):
        return ()


class FakeDispatcher:
    def __init__(self) -> None:
        self.tasks: list[object] = []

    def dispatch(self, task) -> None:
        self.tasks.append(task)


def _service(
    *,
    job: object = _DEFAULT,
    deployments: FakeDeployments | None = None,
    reservations: FakeReservations | None = None,
    dispatcher: FakeDispatcher | None = None,
) -> ApplyCompletionService:
    return ApplyCompletionService(
        deployments=deployments or FakeDeployments(),
        jobs=FakeJobs(_job() if job is _DEFAULT else job),
        reservations=reservations or FakeReservations(),
        outbox_dispatcher=OutboxDispatcher(
            repository=FakeOutboxRepository(), dispatcher=dispatcher or FakeDispatcher()
        ),
        now=lambda: NOW,
    )


class ParseCompletionEventTest(unittest.TestCase):
    def test_reads_only_the_two_coordinates(self) -> None:
        deployment_id, run_id = parse_completion_event(
            {
                "detail": {
                    "deployment_id": DEPLOYMENT_ID,
                    "run_id": RUN_ID,
                    # 아래 주장들은 신호일 뿐이라 읽지 않는다. D가 run에서 다시 읽는다.
                    "conclusion": "success",
                    "plan_hash": "b" * 64,
                }
            }
        )
        self.assertEqual((deployment_id, run_id), (DEPLOYMENT_ID, RUN_ID))

    def test_normalizes_a_numeric_run_id(self) -> None:
        _, run_id = parse_completion_event(
            {"detail": {"deployment_id": DEPLOYMENT_ID, "run_id": 1234567890}}
        )
        self.assertEqual(run_id, RUN_ID)

    def test_rejects_a_missing_coordinate(self) -> None:
        for detail in ({"run_id": RUN_ID}, {"deployment_id": DEPLOYMENT_ID}, {}):
            with self.assertRaises(ApplyCompletionError):
                parse_completion_event({"detail": detail})
        with self.assertRaises(ApplyCompletionError):
            parse_completion_event({})


class ApplyCompletionServiceTest(unittest.TestCase):
    def test_reserves_the_coordinate_and_enqueues_apply_completed(self) -> None:
        reservations, dispatcher = FakeReservations(), FakeDispatcher()
        _service(reservations=reservations, dispatcher=dispatcher).record_completion(
            deployment_id=DEPLOYMENT_ID, run_id=RUN_ID
        )
        call = reservations.calls[0]
        self.assertEqual(call["run_id"], RUN_ID)
        self.assertEqual(call["expected_revision"], 3)
        resumed = call["resumed_job"]
        self.assertEqual(resumed.revision, 4)
        self.assertIs(resumed.current_step, JobCurrentStep.POST_DEPLOY_VERIFICATION)
        self.assertIs(call["outbox"].task.command, WorkflowCommand.APPLY_COMPLETED)
        self.assertEqual(call["outbox"].task.expected_revision, 4)
        self.assertEqual(len(dispatcher.tasks), 1)

    def test_resolves_the_owner_from_storage_not_the_event(self) -> None:
        deployments = FakeDeployments(customer_id=None)
        with self.assertRaises(ApplyCompletionError):
            _service(deployments=deployments).record_completion(
                deployment_id=DEPLOYMENT_ID, run_id=RUN_ID
            )

    def test_a_redelivered_event_is_absorbed_without_a_second_task(self) -> None:
        dispatcher = FakeDispatcher()
        _service(
            reservations=FakeReservations(DuplicateJobError("already reserved")),
            dispatcher=dispatcher,
        ).record_completion(deployment_id=DEPLOYMENT_ID, run_id=RUN_ID)
        self.assertEqual(dispatcher.tasks, [])

    def test_a_terminal_job_is_not_revived(self) -> None:
        for status in (JobStatus.CANCELLED, JobStatus.COMPLETED):
            with self.assertRaises(ApplyCompletionError):
                _service(job=_job(status=status)).record_completion(
                    deployment_id=DEPLOYMENT_ID, run_id=RUN_ID
                )

    def test_a_missing_deployment_or_job_fails_closed(self) -> None:
        with self.assertRaises(ApplyCompletionError):
            _service(deployments=FakeDeployments(record=None)).record_completion(
                deployment_id=DEPLOYMENT_ID, run_id=RUN_ID
            )
        with self.assertRaises(ApplyCompletionError):
            _service(job=None).record_completion(deployment_id=DEPLOYMENT_ID, run_id=RUN_ID)


class ConditionalCheckFailed(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeTransactionClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    def transact_write_items(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {}


class CompletionStoreTest(unittest.TestCase):
    def _reserve(self, client: FakeTransactionClient) -> None:
        from apps.backend.jobs.outbox import WorkflowOutboxEntry
        from packages.contracts import WorkflowTask

        resumed = _job(revision=4)
        DynamoDbDeploymentCompletionStore(
            table_name="metadata", transaction_client=client
        ).reserve_completion_event(
            deployment_id=DEPLOYMENT_ID,
            run_id=RUN_ID,
            resumed_job=resumed,
            expected_revision=3,
            outbox=WorkflowOutboxEntry(
                customer_id=CUSTOMER_ID,
                job_id=JOB_ID,
                task=WorkflowTask(
                    job_id=JOB_ID,
                    expected_revision=4,
                    command=WorkflowCommand.APPLY_COMPLETED,
                ),
            ),
            reserved_at="2026-09-03T10:00:00Z",
        )

    def test_reserves_only_the_run_coordinate(self) -> None:
        client = FakeTransactionClient()
        self._reserve(client)
        event_put = client.calls[0]["TransactItems"][0]["Put"]
        item = event_put["Item"]
        self.assertEqual(item["SK"], {"S": f"DEPLOYMENT#{DEPLOYMENT_ID}#EVENT#{RUN_ID}"})
        self.assertEqual(item["status"], {"S": "PENDING_VERIFICATION"})
        self.assertEqual(item["run_id"], {"S": RUN_ID})
        # 검증되지 않은 주장은 예약 item에 들어가지 않는다.
        for absent in ("conclusion", "commit_sha", "plan_hash", "workflow_path"):
            self.assertNotIn(absent, item)
        self.assertEqual(event_put["ConditionExpression"], "attribute_not_exists(SK)")

    def test_bumps_the_job_only_from_the_expected_revision(self) -> None:
        client = FakeTransactionClient()
        self._reserve(client)
        job_put = client.calls[0]["TransactItems"][1]["Put"]
        self.assertEqual(job_put["ConditionExpression"], "#revision = :expected")
        self.assertEqual(job_put["ExpressionAttributeValues"][":expected"], {"N": "3"})
        self.assertEqual(job_put["Item"]["revision"], {"N": "4"})

    def test_a_duplicate_reservation_is_reported_as_duplicate(self) -> None:
        with self.assertRaises(DuplicateJobError):
            self._reserve(FakeTransactionClient(ConditionalCheckFailed()))

    def test_rejects_an_outbox_that_does_not_match_the_resumed_job(self) -> None:
        from apps.backend.jobs.outbox import WorkflowOutboxEntry
        from packages.contracts import WorkflowTask

        store = DynamoDbDeploymentCompletionStore(
            table_name="metadata", transaction_client=FakeTransactionClient()
        )
        with self.assertRaises(ValueError):
            store.reserve_completion_event(
                deployment_id=DEPLOYMENT_ID,
                run_id=RUN_ID,
                resumed_job=_job(revision=4),
                expected_revision=3,
                outbox=WorkflowOutboxEntry(
                    customer_id=CUSTOMER_ID,
                    job_id=JOB_ID,
                    task=WorkflowTask(
                        job_id=JOB_ID,
                        expected_revision=4,
                        command=WorkflowCommand.RUN_DEPLOYMENT,
                    ),
                ),
                reserved_at="2026-09-03T10:00:00Z",
            )


if __name__ == "__main__":
    unittest.main()
