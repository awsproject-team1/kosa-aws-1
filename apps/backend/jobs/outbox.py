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
            try:
                self._dispatcher.dispatch(entry.task)
                self._repository.mark_outbox_dispatched(entry)
            except Exception:
                # Preserve PENDING on every failure. A later sweeper invocation retries it.
                self._repository.record_outbox_dispatch_failure(entry)
            else:
                dispatched += 1
        return dispatched
