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


class CommandRoutingWorkflowDispatcherTest(unittest.TestCase):
    def _routing(self):
        from apps.backend.jobs import CommandRoutingWorkflowDispatcher

        self.assessment = Client()
        self.remediation = Client()
        self.deployment = Client()
        return CommandRoutingWorkflowDispatcher(
            {
                WorkflowCommand.ASSESS_RESOURCE: SqsWorkflowDispatcher(
                    self.assessment, queue_url="https://sqs.example/assessment"
                ),
                WorkflowCommand.GENERATE_REMEDIATION: SqsRemediationWorkflowDispatcher(
                    self.remediation, queue_url="https://sqs.example/remediation"
                ),
                WorkflowCommand.SYNC_ACTUAL_STATE: SqsRemediationWorkflowDispatcher(
                    self.remediation, queue_url="https://sqs.example/remediation"
                ),
            }
        )

    def test_routes_each_command_to_its_own_queue(self) -> None:
        router = self._routing()
        router.dispatch(
            WorkflowTask(job_id="a", expected_revision=0, command=WorkflowCommand.ASSESS_RESOURCE)
        )
        router.dispatch(
            WorkflowTask(
                job_id="r", expected_revision=0, command=WorkflowCommand.GENERATE_REMEDIATION
            )
        )

        self.assertEqual(len(self.assessment.calls), 1)
        self.assertEqual(len(self.remediation.calls), 1)
        self.assertEqual(len(self.deployment.calls), 0)
        self.assertEqual(
            json.loads(self.remediation.calls[0]["MessageBody"])["command"],
            "GENERATE_REMEDIATION",
        )

    def test_unconfigured_command_fails_closed(self) -> None:
        router = self._routing()
        with self.assertRaisesRegex(ValueError, "no dispatcher configured"):
            router.dispatch(
                WorkflowTask(
                    job_id="d", expected_revision=0, command=WorkflowCommand.RUN_DEPLOYMENT
                )
            )

    def test_empty_dispatcher_map_is_rejected(self) -> None:
        from apps.backend.jobs import CommandRoutingWorkflowDispatcher

        with self.assertRaises(ValueError):
            CommandRoutingWorkflowDispatcher({})


if __name__ == "__main__":
    unittest.main()
