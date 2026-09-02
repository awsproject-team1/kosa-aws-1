"""D Deployment Worker composition root의 SQS 파싱·구동·mode 분기 테스트 (ADR-0019)."""

import json
import os
import unittest
from unittest import mock

from apps.backend.deployment.runtime import (
    DeploymentRuntimeError,
    LivePlanUnavailableError,
    lambda_handler,
    parse_tasks,
    run_tasks,
)
from packages.contracts import WorkflowCommand, WorkflowTask
from tests.unit.test_deployment_worker import (
    COMMIT,
    JOB_ID,
    build_worker,
    plan_work,
)


def _sqs_event(*tasks: WorkflowTask) -> dict[str, object]:
    return {"Records": [{"body": json.dumps(task.to_dict())} for task in tasks]}


class ParseTasksTest(unittest.TestCase):
    def test_parses_three_deployment_commands(self) -> None:
        event = _sqs_event(
            WorkflowTask(job_id="j1", expected_revision=0, command=WorkflowCommand.RUN_DEPLOYMENT),
            WorkflowTask(job_id="j2", expected_revision=1, command=WorkflowCommand.PLAN_COMPLETED),
            WorkflowTask(job_id="j3", expected_revision=2, command=WorkflowCommand.APPLY_COMPLETED),
        )
        tasks = parse_tasks(event)
        self.assertEqual(len(tasks), 3)
        self.assertEqual(tasks[0].command, WorkflowCommand.RUN_DEPLOYMENT)
        self.assertEqual(tasks[2].expected_revision, 2)

    def test_rejects_a_missing_records_list(self) -> None:
        with self.assertRaises(ValueError):
            parse_tasks({})

    def test_rejects_a_non_string_body(self) -> None:
        with self.assertRaises(ValueError):
            parse_tasks({"Records": [{"body": 123}]})


class RunTasksTest(unittest.TestCase):
    def test_drives_run_deployment_through_the_worker(self) -> None:
        # RUN_DEPLOYMENT를 SQS 메시지로 넣으면 Worker가 plan을 요청해 store에 기록한다.
        worker, store, _apply, _actual = build_worker(work=plan_work())
        event = _sqs_event(
            WorkflowTask(job_id=JOB_ID, expected_revision=0, command=WorkflowCommand.RUN_DEPLOYMENT)
        )
        run_tasks(event, worker)
        self.assertEqual(len(store.plans), 1)
        self.assertEqual(store.plans[0].plan.commit_sha, COMMIT)

    def test_rejects_a_non_worker(self) -> None:
        with self.assertRaises(TypeError):
            run_tasks(_sqs_event(), object())


class LambdaHandlerModeTest(unittest.TestCase):
    def test_fails_closed_without_configuration(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(DeploymentRuntimeError):
                lambda_handler({"Records": []}, None)

    def test_live_mode_validates_config_then_stops_pending_7b(self) -> None:
        target = json.dumps(
            [
                {
                    "customer_id": "cust-001",
                    "repository_id": "repo-001",
                    "repository_full_name": "customer/iac",
                    "github_token_secret_id": "github-token",
                    "aws_account_id": "123456789012",
                    "aws_read_role_arn": "arn:aws:iam::123456789012:role/Read",
                    "aws_external_id_secret_id": "external-id",
                    "resource_types": ["AWS::S3::Bucket"],
                }
            ]
        )
        with mock.patch.dict(os.environ, {"DEPLOYMENT_RUNTIME_JSON": target}, clear=True):
            # 설정은 유효하므로 config 검증은 통과하고, live plan 어댑터 부재로 멈춘다(7-B).
            with self.assertRaises(LivePlanUnavailableError):
                lambda_handler({"Records": []}, None)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
