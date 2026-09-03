"""D Deployment Worker composition root의 SQS 파싱·구동·mode 분기 테스트 (ADR-0019)."""

import json
import os
import unittest
from unittest import mock

from apps.backend.deployment.runtime import (
    DeploymentRuntimeError,
    LivePlanUnavailableError,
    _live_plan_outputs_fetcher,
    _live_worker,
    lambda_handler,
    parse_tasks,
    run_tasks,
)
from apps.backend.deployment.worker import DeploymentWorker
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

    def test_missing_metadata_table_fails_closed_before_any_aws_client(self) -> None:
        """설정 누락은 boto3 오류가 아니라 이름을 밝히는 runtime 오류로 멈춘다.

        `_metadata_table()`이 `_required_env()`보다 먼저 평가되면 누락된 환경 변수가
        boto3의 `NoRegionError` 같은 다른 실패로 가려진다. 순서를 회귀로 고정한다.
        """
        target = json.dumps([_TARGET])
        with mock.patch.dict(os.environ, {"DEPLOYMENT_RUNTIME_JSON": target}, clear=True):
            with self.assertRaises(DeploymentRuntimeError) as raised:
                lambda_handler({"Records": []}, None)
        self.assertIn("METADATA_TABLE_NAME", str(raised.exception))

    def test_live_plan_outputs_fetcher_is_blocked_pending_sandbox(self) -> None:
        """검증되지 않은 GitHub plan run I/O는 조용히 실행되지 않고 명시적으로 막힌다."""
        fetch = _live_plan_outputs_fetcher()
        with self.assertRaises(LivePlanUnavailableError):
            fetch(object(), "dep-001", COMMIT)


_TARGET = {
    "customer_id": "cust-001",
    "repository_id": "repo-001",
    "repository_full_name": "customer/iac",
    "github_token_secret_id": "github-token",
    "aws_account_id": "123456789012",
    "aws_read_role_arn": "arn:aws:iam::123456789012:role/Read",
    "aws_external_id_secret_id": "external-id",
    "resource_types": ["AWS::S3::Bucket"],
}


def _assemble_worker(targets: list[dict[str, object]]) -> DeploymentWorker:
    """실제 I/O를 모두 fake로 주입해 조립 로직만 검증한다."""
    return _live_worker(
        json.dumps(targets),
        plan_outputs_fetcher=lambda target, deployment_id, commit_sha: None,
        table=object(),
        table_name="metadata",
        transaction_client=object(),
        secret_reader=lambda secret_id: "secret-value",
        sts_client=object(),
        s3_client_factory=lambda credentials: object(),
    )


class LiveWorkerAssemblyTest(unittest.TestCase):
    def test_assembles_a_worker_from_a_single_target(self) -> None:
        worker = _assemble_worker([_TARGET])
        self.assertIsInstance(worker, DeploymentWorker)

    def test_requires_exactly_one_target(self) -> None:
        second = {**_TARGET, "repository_id": "repo-002"}
        with self.assertRaises(DeploymentRuntimeError):
            _assemble_worker([_TARGET, second])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
