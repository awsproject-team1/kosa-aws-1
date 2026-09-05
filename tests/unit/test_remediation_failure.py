"""A remediation that cannot succeed ends in a recorded FAILED state, not an eternal QUEUED.

라이브에서 실패한 조치는 SQS 재시도 뒤 DLQ로 가고 record는 QUEUED로 남았다 — 화면은 "Worker 결과
대기 중"을 영영 보였고, 재시도마다 Bedrock이 다른 patch를 내 branch·PR이 늘었다. 이 파일은
(1) 다시 보내도 같은 실패는 기록되고 소비되며, (2) 다음에 다를 수 있는 실패는 재시도되고,
(3) 모델이 블록을 빼먹은 patch는 한 번 되물어 고쳐지는 것을 고정한다.
"""

import json
import unittest
from collections.abc import Mapping

from agent.runtime import IaCDocument, MockGitHubTool
from agent.runtime.github_write_tool import GitHubWriteToolError
from agent.runtime.live_github_write_tool import github_failure
from apps.backend.jobs.models import Job
from apps.backend.remediation.bedrock import BedrockPatchError, BedrockPatchGenerator
from apps.backend.remediation.failure import (
    RemediationFailureRecorder,
    failure_code,
    is_terminal,
)
from apps.backend.remediation.patch_content import InMemoryPatchContentStore
from apps.backend.remediation.pull_request import PullRequestActionError
from apps.backend.remediation.runtime import run_tasks
from apps.backend.remediation.worker import (
    RemediationWorker,
    RemediationWorkerError,
    RemediationWorkNotFoundError,
)
from apps.backend.repositories.ports import RepositoryError
from packages.contracts import (
    ApiError,
    JobCurrentStep,
    JobStatus,
    ModelProfile,
    ModelProfileRole,
    RemediationAction,
    WorkflowCommand,
    WorkflowTask,
)
from tests.unit.test_remediation_bedrock import (
    INSECURE_MAIN_TF,
    REWRITTEN_MAIN_TF,
    SECURE_MAIN_TF,
    context,
    decision,
)
from tests.unit.test_remediation_runtime import JOB_ID, WorkRepository, _sqs_event, _work

PROFILE = ModelProfile(
    model_profile_id="remediation-nova-lite-v1",
    role=ModelProfileRole.REMEDIATION,
    region="us-east-1",
    model_id="amazon.nova-lite-v1:0",
    prompt_version="remediation/1",
    rubric_version="remediation-rubric/1",
    golden_dataset_version="remediation-golden/1",
)


class ClassificationTest(unittest.TestCase):
    def test_model_output_and_github_rejections_are_terminal(self) -> None:
        self.assertEqual(failure_code(BedrockPatchError("truncated")), "PATCH_GENERATION_FAILED")
        self.assertEqual(failure_code(PullRequestActionError("scope")), "PULL_REQUEST_FAILED")
        self.assertEqual(
            failure_code(
                github_failure(
                    "GitHub pull request creation failed", 422, {"message": "Validation Failed"}
                )
            ),
            "PULL_REQUEST_FAILED",
        )
        self.assertEqual(
            failure_code(RemediationWorkerError("mismatch")), "REMEDIATION_WORK_INVALID"
        )

    def test_network_and_server_side_github_failures_are_retried(self) -> None:
        self.assertIsNone(failure_code(GitHubWriteToolError("GitHub request failed")))
        self.assertIsNone(failure_code(github_failure("GitHub file commit failed", 502, None)))
        self.assertIsNone(failure_code(RepositoryError("read failed")))
        self.assertFalse(is_terminal(RuntimeError("boto")))

    def test_the_github_failure_carries_status_and_message(self) -> None:
        error = github_failure(
            "GitHub pull request creation failed",
            422,
            {
                "message": "Validation Failed",
                "errors": [{"message": "No commits between main and x"}],
            },
        )
        self.assertEqual(error.status, 422)  # type: ignore[attr-defined]
        self.assertIn("(422): Validation Failed", str(error))
        self.assertIn("No commits between main and x", str(error))


class FailureStore:
    def __init__(self) -> None:
        self.failures: list[tuple[str, str, str]] = []

    def put_result_if_absent(self, *, work, result) -> None:
        raise AssertionError("no result is written on failure")

    def put_pull_request_if_absent(self, *, work, pull_request) -> None:
        raise AssertionError("no pull request is written on failure")

    def put_failure_if_absent(self, *, work, code: str, reason: str) -> None:
        self.failures.append((work.remediation_id, code, reason))


class Jobs:
    def __init__(self, job: Job | None) -> None:
        self.job = job
        self.updated: list[Job] = []

    def get_job(self, customer_id: str, job_id: str) -> Job | None:
        return self.job

    def update_job(self, job: Job, *, expected_revision: int) -> None:
        self.updated.append(job)


def _job(status: JobStatus = JobStatus.QUEUED) -> Job:
    work = _work(RemediationAction.TERRAFORM_PATCH)
    return Job(
        job_id=JOB_ID,
        customer_id=work.customer_id,
        job_type="REMEDIATION",
        status=status,
        current_step=JobCurrentStep.GENERATE_REMEDIATION,
        requested_by="user",
        revision=0,
        remediation_id=work.remediation_id,
    )


class FailingPatchAction:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def generate(self, *, context, decision):
        self.calls += 1
        raise self.error


class NeverOpens:
    def open(self, *, context, patch):
        raise AssertionError("no pull request is opened when patch generation fails")


class RecorderTest(unittest.TestCase):
    def _run(self, error: Exception, job: Job | None | str = "default"):
        if job == "default":
            job = _job()
        store = FailureStore()
        jobs = Jobs(job)
        work = _work(RemediationAction.TERRAFORM_PATCH)
        action = FailingPatchAction(error)
        worker = RemediationWorker(
            work_repository=WorkRepository(work),
            patch_action=action,
            sync_action=object(),  # type: ignore[arg-type]
            result_store=store,
            pull_request_action=NeverOpens(),
        )
        recorder = RemediationFailureRecorder(
            work_repository=WorkRepository(work), result_store=store, jobs=jobs
        )
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=0, command=WorkflowCommand.GENERATE_REMEDIATION
        )
        return store, jobs, action, lambda: run_tasks(_sqs_event(task), worker, recorder)

    def test_a_terminal_failure_is_recorded_and_the_message_is_consumed(self) -> None:
        store, jobs, action, run = self._run(
            BedrockPatchError("model output was truncated (max_tokens)")
        )

        run()  # 예외가 올라오지 않는다 — SQS가 재시도하지 않는다.

        self.assertEqual(action.calls, 1)
        (remediation_id, code, reason) = store.failures[0]
        self.assertEqual(code, "PATCH_GENERATION_FAILED")
        self.assertIn("truncated", reason)
        (failed,) = jobs.updated
        self.assertIs(failed.status, JobStatus.FAILED)
        self.assertEqual(
            failed.error, ApiError(code="PATCH_GENERATION_FAILED", message=reason[:200])
        )
        self.assertEqual(failed.revision, 1)

    def test_a_transient_failure_still_raises_so_sqs_retries(self) -> None:
        store, jobs, _action, run = self._run(RepositoryError("dynamodb unavailable"))

        with self.assertRaises(RepositoryError):
            run()

        self.assertEqual(store.failures, [])
        self.assertEqual(jobs.updated, [])

    def test_a_settled_job_is_not_flipped_back_to_failed(self) -> None:
        store, jobs, _action, run = self._run(BedrockPatchError("x"), job=_job(JobStatus.COMPLETED))

        run()

        self.assertEqual(len(store.failures), 1)
        self.assertEqual(jobs.updated, [])

    def test_without_a_recorder_every_failure_still_raises(self) -> None:
        """예전 동작. recorder를 주지 않은 호출자는 아무것도 잃지 않는다."""
        store = FailureStore()
        work = _work(RemediationAction.TERRAFORM_PATCH)
        worker = RemediationWorker(
            work_repository=WorkRepository(work),
            patch_action=FailingPatchAction(BedrockPatchError("x")),
            sync_action=object(),  # type: ignore[arg-type]
            result_store=store,
            pull_request_action=NeverOpens(),
        )
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=0, command=WorkflowCommand.GENERATE_REMEDIATION
        )
        with self.assertRaises(BedrockPatchError):
            run_tasks(_sqs_event(task), worker)

    def test_a_stale_task_is_dropped_without_a_record(self) -> None:
        class NoWork:
            def get_work(self, *, job_id, expected_revision):
                return None

        store = FailureStore()
        recorder = RemediationFailureRecorder(
            work_repository=NoWork(), result_store=store, jobs=Jobs(None)
        )
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=3, command=WorkflowCommand.GENERATE_REMEDIATION
        )

        self.assertFalse(recorder.record(task, RemediationWorkNotFoundError("stale")))
        self.assertEqual(store.failures, [])


class ScriptedClient:
    """Answers the given bodies in order, recording every request."""

    def __init__(self, bodies: list[object], stop_reasons: list[str | None] | None = None) -> None:
        self.bodies = list(bodies)
        self.stop_reasons = list(stop_reasons or [])
        self.calls: list[dict[str, object]] = []

    def converse(self, **kwargs: object) -> Mapping[str, object]:
        self.calls.append(kwargs)
        body = self.bodies.pop(0)
        response: dict[str, object] = {
            "output": {"message": {"content": [{"text": json.dumps(body)}]}}
        }
        if self.stop_reasons:
            reason = self.stop_reasons.pop(0)
            if reason:
                response["stopReason"] = reason
        return response


def _generator(client: ScriptedClient) -> BedrockPatchGenerator:
    documents = MockGitHubTool(
        customer_id="kosa-sandbox",
        repository_id="test-s3-sandbox",
        snapshots=(),
        documents=(
            IaCDocument(
                customer_id="kosa-sandbox",
                repository_id="test-s3-sandbox",
                commit_sha="b283b6b5a41945349f64c41036870a5507c264f7",
                files=(("main.tf", INSECURE_MAIN_TF),),
            ),
        ),
    )
    return BedrockPatchGenerator(
        client=client,  # type: ignore[arg-type]
        model_profile=PROFILE,
        content_store=InMemoryPatchContentStore(),
        iac_documents=documents,
    )


class PatchRepairTest(unittest.TestCase):
    def test_a_rewrite_that_dropped_blocks_is_re_asked_with_the_blocks_named(self) -> None:
        client = ScriptedClient(
            [{"changes": {"main.tf": REWRITTEN_MAIN_TF}}, {"changes": {"main.tf": SECURE_MAIN_TF}}]
        )

        patch = _generator(client).generate(context=context(), decision=decision())

        self.assertEqual(patch.changed_paths, ("main.tf",))
        self.assertEqual(len(client.calls), 2)
        second = json.loads(client.calls[1]["messages"][0]["content"][0]["text"])  # type: ignore[index]
        self.assertEqual(
            second["repair_hint"],
            {"must_keep_resource_blocks": {"main.tf": ["aws_s3_bucket.sandbox"]}},
        )
        first = json.loads(client.calls[0]["messages"][0]["content"][0]["text"])  # type: ignore[index]
        self.assertNotIn("repair_hint", first)

    def test_a_second_dropped_block_answer_fails_the_remediation(self) -> None:
        client = ScriptedClient(
            [
                {"changes": {"main.tf": REWRITTEN_MAIN_TF}},
                {"changes": {"main.tf": REWRITTEN_MAIN_TF}},
            ]
        )

        with self.assertRaisesRegex(BedrockPatchError, "removes or renames resource blocks"):
            _generator(client).generate(context=context(), decision=decision())
        self.assertEqual(len(client.calls), 2)

    def test_a_truncated_answer_is_named_and_not_re_asked(self) -> None:
        client = ScriptedClient(
            [{"changes": {"main.tf": REWRITTEN_MAIN_TF}}], stop_reasons=["max_tokens"]
        )

        with self.assertRaisesRegex(BedrockPatchError, "truncated"):
            _generator(client).generate(context=context(), decision=decision())
        self.assertEqual(len(client.calls), 1)

    def test_an_unrelated_file_is_refused_without_a_repair(self) -> None:
        client = ScriptedClient([{"changes": {"other.tf": SECURE_MAIN_TF}}])

        with self.assertRaisesRegex(BedrockPatchError, "not a Terraform file"):
            _generator(client).generate(context=context(), decision=decision())
        self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
