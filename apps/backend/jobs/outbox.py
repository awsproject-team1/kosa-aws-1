"""Transactional outbox for durable, at-least-once workflow dispatch."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from packages.contracts import WorkflowTask


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkflowOutboxEntry:
    """One durable request to publish an already-persisted workflow task."""

    customer_id: str
    job_id: str
    task: WorkflowTask
    status: OutboxStatus = OutboxStatus.PENDING
    dispatch_attempts: int = 0

    def __post_init__(self) -> None:
        for name in ("customer_id", "job_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.task.job_id != self.job_id:
            raise ValueError("task job_id must match outbox job_id")
        if not isinstance(self.status, OutboxStatus):
            raise TypeError("status must be an OutboxStatus")
        if isinstance(self.dispatch_attempts, bool) or not isinstance(self.dispatch_attempts, int):
            raise TypeError("dispatch_attempts must be an integer")
        if self.dispatch_attempts < 0:
            raise ValueError("dispatch_attempts must be non-negative")


class WorkflowDispatcher(Protocol):
    """Publish a minimal workflow task to the selected internal queue."""

    def dispatch(self, task: WorkflowTask) -> None: ...


class OutboxRepository(Protocol):
    """Durable outbox operations used by a scheduled or worker-triggered sweeper."""

    def list_pending_outbox(self, *, limit: int) -> tuple[WorkflowOutboxEntry, ...]: ...

    def mark_outbox_dispatched(self, entry: WorkflowOutboxEntry) -> None: ...

    def record_outbox_dispatch_failure(self, entry: WorkflowOutboxEntry) -> None: ...


class OutboxDispatcher:
    """Deliver pending entries; duplicate delivery is safe via WorkflowTask revision checks."""

    def __init__(self, *, repository: OutboxRepository, dispatcher: WorkflowDispatcher) -> None:
        self._repository = repository
        self._dispatcher = dispatcher

    def dispatch_pending(self, *, limit: int = 100) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        dispatched = 0
        for entry in self._repository.list_pending_outbox(limit=limit):
            if self.dispatch_entry(entry):
                dispatched += 1
        return dispatched

    def dispatch_entry(self, entry: WorkflowOutboxEntry) -> bool:
        """Attempt one durable dispatch without surfacing queue failures to the caller.

        The entry is already committed before this method runs. A failed publish or
        status update therefore remains recoverable by the scheduled sweeper; a
        duplicate publish is safe because workers enforce the task revision.
        """
        if not isinstance(entry, WorkflowOutboxEntry):
            raise TypeError("entry must be a WorkflowOutboxEntry")
        try:
            self._dispatcher.dispatch(entry.task)
            self._repository.mark_outbox_dispatched(entry)
        except Exception:
            # Keep the already-persisted entry retryable. A failed bookkeeping
            # update is also non-fatal here: its PENDING state remains the source
            # of truth, and any later sweeper run can retry delivery.
            try:
                self._repository.record_outbox_dispatch_failure(entry)
            except Exception:
                pass
            return False
        return True
