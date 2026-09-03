"""C Remediation Worker 결과의 A 저장 경계 테스트 (ADR-0018, ADR-0019 §4).

고정하는 불변식:
- 결과는 `REMEDIATION#{id}` item에 conditional update로 한 번만 채워지고, 재시도는 멱등 흡수된다.
- 저장 시점에 work scope를 다시 묶는다 — 다른 finding/customer/repository의 결과는 거부한다.
- 결과 종류는 저장된 decision의 action과 일치해야 한다.
"""

import unittest

from apps.backend.remediation.worker import RemediationWork
from apps.backend.repositories.ports import RepositoryError
from apps.backend.repositories.remediation_result import DynamoDbRemediationResultStore
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
    RemediationPatch,
    RemediationSyncTarget,
)

CUSTOMER_ID = "cust-001"
REPOSITORY_ID = "repo-001"
REMEDIATION_ID = "rem-001"
JOB_ID = "job-001"
FINDING_ID = "find-001"
COMMIT = "a" * 40


class ConditionalCheckFailed(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class Throttled(Exception):
    response = {"Error": {"Code": "ProvisionedThroughputExceededException"}}


class FakeTransactionClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    def transact_write_items(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {}


def _snapshot() -> IaCSnapshot:
    return IaCSnapshot(
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
    )


def _work(action: RemediationAction = RemediationAction.TERRAFORM_PATCH) -> RemediationWork:
    finding = Finding(
        finding_id=FINDING_ID,
        resource_id="bucket-1",
        rule_id="S3-PUBLIC-001",
        rule_version="2026-08-31",
        perspective=EvaluationPerspective.IAC,
        status=EvaluationStatus.FAIL,
        severity="HIGH",
        score=10.0,
        rationale="public access is enabled",
        evidence_references=("evidence-1",),
    )
    return RemediationWork(
        customer_id=CUSTOMER_ID,
        remediation_id=REMEDIATION_ID,
        job_id=JOB_ID,
        revision=0,
        context=RemediationContext(
            finding=finding,
            snapshot=_snapshot(),
            evidence_references=("evidence-1",),
            source_assessment_id="asm-001",
        ),
        decision=RemediationDecision(
            finding_id=FINDING_ID,
            resource_id="bucket-1",
            rule_id="S3-PUBLIC-001",
            rule_version="2026-08-31",
            perspective=EvaluationPerspective.IAC,
            action=action,
        ),
    )


def _patch(**overrides: object) -> RemediationPatch:
    values: dict[str, object] = {
        "finding_id": FINDING_ID,
        "base_commit_sha": COMMIT,
        "artifact": ArtifactReference(
            artifact_id="patch-1",
            artifact_type=ArtifactType.REMEDIATION_PATCH,
            content_sha256="c" * 64,
            customer_id=CUSTOMER_ID,
            repository_id=REPOSITORY_ID,
        ),
        "changed_paths": ("main.tf",),
    }
    values.update(overrides)
    return RemediationPatch(**values)  # type: ignore[arg-type]


def _sync_target(**overrides: object) -> RemediationSyncTarget:
    values: dict[str, object] = {
        "finding_id": FINDING_ID,
        "customer_id": CUSTOMER_ID,
        "repository_id": REPOSITORY_ID,
        "commit_sha": COMMIT,
    }
    values.update(overrides)
    return RemediationSyncTarget(**values)  # type: ignore[arg-type]


def _store(client: FakeTransactionClient) -> DynamoDbRemediationResultStore:
    return DynamoDbRemediationResultStore(table_name="metadata", transaction_client=client)


class RemediationResultStoreTest(unittest.TestCase):
    def test_writes_the_patch_onto_the_remediation_item_once(self) -> None:
        client = FakeTransactionClient()
        _store(client).put_result_if_absent(work=_work(), result=_patch())
        update = client.calls[0]["TransactItems"][0]["Update"]
        self.assertEqual(
            update["Key"],
            {
                "PK": {"S": f"CUSTOMER#{CUSTOMER_ID}"},
                "SK": {"S": f"REMEDIATION#{REMEDIATION_ID}"},
            },
        )
        self.assertEqual(
            update["ConditionExpression"],
            "attribute_exists(PK) AND attribute_not_exists(#result)",
        )
        stored = update["ExpressionAttributeValues"][":result"]["M"]
        self.assertEqual(stored["kind"], {"S": "TERRAFORM_PATCH"})
        self.assertEqual(stored["patch"]["M"]["finding_id"], {"S": FINDING_ID})

    def test_writes_the_sync_target_for_an_actual_sync_decision(self) -> None:
        client = FakeTransactionClient()
        _store(client).put_result_if_absent(
            work=_work(RemediationAction.ACTUAL_SYNC), result=_sync_target()
        )
        stored = client.calls[0]["TransactItems"][0]["Update"]["ExpressionAttributeValues"][
            ":result"
        ]["M"]
        self.assertEqual(stored["kind"], {"S": "ACTUAL_SYNC"})
        self.assertEqual(stored["sync_target"]["M"]["commit_sha"], {"S": COMMIT})

    def test_absorbs_a_retry_at_the_same_revision(self) -> None:
        client = FakeTransactionClient(ConditionalCheckFailed())
        _store(client).put_result_if_absent(work=_work(), result=_patch())
        self.assertEqual(len(client.calls), 1)

    def test_reports_a_non_conditional_failure(self) -> None:
        client = FakeTransactionClient(Throttled())
        with self.assertRaises(RepositoryError):
            _store(client).put_result_if_absent(work=_work(), result=_patch())

    def test_rejects_a_result_kind_that_contradicts_the_decision(self) -> None:
        with self.assertRaises(ValueError):
            _store(FakeTransactionClient()).put_result_if_absent(
                work=_work(RemediationAction.ACTUAL_SYNC), result=_patch()
            )
        with self.assertRaises(ValueError):
            _store(FakeTransactionClient()).put_result_if_absent(
                work=_work(), result=_sync_target()
            )

    def test_rejects_a_patch_outside_the_work(self) -> None:
        store = _store(FakeTransactionClient())
        foreign_artifact = ArtifactReference(
            artifact_id="patch-1",
            artifact_type=ArtifactType.REMEDIATION_PATCH,
            content_sha256="c" * 64,
            customer_id="cust-002",
            repository_id=REPOSITORY_ID,
        )
        for result in (
            _patch(finding_id="find-999"),
            _patch(base_commit_sha="d" * 40),
            _patch(artifact=foreign_artifact),
        ):
            with self.assertRaises(ValueError):
                store.put_result_if_absent(work=_work(), result=result)

    def test_rejects_a_sync_target_outside_the_work(self) -> None:
        store = _store(FakeTransactionClient())
        work = _work(RemediationAction.ACTUAL_SYNC)
        for result in (
            _sync_target(finding_id="find-999"),
            _sync_target(customer_id="cust-002"),
            _sync_target(repository_id="repo-999"),
            _sync_target(commit_sha="d" * 40),
        ):
            with self.assertRaises(ValueError):
                store.put_result_if_absent(work=work, result=result)

    def test_rejects_a_non_result_value(self) -> None:
        with self.assertRaises(TypeError):
            _store(FakeTransactionClient()).put_result_if_absent(work=_work(), result=object())


if __name__ == "__main__":
    unittest.main()
