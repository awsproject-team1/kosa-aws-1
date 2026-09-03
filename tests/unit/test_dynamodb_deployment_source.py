"""Deployment 생성 입력 reader 테스트 (ADR-0019 §3·§4).

고정하는 불변식:
- decision과 worker 결과는 한 item에서 strongly-consistent get 한 번으로 읽는다.
- `ACTUAL_SYNC`의 대상은 sync target commit이고 도달 가능성이 그 값의 정의에 포함된다.
- `TERRAFORM_PATCH`의 대상은 base commit이 아니라 merge된 default branch commit이다.
- merge 전이면 도달 불가로 표시하고(생성은 호출자가 막는다) commit을 지어내지 않는다.
- 저장된 결과 종류가 decision과 어긋나면 fail-closed한다.
"""

import unittest

from apps.backend.repositories.deployment_source import (
    DynamoDbDeploymentSourceReader,
    RemediationNotFoundError,
)
from apps.backend.repositories.ports import StoredDataError
from packages.contracts import RemediationAction

CUSTOMER_ID = "cust-001"
REPOSITORY_ID = "repo-001"
REMEDIATION_ID = "rem-001"
FINDING_ID = "find-001"
BASE_COMMIT = "a" * 40
MERGE_COMMIT = "e" * 40


def _patch_result() -> dict[str, object]:
    return {
        "kind": "TERRAFORM_PATCH",
        "patch": {
            "finding_id": FINDING_ID,
            "base_commit_sha": BASE_COMMIT,
            "artifact": {
                "artifact_id": "patch-1",
                "artifact_type": "REMEDIATION_PATCH",
                "content_sha256": "c" * 64,
                "customer_id": CUSTOMER_ID,
                "repository_id": REPOSITORY_ID,
            },
            "changed_paths": ["main.tf"],
        },
    }


def _sync_result() -> dict[str, object]:
    return {
        "kind": "ACTUAL_SYNC",
        "sync_target": {
            "finding_id": FINDING_ID,
            "customer_id": CUSTOMER_ID,
            "repository_id": REPOSITORY_ID,
            "commit_sha": BASE_COMMIT,
        },
    }


def _item(
    *,
    action: str = "TERRAFORM_PATCH",
    result: object | None = None,
    customer_id: str = CUSTOMER_ID,
    source_assessment_id: object = "asm-001",
) -> dict[str, object]:
    context: dict[str, object] = {
        "finding": {"finding_id": FINDING_ID},
        "snapshot": {
            "customer_id": customer_id,
            "repository_id": REPOSITORY_ID,
            "commit_sha": BASE_COMMIT,
        },
    }
    if source_assessment_id is not None:
        context["source_assessment_id"] = source_assessment_id
    item: dict[str, object] = {
        "PK": f"CUSTOMER#{customer_id}",
        "SK": f"REMEDIATION#{REMEDIATION_ID}",
        "entity_type": "REMEDIATION",
        "customer_id": customer_id,
        "remediation_id": REMEDIATION_ID,
        "finding_id": FINDING_ID,
        "context": context,
        "decision": {"action": action},
    }
    if result is not None:
        item["result"] = result
    return item


class FakeTable:
    def __init__(self, item: object) -> None:
        self.item = item
        self.calls: list[dict[str, object]] = []

    def get_item(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {} if self.item is None else {"Item": self.item}


class FakeResolver:
    def __init__(self, commit: str | None) -> None:
        self.commit = commit
        self.calls: list[dict[str, object]] = []

    def resolve_default_branch_commit(self, *, customer_id, repository_id, patch):
        self.calls.append(
            {"customer_id": customer_id, "repository_id": repository_id, "patch": patch}
        )
        return self.commit


def _reader(item: object, commit: str | None = MERGE_COMMIT) -> DynamoDbDeploymentSourceReader:
    return DynamoDbDeploymentSourceReader(FakeTable(item), commits=FakeResolver(commit))


class DeploymentSourceReaderTest(unittest.TestCase):
    def test_reads_the_remediation_item_consistently(self) -> None:
        table = FakeTable(_item(result=_patch_result()))
        reader = DynamoDbDeploymentSourceReader(table, commits=FakeResolver(MERGE_COMMIT))
        reader.get_deployment_source(customer_id=CUSTOMER_ID, remediation_id=REMEDIATION_ID)
        self.assertIs(table.calls[0]["ConsistentRead"], True)
        self.assertEqual(
            table.calls[0]["Key"],
            {"PK": f"CUSTOMER#{CUSTOMER_ID}", "SK": f"REMEDIATION#{REMEDIATION_ID}"},
        )

    def test_patch_target_is_the_merge_commit_not_the_base_commit(self) -> None:
        source = _reader(_item(result=_patch_result())).get_deployment_source(
            customer_id=CUSTOMER_ID, remediation_id=REMEDIATION_ID
        )
        self.assertEqual(source.commit_sha, MERGE_COMMIT)
        self.assertNotEqual(source.commit_sha, BASE_COMMIT)
        self.assertTrue(source.commit_reachable_from_default_branch)
        self.assertTrue(source.has_worker_result)
        self.assertIs(source.action, RemediationAction.TERRAFORM_PATCH)
        self.assertEqual(source.source_assessment_id, "asm-001")

    def test_unmerged_patch_is_not_reachable_and_invents_no_commit(self) -> None:
        source = _reader(_item(result=_patch_result()), commit=None).get_deployment_source(
            customer_id=CUSTOMER_ID, remediation_id=REMEDIATION_ID
        )
        self.assertFalse(source.commit_reachable_from_default_branch)
        self.assertEqual(source.commit_sha, BASE_COMMIT)

    def test_sync_target_is_reachable_without_a_github_read(self) -> None:
        resolver = FakeResolver(MERGE_COMMIT)
        reader = DynamoDbDeploymentSourceReader(
            FakeTable(_item(action="ACTUAL_SYNC", result=_sync_result())), commits=resolver
        )
        source = reader.get_deployment_source(
            customer_id=CUSTOMER_ID, remediation_id=REMEDIATION_ID
        )
        self.assertEqual(source.commit_sha, BASE_COMMIT)
        self.assertTrue(source.commit_reachable_from_default_branch)
        self.assertEqual(resolver.calls, [])

    def test_missing_worker_result_is_reported_not_guessed(self) -> None:
        source = _reader(_item()).get_deployment_source(
            customer_id=CUSTOMER_ID, remediation_id=REMEDIATION_ID
        )
        self.assertFalse(source.has_worker_result)
        self.assertFalse(source.commit_reachable_from_default_branch)

    def test_non_actionable_decision_has_no_target(self) -> None:
        source = _reader(_item(action="MANUAL_REVIEW", result=None)).get_deployment_source(
            customer_id=CUSTOMER_ID, remediation_id=REMEDIATION_ID
        )
        self.assertIs(source.action, RemediationAction.MANUAL_REVIEW)
        self.assertFalse(source.commit_reachable_from_default_branch)

    def test_result_kind_must_match_the_decision(self) -> None:
        with self.assertRaises(StoredDataError):
            _reader(_item(action="ACTUAL_SYNC", result=_patch_result())).get_deployment_source(
                customer_id=CUSTOMER_ID, remediation_id=REMEDIATION_ID
            )
        with self.assertRaises(StoredDataError):
            _reader(_item(result=_sync_result())).get_deployment_source(
                customer_id=CUSTOMER_ID, remediation_id=REMEDIATION_ID
            )

    def test_missing_source_assessment_closes_the_path(self) -> None:
        """검증을 정확한 before-state에 묶을 수 없으면 Deployment를 만들지 않는다."""
        with self.assertRaises(StoredDataError):
            _reader(_item(result=_patch_result(), source_assessment_id=None)).get_deployment_source(
                customer_id=CUSTOMER_ID, remediation_id=REMEDIATION_ID
            )

    def test_rejects_a_stored_remediation_from_another_customer(self) -> None:
        with self.assertRaises(StoredDataError):
            _reader(_item(customer_id="cust-002")).get_deployment_source(
                customer_id=CUSTOMER_ID, remediation_id=REMEDIATION_ID
            )

    def test_missing_remediation_is_not_found(self) -> None:
        with self.assertRaises(RemediationNotFoundError):
            _reader(None).get_deployment_source(
                customer_id=CUSTOMER_ID, remediation_id=REMEDIATION_ID
            )

    def test_rejects_a_blank_resolved_commit(self) -> None:
        with self.assertRaises(StoredDataError):
            _reader(_item(result=_patch_result()), commit="   ").get_deployment_source(
                customer_id=CUSTOMER_ID, remediation_id=REMEDIATION_ID
            )


if __name__ == "__main__":
    unittest.main()
