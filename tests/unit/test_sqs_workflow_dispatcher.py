"""Unit tests for the internal SQS WorkflowTask dispatcher."""

import json
import unittest

from apps.backend.jobs import SqsWorkflowDispatcher
from packages.contracts import WorkflowCommand, WorkflowTask


class Client:
    def __init__(self) -> None:
        self.calls = []

    def send_message(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


class SqsWorkflowDispatcherTest(unittest.TestCase):
    def test_serializes_only_the_minimal_assessment_task(self) -> None:
        client = Client()
        SqsWorkflowDispatcher(client, queue_url="https://sqs.example/assessment").dispatch(
            WorkflowTask(
                job_id="job-001", expected_revision=0, command=WorkflowCommand.ASSESS_RESOURCE
            )
        )

        self.assertEqual(
            json.loads(client.calls[0]["MessageBody"]),
            {"job_id": "job-001", "expected_revision": 0, "command": "ASSESS_RESOURCE"},
        )

    def test_rejects_a_task_for_another_queue(self) -> None:
        with self.assertRaisesRegex(ValueError, "only accepts"):
            SqsWorkflowDispatcher(Client(), queue_url="https://sqs.example/assessment").dispatch(
                WorkflowTask(
                    job_id="job-001",
                    expected_revision=0,
                    command=WorkflowCommand.GENERATE_REMEDIATION,
                )
            )
