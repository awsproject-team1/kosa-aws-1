"""C Remediation Worker composition root 테스트 (ADR-0013, ADR-0018).

고정하는 불변식:
- 이 큐는 두 remediation command만 받는다. 다른 큐의 command가 흘러들면 파싱에서 막는다.
- `ACTUAL_SYNC` 대상은 평가된 snapshot commit이다 — 지금의 default branch head가 아니다.
- `TERRAFORM_PATCH`는 실제 생성기가 없으므로 fixture로 조용히 대체되지 않고 막힌다.
- 설정 누락은 이름을 밝히는 runtime 오류로 fail-closed한다.
"""

import json
import os
import unittest
from unittest import mock

from apps.backend.remediation.runtime import (
    PatchGenerationUnavailableError,
    RemediationRuntimeError,
    UnavailablePatchAction,
    lambda_handler,
    parse_tasks,
    run_tasks,
)
from apps.backend.remediation.sync import SnapshotSyncAction, SyncActionError
from apps.backend.remediation.worker import RemediationWork, RemediationWorker
from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    EvaluationPerspective,
    EvaluationStatus,
    Finding,
    IaCSnapshot,
    RemediationAction,
    RemediationContext,
    RemediationDecision,
    RemediationSyncTarget,
    WorkflowCommand,
    WorkflowTask,
)

CUSTOMER_ID = "cust-001"
REPOSITORY_ID = "repo-001"
FINDING_ID = "find-001"
COMMIT = "a" * 40
JOB_ID = "job-001"


def _context() -> RemediationContext:
    return RemediationContext(
        finding=Finding(
            finding_id=FINDING_ID,
            resource_id="bucket-public-001",
            rule_id="S3-PUBLIC-001",
            rule_version="2026-08-31",
            perspective=EvaluationPerspective.AWS_ACTUAL,
            status=EvaluationStatus.FAIL,
            severity="HIGH",
            score=20.0,
            rationale="drifted",
            evidence_references=("aws:s3:fixture",),
        ),
        snapshot=IaCSnapshot(
            customer_id=CUSTOMER_ID,
            repository_id=REPOSITORY_ID,
            commit_sha=COMMIT,
            artifact=ArtifactReference(
                artifact_id="snap-1",
                artifact_type=ArtifactType.TERRAFORM_SNAPSHOT,
                content_sha256="b" * 64,
                customer_id=CUSTOMER_ID,
                repository_id=REPOSITORY_ID,
            ),
        ),
        evidence_references=("aws:s3:fixture",),
        source_assessment_id="asm-001",
    )


def _decision(action: RemediationAction = RemediationAction.ACTUAL_SYNC) -> RemediationDecision:
    return RemediationDecision(
        finding_id=FINDING_ID,
        resource_id="bucket-public-001",
        rule_id="S3-PUBLIC-001",
        rule_version="2026-08-31",
        perspective=EvaluationPerspective.AWS_ACTUAL,
        action=action,
    )


def _work(action: RemediationAction = RemediationAction.ACTUAL_SYNC) -> RemediationWork:
    return RemediationWork(
        customer_id=CUSTOMER_ID,
        remediation_id="rem-001",
        job_id=JOB_ID,
        revision=0,
        context=_context(),
        decision=_decision(action),
    )


class WorkRepository:
    def __init__(self, work: RemediationWork) -> None:
        self.work = work

    def get_work(self, *, job_id: str, expected_revision: int):
        return self.work


class ResultStore:
    def __init__(self) -> None:
        self.results: list[object] = []

    def put_result_if_absent(self, *, work, result) -> None:
        self.results.append(result)


def _sqs_event(*tasks: WorkflowTask) -> dict[str, object]:
    return {"Records": [{"body": json.dumps(task.to_dict())} for task in tasks]}


class ParseTasksTest(unittest.TestCase):
    def test_parses_the_two_remediation_commands(self) -> None:
        event = _sqs_event(
            WorkflowTask(
                job_id="j1", expected_revision=0, command=WorkflowCommand.GENERATE_REMEDIATION
            ),
            WorkflowTask(
                job_id="j2", expected_revision=1, command=WorkflowCommand.SYNC_ACTUAL_STATE
            ),
        )
        tasks = parse_tasks(event)
        self.assertEqual(len(tasks), 2)
        self.assertIs(tasks[0].command, WorkflowCommand.GENERATE_REMEDIATION)
        self.assertEqual(tasks[1].expected_revision, 1)

    def test_rejects_a_command_from_another_queue(self) -> None:
        """큐를 잘못 지목한 것은 재시도로 나아지지 않는다."""
        event = _sqs_event(
            WorkflowTask(job_id="j1", expected_revision=0, command=WorkflowCommand.RUN_DEPLOYMENT)
        )
        with self.assertRaises(ValueError):
            parse_tasks(event)

    def test_rejects_a_malformed_event(self) -> None:
        for event in ({}, {"Records": "nope"}, {"Records": [{"body": 1}]}):
            with self.assertRaises(ValueError):
                parse_tasks(event)


class RunTasksTest(unittest.TestCase):
    def _worker(self, action: RemediationAction) -> tuple[RemediationWorker, ResultStore]:
        store = ResultStore()
        worker = RemediationWorker(
            work_repository=WorkRepository(_work(action)),
            patch_action=UnavailablePatchAction(),
            sync_action=SnapshotSyncAction(),
            result_store=store,
        )
        return worker, store

    def test_drives_the_sync_path_end_to_end(self) -> None:
        worker, store = self._worker(RemediationAction.ACTUAL_SYNC)
        run_tasks(
            _sqs_event(
                WorkflowTask(
                    job_id=JOB_ID,
                    expected_revision=0,
                    command=WorkflowCommand.SYNC_ACTUAL_STATE,
                )
            ),
            worker,
        )
        result = store.results[0]
        self.assertIsInstance(result, RemediationSyncTarget)
        self.assertEqual(result.commit_sha, COMMIT)
        self.assertEqual(result.repository_id, REPOSITORY_ID)

    def test_the_patch_path_is_blocked_not_faked(self) -> None:
        worker, store = self._worker(RemediationAction.TERRAFORM_PATCH)
        with self.assertRaises(PatchGenerationUnavailableError):
            run_tasks(
                _sqs_event(
                    WorkflowTask(
                        job_id=JOB_ID,
                        expected_revision=0,
                        command=WorkflowCommand.GENERATE_REMEDIATION,
                    )
                ),
                worker,
            )
        self.assertEqual(store.results, [])

    def test_rejects_a_non_worker(self) -> None:
        with self.assertRaises(TypeError):
            run_tasks(_sqs_event(), object())


class SnapshotSyncActionTest(unittest.TestCase):
    def test_targets_the_assessed_snapshot_commit(self) -> None:
        """지금의 default branch head를 읽으면 평가 이후 merge된 변경까지 대상이 된다."""
        target = SnapshotSyncAction().prepare(context=_context(), decision=_decision())
        self.assertEqual(target.commit_sha, COMMIT)
        self.assertEqual(target.customer_id, CUSTOMER_ID)
        self.assertEqual(target.finding_id, FINDING_ID)

    def test_refuses_a_decision_that_did_not_authorize_a_sync(self) -> None:
        with self.assertRaises(SyncActionError):
            SnapshotSyncAction().prepare(
                context=_context(), decision=_decision(RemediationAction.TERRAFORM_PATCH)
            )


class LambdaHandlerTest(unittest.TestCase):
    def test_missing_configuration_names_the_variable(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RemediationRuntimeError) as raised:
                lambda_handler({"Records": []}, None)
        self.assertIn("METADATA_TABLE_NAME", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
