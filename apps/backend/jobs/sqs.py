"""SQS adapters for durable Workflow Outbox entries."""

import json
from typing import Protocol

from packages.contracts import WorkflowCommand, WorkflowTask


class SqsClient(Protocol):
    def send_message(self, **kwargs: object) -> object: ...


class SqsWorkflowDispatcher:
    """Publish only Assessment tasks to one injected internal queue."""

    def __init__(self, client: SqsClient, *, queue_url: str) -> None:
        _require_configuration(client, queue_url)
        self._client = client
        self._queue_url = queue_url

    def dispatch(self, task: WorkflowTask) -> None:
        _require_task(task)
        if task.command is not WorkflowCommand.ASSESS_RESOURCE:
            raise ValueError("assessment dispatcher only accepts ASSESS_RESOURCE tasks")
        self._send(task)

    def _send(self, task: WorkflowTask) -> None:
        self._client.send_message(
            QueueUrl=self._queue_url,
            MessageBody=json.dumps(task.to_dict(), separators=(",", ":")),
        )


class SqsRemediationWorkflowDispatcher:
    """Publish only C Remediation Worker commands to its queue."""

    _ALLOWED_COMMANDS = frozenset(
        {WorkflowCommand.GENERATE_REMEDIATION, WorkflowCommand.SYNC_ACTUAL_STATE}
    )

    def __init__(self, client: SqsClient, *, queue_url: str) -> None:
        _require_configuration(client, queue_url)
        self._client = client
        self._queue_url = queue_url

    def dispatch(self, task: WorkflowTask) -> None:
        _require_task(task)
        if task.command not in self._ALLOWED_COMMANDS:
            raise ValueError("remediation dispatcher only accepts remediation tasks")
        self._client.send_message(
            QueueUrl=self._queue_url,
            MessageBody=json.dumps(task.to_dict(), separators=(",", ":")),
        )


def _require_configuration(client: SqsClient, queue_url: str) -> None:
    if client is None:
        raise TypeError("client is required")
    if not isinstance(queue_url, str) or not queue_url.strip():
        raise ValueError("queue_url must be a non-empty string")


def _require_task(task: WorkflowTask) -> None:
    if not isinstance(task, WorkflowTask):
        raise TypeError("task must be a WorkflowTask")
