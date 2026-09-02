"""D Deployment Worker work repository 조립 테스트 (ADR-0019).

고정하는 불변식:
- Job은 GSI1(`JOB#{job_id}`)로 전역 조회하고, job_type/revision/job_id가 맞아야 한다.
- plan facts가 없는 생성 직후(RUN_DEPLOYMENT)에는 필수 필드만 채운다.
- plan facts·approval·plan_run이 채워지면 apply 단계에 필요한 optional을 조립한다.
- aws_account_id는 DeploymentRecord/Job에 없으므로 주입된 resolver로 채우고, 실패는 fail-closed.
- run_reference는 이 reader가 채우지 않는다(EventBridge 완료 Event 저장은 별도 조각).
"""

import unittest

from apps.backend.repositories.deployment_work import DynamoDbDeploymentWorkRepository
from apps.backend.repositories.ports import RepositoryError, StoredDataError
from tests.unit.test_deployment_worker import (
    AWS_ACCOUNT_ID,
    COMMIT,
    CUSTOMER_ID,
    DEPLOYMENT_ID,
    JOB_ID,
    LINEAGE,
    PLAN_HASH,
    PLAN_RUN_ID,
    REPOSITORY_ID,
    SERIAL,
)

REMEDIATION_ID = "rem-001"
FINDING_ID = "find-001"


def _job_item(*, revision: int = 1, job_type: str = "DEPLOYMENT") -> dict[str, object]:
    return {
        "PK": f"CUSTOMER#{CUSTOMER_ID}",
        "SK": f"JOB#{JOB_ID}",
        "entity_type": "JOB",
        "job_id": JOB_ID,
        "job_type": job_type,
        "customer_id": CUSTOMER_ID,
        "deployment_id": DEPLOYMENT_ID,
        "revision": revision,
        "GSI1PK": f"JOB#{JOB_ID}",
    }


def _deployment_item(*, with_plan: bool = False) -> dict[str, object]:
    item: dict[str, object] = {
        "PK": f"CUSTOMER#{CUSTOMER_ID}",
        "SK": f"DEPLOYMENT#{DEPLOYMENT_ID}",
        "entity_type": "DEPLOYMENT",
        "customer_id": CUSTOMER_ID,
        "deployment_id": DEPLOYMENT_ID,
        "repository_id": REPOSITORY_ID,
        "job_id": JOB_ID,
        "remediation_id": REMEDIATION_ID,
        "commit_sha": COMMIT,
        "source_assessment_id": "asm-001",
    }
    if with_plan:
        item.update(
            {
                "plan_hash": PLAN_HASH,
                "plan_artifact": {
                    "artifact_id": "art-plan-1",
                    "artifact_type": "TERRAFORM_PLAN",
                    "content_sha256": PLAN_HASH,
                    "customer_id": CUSTOMER_ID,
                    "repository_id": REPOSITORY_ID,
                },
                "binary_artifact": {
                    "artifact_id": "art-bin-1",
                    "artifact_type": "TERRAFORM_PLAN_BINARY",
                    "content_sha256": "b" * 64,
                    "customer_id": CUSTOMER_ID,
                    "repository_id": REPOSITORY_ID,
                },
                "state_version": {"lineage": LINEAGE, "serial": SERIAL},
                "plan_run": {
                    "deployment_id": DEPLOYMENT_ID,
                    "repository_id": REPOSITORY_ID,
                    "run_id": PLAN_RUN_ID,
                },
            }
        )
    return item


def _approval_item() -> dict[str, object]:
    return {
        "PK": f"CUSTOMER#{CUSTOMER_ID}",
        "SK": f"DEPLOYMENT#{DEPLOYMENT_ID}#APPROVAL#approval-{DEPLOYMENT_ID}",
        "entity_type": "DEPLOYMENT_APPROVAL",
        "deployment_id": DEPLOYMENT_ID,
        "approved_by": "admin-1",
        "commit_sha": COMMIT,
        "plan_hash": PLAN_HASH,
    }


def _remediation_item() -> dict[str, object]:
    return {
        "PK": f"CUSTOMER#{CUSTOMER_ID}",
        "SK": f"REMEDIATION#{REMEDIATION_ID}",
        "context": {"finding": {"finding_id": FINDING_ID}},
    }


class FakeTable:
    """GSI1 query + get_item 을 흉내내는 resource table stub."""

    def __init__(self, *, job=None, items=None) -> None:
        self._job = job
        self._items = items or {}

    def query(self, **kwargs: object) -> dict[str, object]:
        return {"Items": [] if self._job is None else [self._job]}

    def get_item(self, **kwargs: object) -> dict[str, object]:
        key = kwargs["Key"]
        stored = self._items.get((key["PK"], key["SK"]))
        return {} if stored is None else {"Item": stored}


def _resolver(customer_id: str, repository_id: str) -> str:
    return AWS_ACCOUNT_ID


class DeploymentWorkRepositoryTest(unittest.TestCase):
    def test_assembles_run_deployment_work_without_plan_facts(self) -> None:
        table = FakeTable(
            job=_job_item(revision=1),
            items={(f"CUSTOMER#{CUSTOMER_ID}", f"DEPLOYMENT#{DEPLOYMENT_ID}"): _deployment_item()},
        )
        repo = DynamoDbDeploymentWorkRepository(table, aws_account_id_for=_resolver)
        work = repo.get_work(job_id=JOB_ID, expected_revision=1)
        self.assertIsNotNone(work)
        self.assertEqual(work.customer_id, CUSTOMER_ID)
        self.assertEqual(work.aws_account_id, AWS_ACCOUNT_ID)
        self.assertIsNone(work.plan)
        self.assertIsNone(work.approval)
        self.assertIsNone(work.plan_run)
        self.assertIsNone(work.sync_target)
        self.assertIsNone(work.run_reference)

    def test_assembles_apply_work_with_plan_approval_and_plan_run(self) -> None:
        table = FakeTable(
            job=_job_item(revision=2),
            items={
                (f"CUSTOMER#{CUSTOMER_ID}", f"DEPLOYMENT#{DEPLOYMENT_ID}"): _deployment_item(
                    with_plan=True
                ),
                (
                    f"CUSTOMER#{CUSTOMER_ID}",
                    f"DEPLOYMENT#{DEPLOYMENT_ID}#APPROVAL#approval-{DEPLOYMENT_ID}",
                ): _approval_item(),
                (
                    f"CUSTOMER#{CUSTOMER_ID}",
                    f"REMEDIATION#{REMEDIATION_ID}",
                ): _remediation_item(),
            },
        )
        repo = DynamoDbDeploymentWorkRepository(table, aws_account_id_for=_resolver)
        work = repo.get_work(job_id=JOB_ID, expected_revision=2)
        self.assertIsNotNone(work)
        self.assertEqual(work.plan.plan_hash, PLAN_HASH)
        self.assertEqual(work.state_version.lineage, LINEAGE)
        self.assertEqual(work.plan_run.run_id, PLAN_RUN_ID)
        self.assertEqual(work.approval.approved_by, "admin-1")
        self.assertEqual(work.sync_target.finding_id, FINDING_ID)
        # run_reference는 EventBridge 완료 Event 저장(별도 조각) 전까지 None으로 남는다.
        self.assertIsNone(work.run_reference)

    def test_returns_none_for_a_wrong_job_type(self) -> None:
        table = FakeTable(job=_job_item(job_type="ASSESSMENT"))
        repo = DynamoDbDeploymentWorkRepository(table, aws_account_id_for=_resolver)
        self.assertIsNone(repo.get_work(job_id=JOB_ID, expected_revision=1))

    def test_returns_none_for_a_revision_mismatch(self) -> None:
        table = FakeTable(
            job=_job_item(revision=1),
            items={(f"CUSTOMER#{CUSTOMER_ID}", f"DEPLOYMENT#{DEPLOYMENT_ID}"): _deployment_item()},
        )
        repo = DynamoDbDeploymentWorkRepository(table, aws_account_id_for=_resolver)
        self.assertIsNone(repo.get_work(job_id=JOB_ID, expected_revision=9))

    def test_returns_none_when_deployment_item_is_absent(self) -> None:
        table = FakeTable(job=_job_item(revision=1), items={})
        repo = DynamoDbDeploymentWorkRepository(table, aws_account_id_for=_resolver)
        self.assertIsNone(repo.get_work(job_id=JOB_ID, expected_revision=1))

    def test_fails_closed_when_aws_account_id_cannot_be_resolved(self) -> None:
        table = FakeTable(
            job=_job_item(revision=1),
            items={(f"CUSTOMER#{CUSTOMER_ID}", f"DEPLOYMENT#{DEPLOYMENT_ID}"): _deployment_item()},
        )
        repo = DynamoDbDeploymentWorkRepository(table, aws_account_id_for=lambda c, r: "")
        with self.assertRaises(RepositoryError):
            repo.get_work(job_id=JOB_ID, expected_revision=1)

    def test_rejects_a_mismatched_deployment_scope(self) -> None:
        wrong = _deployment_item()
        wrong["customer_id"] = "cust-other"
        table = FakeTable(
            job=_job_item(revision=1),
            items={(f"CUSTOMER#{CUSTOMER_ID}", f"DEPLOYMENT#{DEPLOYMENT_ID}"): wrong},
        )
        repo = DynamoDbDeploymentWorkRepository(table, aws_account_id_for=_resolver)
        with self.assertRaises(StoredDataError):
            repo.get_work(job_id=JOB_ID, expected_revision=1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
