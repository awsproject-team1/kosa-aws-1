"""C-owned remediation worker orchestration tests."""

import unittest

from apps.backend.remediation import (
    RemediationSyncTarget,
    RemediationWork,
    RemediationWorker,
    RemediationWorkerError,
    RemediationWorkNotFoundError,
)
from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    EvaluationPerspective,
    EvaluationStatus,
    Finding,
    IaCSnapshot,
    ManualReviewCode,
    RemediationAction,
    RemediationContext,
    RemediationDecision,
    RemediationPatch,
    WorkflowCommand,
    WorkflowTask,
)


def context() -> RemediationContext:
    finding = Finding(
        finding_id="finding-001",
        resource_id="bucket-001",
        rule_id="rule-001",
        rule_version="v1",
        perspective=EvaluationPerspective.DRIFT,
        status=EvaluationStatus.FAIL,
        severity="HIGH",
        score=0,
        rationale="unsafe",
        evidence_references=("aws:bucket-001",),
    )
    return RemediationContext(
        finding=finding,
        snapshot=IaCSnapshot(
            customer_id="cust-001",
            repository_id="repo-001",
            commit_sha="base-commit",
            artifact=ArtifactReference(
                artifact_id="snapshot-001",
                artifact_type=ArtifactType.TERRAFORM_SNAPSHOT,
                content_sha256="snapshot-hash",
                customer_id="cust-001",
                repository_id="repo-001",
            ),
        ),
        evidence_references=finding.evidence_references,
    )


def decision(action: RemediationAction) -> RemediationDecision:
    finding = context().finding
    return RemediationDecision(
        finding_id=finding.finding_id,
        resource_id=finding.resource_id,
        rule_id=finding.rule_id,
        rule_version=finding.rule_version,
        perspective=finding.perspective,
        action=action,
        manual_review_code=(
            ManualReviewCode.RULE_NOT_IN_SCOPE
            if action is RemediationAction.MANUAL_REVIEW
            else None
        ),
    )


def work(action: RemediationAction, *, revision: int = 2) -> RemediationWork:
    return RemediationWork(
        customer_id="cust-001",
        remediation_id="rem-001",
        job_id="job-001",
        revision=revision,
        context=context(),
        decision=decision(action),
    )


class WorkRepository:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def get_work(self, *, job_id, expected_revision):
        self.calls.append((job_id, expected_revision))
        return self.value


class PatchAction:
    def __init__(self):
        self.calls = []

    def generate(self, *, context, decision):
        self.calls.append((context, decision))
        return RemediationPatch(
            finding_id=context.finding.finding_id,
            base_commit_sha=context.snapshot.commit_sha,
            artifact=ArtifactReference(
                artifact_id="patch-001",
                artifact_type=ArtifactType.REMEDIATION_PATCH,
                content_sha256="patch-hash",
                customer_id=context.snapshot.customer_id,
                repository_id=context.snapshot.repository_id,
            ),
            changed_paths=("main.tf",),
        )


class SyncAction:
    def __init__(self):
        self.calls = []

    def prepare(self, *, context, decision):
        self.calls.append((context, decision))
        return RemediationSyncTarget(
            finding_id=context.finding.finding_id,
            customer_id=context.snapshot.customer_id,
            repository_id=context.snapshot.repository_id,
            commit_sha=context.snapshot.commit_sha,
        )


class ResultStore:
    def __init__(self):
        self.calls = []

    def put_result_if_absent(self, *, work, result):
        self.calls.append((work, result))


def task(command: WorkflowCommand) -> WorkflowTask:
    return WorkflowTask(job_id="job-001", expected_revision=2, command=command)


def worker(value):
    repository = WorkRepository(value)
    patch = PatchAction()
    sync = SyncAction()
    results = ResultStore()
    return (
        RemediationWorker(
            work_repository=repository,
            patch_action=patch,
            sync_action=sync,
            result_store=results,
        ),
        repository,
        patch,
        sync,
        results,
    )


class RemediationWorkerTest(unittest.TestCase):
    def test_patch_command_reloads_revision_and_calls_only_patch_port(self):
        subject, repository, patch, sync, results = worker(work(RemediationAction.TERRAFORM_PATCH))

        result = subject.handle(task(WorkflowCommand.GENERATE_REMEDIATION))

        self.assertIsInstance(result, RemediationPatch)
        self.assertEqual(repository.calls, [("job-001", 2)])
        self.assertEqual(len(patch.calls), 1)
        self.assertEqual(sync.calls, [])
        self.assertEqual(results.calls[0][1], result)

    def test_sync_command_calls_only_sync_port(self):
        subject, _, patch, sync, results = worker(work(RemediationAction.ACTUAL_SYNC))

        result = subject.handle(task(WorkflowCommand.SYNC_ACTUAL_STATE))

        self.assertIsInstance(result, RemediationSyncTarget)
        self.assertEqual(patch.calls, [])
        self.assertEqual(len(sync.calls), 1)
        self.assertEqual(results.calls[0][1], result)

    def test_missing_or_stale_work_fails_before_any_action(self):
        subject, _, patch, sync, results = worker(None)

        with self.assertRaises(RemediationWorkNotFoundError):
            subject.handle(task(WorkflowCommand.GENERATE_REMEDIATION))

        self.assertEqual(patch.calls, [])
        self.assertEqual(sync.calls, [])
        self.assertEqual(results.calls, [])

    def test_command_and_stored_action_must_match(self):
        subject, _, patch, sync, results = worker(work(RemediationAction.ACTUAL_SYNC))

        with self.assertRaisesRegex(RemediationWorkerError, "does not match"):
            subject.handle(task(WorkflowCommand.GENERATE_REMEDIATION))

        self.assertEqual(patch.calls, [])
        self.assertEqual(sync.calls, [])
        self.assertEqual(results.calls, [])

    def test_non_actionable_decision_can_never_reach_an_action_port(self):
        subject, _, patch, sync, _ = worker(work(RemediationAction.MANUAL_REVIEW))

        with self.assertRaises(RemediationWorkerError):
            subject.handle(task(WorkflowCommand.GENERATE_REMEDIATION))

        self.assertEqual(patch.calls, [])
        self.assertEqual(sync.calls, [])

    def test_deployment_command_is_not_a_c_remediation_command(self):
        subject, repository, patch, sync, _ = worker(work(RemediationAction.TERRAFORM_PATCH))

        with self.assertRaisesRegex(RemediationWorkerError, "unsupported"):
            subject.handle(task(WorkflowCommand.RUN_DEPLOYMENT))

        self.assertEqual(repository.calls, [])
        self.assertEqual(patch.calls, [])
        self.assertEqual(sync.calls, [])

    def test_repository_work_must_match_task_revision(self):
        subject, _, patch, sync, _ = worker(work(RemediationAction.TERRAFORM_PATCH, revision=1))

        with self.assertRaisesRegex(RemediationWorkerError, "does not match"):
            subject.handle(task(WorkflowCommand.GENERATE_REMEDIATION))

        self.assertEqual(patch.calls, [])
        self.assertEqual(sync.calls, [])


if __name__ == "__main__":
    unittest.main()
