"""Unit tests for queue-specific SQS WorkflowTask dispatchers."""

import json
import unittest

from apps.backend.jobs import SqsRemediationWorkflowDispatcher, SqsWorkflowDispatcher
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

    def test_assessment_dispatcher_rejects_a_remediation_task(self) -> None:
        with self.assertRaisesRegex(ValueError, "only accepts"):
            SqsWorkflowDispatcher(Client(), queue_url="https://sqs.example/assessment").dispatch(
                WorkflowTask(
                    job_id="job-001",
                    expected_revision=0,
                    command=WorkflowCommand.GENERATE_REMEDIATION,
                )
            )

    def test_remediation_dispatcher_accepts_both_c_commands(self) -> None:
        client = Client()
        dispatcher = SqsRemediationWorkflowDispatcher(
            client, queue_url="https://sqs.example/remediation"
        )
        for command in (
            WorkflowCommand.GENERATE_REMEDIATION,
            WorkflowCommand.SYNC_ACTUAL_STATE,
        ):
            dispatcher.dispatch(
                WorkflowTask(job_id="job-001", expected_revision=0, command=command)
            )

        self.assertEqual(
            [json.loads(call["MessageBody"])["command"] for call in client.calls],
            ["GENERATE_REMEDIATION", "SYNC_ACTUAL_STATE"],
        )

    def test_remediation_dispatcher_rejects_assessment_and_deployment_tasks(self) -> None:
        dispatcher = SqsRemediationWorkflowDispatcher(
            Client(), queue_url="https://sqs.example/remediation"
        )
        for command in (WorkflowCommand.ASSESS_RESOURCE, WorkflowCommand.RUN_DEPLOYMENT):
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, "only accepts"):
                dispatcher.dispatch(
                    WorkflowTask(job_id="job-001", expected_revision=0, command=command)
                )


if __name__ == "__main__":
    unittest.main()
