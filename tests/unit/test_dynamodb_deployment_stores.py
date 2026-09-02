"""D Deployment Worker의 plan/run/verification store DynamoDB write 테스트 (ADR-0019).

고정하는 불변식:
- plan facts는 `DEPLOYMENT#{id}` item에 conditional update로 채워지고, 재시도는 멱등 흡수된다.
- apply dispatch는 결정적 `#DISPATCH` 키로 한 번만 기록된다.
- verified run facts는 `#EVENT#{run_id}` 키로 한 번만 기록된다.
- store는 work scope 밖의 값(다른 customer/repository/deployment)을 거부한다.
"""

import unittest

from apps.backend.deployment import DeploymentWork
from apps.backend.repositories import (
    DynamoDbDeploymentPlanStore,
    DynamoDbDeploymentRunStore,
    DynamoDbDeploymentVerificationStore,
)
from packages.contracts import (
    ApplyDispatchReceipt,
    WorkflowConclusion,
    WorkflowRunFacts,
)
from tests.unit.test_deployment_worker import (
    APPLY_WORKFLOW,
    AWS_ACCOUNT_ID,
    COMMIT,
    CUSTOMER_ID,
    DEPLOYMENT_ID,
    JOB_ID,
    REPOSITORY_ID,
    RUN_ID,
    build_approval,
    build_plan,
    build_plan_result,
    build_plan_run,
    success_facts,
)


class ConditionalCheckFailed(Exception):
    def __init__(self) -> None:
        super().__init__("conditional check failed")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class Transactions:
    """A transaction client stub that records calls and can be told to reject once."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self._fail = fail

    def transact_write_items(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self._fail:
            raise ConditionalCheckFailed()
        return {}


def apply_work() -> DeploymentWork:
    """A work item carrying the plan/approval/state needed by apply-side stores."""
    return DeploymentWork(
        customer_id=CUSTOMER_ID,
        deployment_id=DEPLOYMENT_ID,
        repository_id=REPOSITORY_ID,
        aws_account_id=AWS_ACCOUNT_ID,
        job_id=JOB_ID,
        revision=2,
        commit_sha=COMMIT,
        plan=build_plan(),
        state_version=build_plan_result().state_version,
        plan_run=build_plan_run(),
        approval=build_approval(),
    )


def plan_work() -> DeploymentWork:
    return DeploymentWork(
        customer_id=CUSTOMER_ID,
        deployment_id=DEPLOYMENT_ID,
        repository_id=REPOSITORY_ID,
        aws_account_id=AWS_ACCOUNT_ID,
        job_id=JOB_ID,
        revision=1,
        commit_sha=COMMIT,
    )


class DeploymentPlanStoreTest(unittest.TestCase):
    def test_fills_plan_facts_with_a_conditional_update(self) -> None:
        transactions = Transactions()
        store = DynamoDbDeploymentPlanStore(table_name="metadata", transaction_client=transactions)
        store.put_plan_if_absent(work=plan_work(), result=build_plan_result())
        update = transactions.calls[0]["TransactItems"][0]["Update"]
        self.assertIn("attribute_not_exists(plan_hash)", update["ConditionExpression"])
        self.assertIn("plan_run = :plan_run", update["UpdateExpression"])
        # customer/deployment scope가 key에 정확히 들어간다.
        self.assertEqual(update["Key"]["PK"], {"S": f"CUSTOMER#{CUSTOMER_ID}"})
        self.assertEqual(update["Key"]["SK"], {"S": f"DEPLOYMENT#{DEPLOYMENT_ID}"})

    def test_absorbs_a_conditional_failure_as_idempotent_retry(self) -> None:
        store = DynamoDbDeploymentPlanStore(
            table_name="metadata", transaction_client=Transactions(fail=True)
        )
        # 이미 채워진 plan을 다시 쓰려는 재시도는 오류가 아니라 흡수된다.
        store.put_plan_if_absent(work=plan_work(), result=build_plan_result())

    def test_rejects_a_plan_outside_the_work_scope(self) -> None:
        store = DynamoDbDeploymentPlanStore(
            table_name="metadata", transaction_client=Transactions()
        )
        foreign = DeploymentWork(
            customer_id="cust-other",
            deployment_id=DEPLOYMENT_ID,
            repository_id=REPOSITORY_ID,
            aws_account_id=AWS_ACCOUNT_ID,
            job_id=JOB_ID,
            revision=1,
            commit_sha=COMMIT,
        )
        with self.assertRaises(ValueError):
            store.put_plan_if_absent(work=foreign, result=build_plan_result())


class DeploymentRunStoreTest(unittest.TestCase):
    def test_records_the_dispatch_under_a_deterministic_key(self) -> None:
        transactions = Transactions()
        store = DynamoDbDeploymentRunStore(table_name="metadata", transaction_client=transactions)
        receipt = ApplyDispatchReceipt(
            deployment_id=DEPLOYMENT_ID, repository_id=REPOSITORY_ID, workflow_path=APPLY_WORKFLOW
        )
        store.put_receipt_if_absent(work=apply_work(), receipt=receipt)
        item = transactions.calls[0]["TransactItems"][0]["Put"]["Item"]
        self.assertEqual(item["SK"], {"S": f"DEPLOYMENT#{DEPLOYMENT_ID}#DISPATCH"})
        self.assertEqual(item["entity_type"], {"S": "DEPLOYMENT_APPLY_DISPATCH"})

    def test_absorbs_a_duplicate_dispatch(self) -> None:
        store = DynamoDbDeploymentRunStore(
            table_name="metadata", transaction_client=Transactions(fail=True)
        )
        receipt = ApplyDispatchReceipt(
            deployment_id=DEPLOYMENT_ID, repository_id=REPOSITORY_ID, workflow_path=APPLY_WORKFLOW
        )
        store.put_receipt_if_absent(work=apply_work(), receipt=receipt)

    def test_rejects_a_receipt_outside_the_work_scope(self) -> None:
        store = DynamoDbDeploymentRunStore(table_name="metadata", transaction_client=Transactions())
        receipt = ApplyDispatchReceipt(
            deployment_id="dep-other", repository_id=REPOSITORY_ID, workflow_path=APPLY_WORKFLOW
        )
        with self.assertRaises(ValueError):
            store.put_receipt_if_absent(work=apply_work(), receipt=receipt)


class DeploymentVerificationStoreTest(unittest.TestCase):
    def test_confirms_the_pending_event_item_to_verified(self) -> None:
        transactions = Transactions()
        store = DynamoDbDeploymentVerificationStore(
            table_name="metadata", transaction_client=transactions
        )
        store.put_verification_if_absent(work=apply_work(), facts=success_facts())
        update = transactions.calls[0]["TransactItems"][0]["Update"]
        # 예약 item(PENDING_VERIFICATION)을 conditional update로 VERIFIED 확정한다.
        self.assertEqual(update["Key"]["SK"], {"S": f"DEPLOYMENT#{DEPLOYMENT_ID}#EVENT#{RUN_ID}"})
        self.assertEqual(update["ConditionExpression"], "#status = :pending")
        self.assertIn("#status = :verified", update["UpdateExpression"])
        self.assertEqual(update["ExpressionAttributeValues"][":conclusion"], {"S": "SUCCESS"})

    def test_absorbs_an_already_verified_or_absent_reservation(self) -> None:
        store = DynamoDbDeploymentVerificationStore(
            table_name="metadata", transaction_client=Transactions(fail=True)
        )
        # 이미 VERIFIED이거나 예약이 없으면 조건 실패 → 멱등 흡수(오류 아님).
        store.put_verification_if_absent(work=apply_work(), facts=success_facts())

    def test_rejects_facts_outside_the_repository_scope(self) -> None:
        store = DynamoDbDeploymentVerificationStore(
            table_name="metadata", transaction_client=Transactions()
        )
        # work는 유효하게 두고, 재조회 facts만 다른 repository로 온 경우를 거부한다.
        foreign = WorkflowRunFacts(
            run_id=RUN_ID,
            repository_id="repo-other",
            workflow_path=APPLY_WORKFLOW,
            ref=COMMIT,
            commit_sha=COMMIT,
            conclusion=WorkflowConclusion.SUCCESS,
            plan_hash=success_facts().plan_hash,
        )
        with self.assertRaises(ValueError):
            store.put_verification_if_absent(work=apply_work(), facts=foreign)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
