"""Transactional outbox delivery tests."""

import unittest

from apps.backend.jobs import OutboxDispatcher, WorkflowOutboxEntry
from packages.contracts import WorkflowCommand, WorkflowTask


def entry() -> WorkflowOutboxEntry:
    return WorkflowOutboxEntry(
        customer_id="cust-001",
        job_id="job-001",
        task=WorkflowTask(
            job_id="job-001", expected_revision=0, command=WorkflowCommand.ASSESS_RESOURCE
        ),
    )


class Repository:
    def __init__(self) -> None:
        self.pending = [entry()]
        self.dispatched = []
        self.failures = []

    def list_pending_outbox(self, *, limit: int):
        return tuple(self.pending[:limit])

    def mark_outbox_dispatched(self, item) -> None:
        self.dispatched.append(item)
        self.pending.remove(item)

    def record_outbox_dispatch_failure(self, item) -> None:
        self.failures.append(item)


class Dispatcher:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.tasks = []

    def dispatch(self, task) -> None:
        if self.fail:
            raise RuntimeError("SQS unavailable")
        self.tasks.append(task)


class OutboxDispatcherTest(unittest.TestCase):
    def test_success_marks_entry_dispatched(self) -> None:
        repository = Repository()
        dispatcher = Dispatcher()

        self.assertEqual(
            OutboxDispatcher(repository=repository, dispatcher=dispatcher).dispatch_pending(), 1
        )
        self.assertEqual(len(repository.dispatched), 1)
        self.assertEqual(len(repository.pending), 0)

    def test_dispatch_failure_leaves_entry_pending_for_a_later_sweeper_run(self) -> None:
        repository = Repository()

        self.assertEqual(
            OutboxDispatcher(
                repository=repository, dispatcher=Dispatcher(fail=True)
            ).dispatch_pending(),
            0,
        )
        self.assertEqual(len(repository.pending), 1)
        self.assertEqual(repository.failures, [entry()])


if __name__ == "__main__":
    unittest.main()
