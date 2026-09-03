"""승인 입력 reader 테스트 (ADR-0019 §1 addendum, §8).

고정하는 불변식:
- readiness는 저장하지 않고 저장된 plan 요약 + Worker context에서 read 시 파생한다.
- destructive plan은 `MANUAL_REVIEW`, 매핑되지 않은 finding 리소스는 `BLOCKED`로 판정된다.
- plan facts가 없으면 승인할 것이 없다(파생하지 않고 not-ready로 닫는다).
- 다른 고객의 remediation context는 승인 판정의 근거가 되지 않는다.
"""

import unittest

from apps.backend.repositories.deployment import DynamoDbDeploymentRepository
from apps.backend.repositories.deployment_plan import (
    DeploymentPlanNotReadyError,
    DynamoDbDeploymentPlanReader,
)
from apps.backend.repositories.ports import StoredDataError
from packages.contracts import (
    DeploymentReadinessSignal,
)
from packages.contracts.remediation import DeploymentReadinessStatus

CUSTOMER_ID = "cust-001"
REPOSITORY_ID = "repo-001"
DEPLOYMENT_ID = "dep-001"
REMEDIATION_ID = "rem-001"
FINDING_ID = "find-001"
RESOURCE_ID = "bucket-public-001"
COMMIT = "a" * 40
PLAN_HASH = "b" * 64


def _plan_artifact() -> dict[str, object]:
    return {
        "artifact_id": "art-plan-001",
        "artifact_type": "TERRAFORM_PLAN",
        "content_sha256": PLAN_HASH,
        "customer_id": CUSTOMER_ID,
        "repository_id": REPOSITORY_ID,
    }


def _deployment_item(*, with_plan: bool = True, summary: dict[str, object] | None = None):
    item: dict[str, object] = {
        "PK": f"CUSTOMER#{CUSTOMER_ID}",
        "SK": f"DEPLOYMENT#{DEPLOYMENT_ID}",
        "entity_type": "DEPLOYMENT",
        "deployment_id": DEPLOYMENT_ID,
        "customer_id": CUSTOMER_ID,
        "repository_id": REPOSITORY_ID,
        "job_id": "job-001",
        "remediation_id": REMEDIATION_ID,
        "commit_sha": COMMIT,
        "source_assessment_id": "asm-001",
    }
    if with_plan:
        item.update(
            {
                "plan_hash": PLAN_HASH,
                "plan_artifact": _plan_artifact(),
                "binary_artifact": {
                    "artifact_id": "art-plan-bin-001",
                    "artifact_type": "TERRAFORM_PLAN_BINARY",
                    "content_sha256": "c" * 64,
                    "customer_id": CUSTOMER_ID,
                    "repository_id": REPOSITORY_ID,
                },
                "state_version": {"lineage": "lin-1", "serial": 3},
                "plan_summary": summary
                or {
                    "refreshed": True,
                    "has_destructive_changes": False,
                    "mapped_resource_ids": [RESOURCE_ID],
                },
            }
        )
    return item


def _remediation_item(*, customer_id: str = CUSTOMER_ID) -> dict[str, object]:
    return {
        "PK": f"CUSTOMER#{customer_id}",
        "SK": f"REMEDIATION#{REMEDIATION_ID}",
        "entity_type": "REMEDIATION",
        "customer_id": customer_id,
        "remediation_id": REMEDIATION_ID,
        "context": {
            "finding": {
                "finding_id": FINDING_ID,
                "resource_id": RESOURCE_ID,
                "rule_id": "S3-PUBLIC-001",
                "rule_version": "2026-08-31",
                "perspective": "AWS_ACTUAL",
                "status": "FAIL",
                "severity": "HIGH",
                "score": 20,
                "rationale": "public access is enabled",
                "evidence_references": ["aws:s3:fixture"],
            },
            "snapshot": {
                "customer_id": customer_id,
                "repository_id": REPOSITORY_ID,
                "commit_sha": COMMIT,
                "artifact": {
                    "artifact_id": "snap-1",
                    "artifact_type": "TERRAFORM_SNAPSHOT",
                    "content_sha256": "d" * 64,
                    "customer_id": customer_id,
                    "repository_id": REPOSITORY_ID,
                },
            },
            "evidence_references": ["aws:s3:fixture"],
            "source_assessment_id": "asm-001",
        },
    }


class FakeTable:
    def __init__(self, items: dict[str, object]) -> None:
        self.items = items

    def get_item(self, **kwargs: object) -> dict[str, object]:
        key = kwargs.get("Key")
        assert isinstance(key, dict)
        item = self.items.get(key["SK"])
        return {} if item is None else {"Item": item}


class Transactions:
    def transact_write_items(self, **kwargs: object) -> object:  # pragma: no cover - unused
        raise AssertionError("not used")


def _reader(items: dict[str, object]) -> DynamoDbDeploymentPlanReader:
    table = FakeTable(items)
    return DynamoDbDeploymentPlanReader(
        table,
        deployments=DynamoDbDeploymentRepository(
            table=table, table_name="metadata", transaction_client=Transactions()
        ),
    )


def _items(**overrides: object) -> dict[str, object]:
    items: dict[str, object] = {
        f"DEPLOYMENT#{DEPLOYMENT_ID}": _deployment_item(),
        f"REMEDIATION#{REMEDIATION_ID}": _remediation_item(),
    }
    items.update(overrides)
    return items


class DeploymentPlanReaderTest(unittest.TestCase):
    def test_returns_the_stored_plan_and_a_ready_verdict(self) -> None:
        plan, readiness = _reader(_items()).get_approval_input(
            customer_id=CUSTOMER_ID, deployment_id=DEPLOYMENT_ID
        )
        self.assertEqual(plan.deployment_id, DEPLOYMENT_ID)
        self.assertEqual(plan.plan_hash, PLAN_HASH)
        self.assertEqual(plan.commit_sha, COMMIT)
        self.assertIs(readiness.status, DeploymentReadinessStatus.READY_FOR_APPROVAL)
        self.assertEqual(readiness.finding_id, FINDING_ID)

    def test_a_destructive_plan_requires_manual_review(self) -> None:
        items = _items(
            **{
                f"DEPLOYMENT#{DEPLOYMENT_ID}": _deployment_item(
                    summary={
                        "refreshed": True,
                        "has_destructive_changes": True,
                        "mapped_resource_ids": [RESOURCE_ID],
                    }
                )
            }
        )
        _, readiness = _reader(items).get_approval_input(
            customer_id=CUSTOMER_ID, deployment_id=DEPLOYMENT_ID
        )
        self.assertIs(readiness.status, DeploymentReadinessStatus.MANUAL_REVIEW)
        self.assertIn("DESTRUCTIVE_CHANGE_REQUIRES_MANUAL_REVIEW", readiness.reason_codes)

    def test_an_unmapped_finding_resource_blocks_approval(self) -> None:
        """plan이 Finding의 리소스를 건드리지 않으면 그 plan은 이 Finding의 조치가 아니다."""
        items = _items(
            **{
                f"DEPLOYMENT#{DEPLOYMENT_ID}": _deployment_item(
                    summary={
                        "refreshed": True,
                        "has_destructive_changes": False,
                        "mapped_resource_ids": ["bucket-other-999"],
                    }
                )
            }
        )
        _, readiness = _reader(items).get_approval_input(
            customer_id=CUSTOMER_ID, deployment_id=DEPLOYMENT_ID
        )
        self.assertIs(readiness.status, DeploymentReadinessStatus.BLOCKED)
        self.assertIn("FINDING_RESOURCE_NOT_MAPPED", readiness.reason_codes)

    def test_an_unrefreshed_plan_blocks_approval(self) -> None:
        items = _items(
            **{
                f"DEPLOYMENT#{DEPLOYMENT_ID}": _deployment_item(
                    summary={
                        "refreshed": False,
                        "has_destructive_changes": False,
                        "mapped_resource_ids": [RESOURCE_ID],
                    }
                )
            }
        )
        _, readiness = _reader(items).get_approval_input(
            customer_id=CUSTOMER_ID, deployment_id=DEPLOYMENT_ID
        )
        self.assertIs(readiness.status, DeploymentReadinessStatus.BLOCKED)
        self.assertIn("PLAN_NOT_REFRESHED", readiness.reason_codes)

    def test_a_deployment_without_a_plan_has_nothing_to_approve(self) -> None:
        items = _items(**{f"DEPLOYMENT#{DEPLOYMENT_ID}": _deployment_item(with_plan=False)})
        with self.assertRaises(DeploymentPlanNotReadyError):
            _reader(items).get_approval_input(customer_id=CUSTOMER_ID, deployment_id=DEPLOYMENT_ID)

    def test_a_missing_deployment_is_not_ready(self) -> None:
        with self.assertRaises(DeploymentPlanNotReadyError):
            _reader({}).get_approval_input(customer_id=CUSTOMER_ID, deployment_id=DEPLOYMENT_ID)

    def test_a_foreign_remediation_context_is_refused(self) -> None:
        items = _items(
            **{f"REMEDIATION#{REMEDIATION_ID}": _remediation_item(customer_id="cust-002")}
        )
        with self.assertRaises(StoredDataError):
            _reader(items).get_approval_input(customer_id=CUSTOMER_ID, deployment_id=DEPLOYMENT_ID)

    def test_a_missing_remediation_context_fails_closed(self) -> None:
        items = {f"DEPLOYMENT#{DEPLOYMENT_ID}": _deployment_item()}
        with self.assertRaises(StoredDataError):
            _reader(items).get_approval_input(customer_id=CUSTOMER_ID, deployment_id=DEPLOYMENT_ID)


class DeploymentReadinessSignalTest(unittest.TestCase):
    def test_status_read_reuses_the_approval_verdict(self) -> None:
        signal = _reader(_items()).get_readiness_signal(
            customer_id=CUSTOMER_ID, deployment_id=DEPLOYMENT_ID
        )
        self.assertIs(signal, DeploymentReadinessSignal.READY_FOR_APPROVAL)

    def test_a_blocked_plan_is_reported_as_blocked_not_ready(self) -> None:
        items = _items(
            **{
                f"DEPLOYMENT#{DEPLOYMENT_ID}": _deployment_item(
                    summary={
                        "refreshed": False,
                        "has_destructive_changes": False,
                        "mapped_resource_ids": [RESOURCE_ID],
                    }
                )
            }
        )
        signal = _reader(items).get_readiness_signal(
            customer_id=CUSTOMER_ID, deployment_id=DEPLOYMENT_ID
        )
        self.assertIs(signal, DeploymentReadinessSignal.BLOCKED)

    def test_no_plan_yields_no_signal(self) -> None:
        items = _items(**{f"DEPLOYMENT#{DEPLOYMENT_ID}": _deployment_item(with_plan=False)})
        self.assertIsNone(
            _reader(items).get_readiness_signal(
                customer_id=CUSTOMER_ID, deployment_id=DEPLOYMENT_ID
            )
        )


if __name__ == "__main__":
    unittest.main()
