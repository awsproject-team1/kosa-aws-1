"""Verify DynamoDB Job keys follow the current DATABASE.md model."""

import unittest

from apps.backend.assessment import Assessment
from apps.backend.jobs import WorkflowOutboxEntry, create_job
from apps.backend.repositories import DynamoDbAssessmentWorkflowRepository, DynamoDbJobRepository
from packages.contracts import (
    AssessmentPhase,
    JobCurrentStep,
    WorkflowCommand,
    WorkflowTask,
)


def _attr(item: dict, key: str) -> object:
    """Read a value stored through the low-level transaction client (AttributeValue).

    Transaction writes go through the low-level DynamoDB client, so items are stored
    marshaled as AttributeValues such as {"S": "..."}; unwrap the scalar for assertions.
    """
    value = item[key]
    if isinstance(value, dict) and len(value) == 1:
        return next(iter(value.values()))
    return value


class InMemoryTable:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, object]] = {}

    def put_item(self, **kwargs: object) -> object:
        item = kwargs["Item"]
        assert isinstance(item, dict)
        key = (item["PK"], item["SK"])
        if "attribute_not_exists" in str(kwargs.get("ConditionExpression")) and key in self.items:
            error = Exception()
            error.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
            raise error
        self.items[key] = item
        return {}

    def get_item(self, **kwargs: object) -> dict[str, object]:
        key_value = kwargs["Key"]
        assert isinstance(key_value, dict)
        item = self.items.get((key_value["PK"], key_value["SK"]))
        return {} if item is None else {"Item": item}

    def query(self, **kwargs: object) -> dict[str, object]:
        """GSI2 (`OUTBOX#PENDING`) 조회. sweeper가 무엇을 다시 보낼지 정하는 그 query다."""
        values = kwargs["ExpressionAttributeValues"]
        assert isinstance(values, dict)
        wanted = values[":pending"]
        matched = [
            _plain(item)
            for item in self.items.values()
            if "GSI2PK" in item and _attr(item, "GSI2PK") == wanted
        ]
        return {"Items": matched}

    def update_item(self, **kwargs: object) -> object:
        """Evaluate the outbox bookkeeping update, condition included.

        조건을 무시하는 fake는 이 회귀를 잡지 못한다 — 조건이 항상 실패해도 테스트는 통과했고,
        라이브에서는 모든 entry가 영영 PENDING으로 남아 1분마다 다시 큐에 들어갔다.
        """
        key_value = kwargs["Key"]
        assert isinstance(key_value, dict)
        item = self.items.get((key_value["PK"], key_value["SK"]))
        if item is None:
            raise _conditional_failure()
        names = kwargs.get("ExpressionAttributeNames") or {}
        values = kwargs.get("ExpressionAttributeValues") or {}
        assert isinstance(names, dict) and isinstance(values, dict)
        condition = str(kwargs["ConditionExpression"])
        field, _, placeholder = (part.strip() for part in condition.partition("="))
        if _attr(item, names.get(field, field)) != values[placeholder]:
            raise _conditional_failure()
        expression = str(kwargs["UpdateExpression"])
        plain = _plain(item)
        for clause in (
            expression.replace(" REMOVE ", "\nREMOVE ").replace(" ADD ", "\nADD ").splitlines()
        ):
            verb, _, rest = clause.strip().partition(" ")
            if verb == "SET":
                for assignment in rest.split(","):
                    target, _, value_key = (x.strip() for x in assignment.partition("="))
                    plain[names.get(target, target)] = values[value_key]
            elif verb == "REMOVE":
                for name in rest.split(","):
                    plain.pop(name.strip(), None)
            elif verb == "ADD":
                target, value_key = rest.split()
                plain[target] = int(plain.get(target, 0)) + int(values[value_key])
        self.items[(key_value["PK"], key_value["SK"])] = plain
        return {}

    def transact_write_items(self, **kwargs: object) -> object:
        # The production transaction client is a low-level DynamoDB client, so items
        # arrive marshaled as AttributeValues ({"S": ...}); mirror that contract here.
        items = kwargs["TransactItems"]
        assert isinstance(items, list)
        candidates = [entry["Put"]["Item"] for entry in items]
        keys = [(item["PK"]["S"], item["SK"]["S"]) for item in candidates]
        if any(key in self.items for key in keys):
            error = Exception()
            error.response = {"Error": {"Code": "TransactionCanceledException"}}
            raise error
        for key, item in zip(keys, candidates, strict=True):
            self.items[key] = item
        return {}


def _plain(item: dict[str, object]) -> dict[str, object]:
    """Unwrap AttributeValues the transaction path stored; numbers come back as numbers."""
    plain: dict[str, object] = {}
    for name, value in item.items():
        if isinstance(value, dict) and len(value) == 1 and "N" in value:
            plain[name] = int(value["N"])
        else:
            plain[name] = _attr(item, name)
    return plain


def _conditional_failure() -> Exception:
    error = Exception()
    error.response = {"Error": {"Code": "ConditionalCheckFailedException"}}  # type: ignore[attr-defined]
    return error


class DynamoDbJobRepositoryTest(unittest.TestCase):
    def test_job_uses_customer_partition_and_job_sort_key(self) -> None:
        table = InMemoryTable()
        repository = DynamoDbJobRepository(table)
        job = create_job(
            job_id="job-001",
            customer_id="cust-001",
            job_type="ASSESSMENT",
            initial_step=JobCurrentStep.LOAD_IAC,
            requested_by="subject-001",
        )

        repository.create_job(job)

        self.assertIn(("CUSTOMER#cust-001", "JOB#job-001"), table.items)
        self.assertEqual(repository.get_job("cust-001", "job-001"), job)
        self.assertIsNone(repository.get_job("cust-002", "job-001"))

    def test_assessment_job_and_pending_outbox_are_created_atomically(self) -> None:
        table = InMemoryTable()
        repository = DynamoDbAssessmentWorkflowRepository(
            table, table_name="metadata", transaction_client=table
        )
        assessment = Assessment(
            assessment_id="asm-001",
            customer_id="cust-001",
            job_id="job-001",
            repository_id="repo-001",
            policy_profile_id="profile-001",
            policy_profile_version="v1",
        )
        job = create_job(
            job_id="job-001",
            customer_id="cust-001",
            job_type="ASSESSMENT",
            initial_step=JobCurrentStep.LOAD_IAC,
            requested_by="subject-001",
            assessment_id="asm-001",
        )

        repository.create_assessment_workflow(
            assessment,
            job,
            WorkflowOutboxEntry(
                customer_id="cust-001",
                job_id="job-001",
                task=WorkflowTask(
                    job_id="job-001", expected_revision=0, command=WorkflowCommand.ASSESS_RESOURCE
                ),
            ),
        )

        item = table.items[("CUSTOMER#cust-001", "ASSESSMENT#asm-001")]
        self.assertEqual(_attr(item, "job_id"), "job-001")
        self.assertEqual(_attr(item, "repository_id"), "repo-001")
        self.assertEqual(_attr(item, "policy_profile_id"), "profile-001")
        self.assertEqual(_attr(item, "GSI3PK"), "REPOSITORY#repo-001")
        self.assertEqual(_attr(item, "phase"), "INITIAL")
        self.assertNotIn("source_assessment_id", item)
        self.assertNotIn("deployment_id", item)
        self.assertNotIn("model_profile_id", item)
        self.assertNotIn("rubric_version", item)
        # 판본은 verification 전용 pin이 아니라 **모든 phase**가 갖는 값이다. Initial이 이것을
        # 저장하지 않으면 Runtime은 실행 시점의 최신 pointer로 평가하게 된다.
        self.assertEqual(_attr(item, "policy_profile_version"), "v1")
        self.assertIn(("CUSTOMER#cust-001", "JOB#job-001"), table.items)
        outbox = table.items[("CUSTOMER#cust-001", "OUTBOX#JOB#job-001")]
        self.assertEqual(_attr(outbox, "GSI2PK"), "OUTBOX#PENDING")

    def test_verification_assessment_persists_its_phase_and_correlation(self) -> None:
        table = InMemoryTable()
        repository = DynamoDbAssessmentWorkflowRepository(
            table, table_name="metadata", transaction_client=table
        )
        assessment = Assessment(
            assessment_id="asm-002",
            customer_id="cust-001",
            job_id="job-002",
            repository_id="repo-001",
            policy_profile_id="profile-001",
            phase=AssessmentPhase.POST_DEPLOY_VERIFICATION,
            source_assessment_id="asm-001",
            deployment_id="dep-001",
            model_profile_id="assessment-nova-lite-m1-v2",
            rubric_version="m1-v2",
            policy_profile_version="v2",
        )
        job = create_job(
            job_id="job-002",
            customer_id="cust-001",
            job_type="ASSESSMENT",
            initial_step=JobCurrentStep.LOAD_IAC,
            requested_by="subject-001",
            assessment_id="asm-002",
        )

        repository.create_assessment_workflow(
            assessment,
            job,
            WorkflowOutboxEntry(
                customer_id="cust-001",
                job_id="job-002",
                task=WorkflowTask(
                    job_id="job-002", expected_revision=0, command=WorkflowCommand.ASSESS_RESOURCE
                ),
            ),
        )

        item = table.items[("CUSTOMER#cust-001", "ASSESSMENT#asm-002")]
        self.assertEqual(_attr(item, "phase"), "POST_DEPLOY_VERIFICATION")
        self.assertEqual(_attr(item, "source_assessment_id"), "asm-001")
        self.assertEqual(_attr(item, "deployment_id"), "dep-001")
        self.assertEqual(_attr(item, "model_profile_id"), "assessment-nova-lite-m1-v2")
        self.assertEqual(_attr(item, "rubric_version"), "m1-v2")
        self.assertEqual(_attr(item, "policy_profile_version"), "v2")


class OutboxBookkeepingTest(unittest.TestCase):
    """The sweeper only stops re-sending an entry once its status update actually lands.

    라이브 sandbox에서 20개 entry가 dispatch_attempts=0인 채 영영 PENDING이었고 sweeper가 1분마다
    전부 다시 보냈다. 원인은 조건식이 저장된 `status`("PENDING")를 GSI 파티션 값
    ("OUTBOX#PENDING")과 비교해 항상 실패한 것이다.
    """

    def _entry(
        self, table: InMemoryTable
    ) -> tuple[DynamoDbAssessmentWorkflowRepository, WorkflowOutboxEntry]:
        repository = DynamoDbAssessmentWorkflowRepository(
            table, table_name="metadata", transaction_client=table
        )
        entry = WorkflowOutboxEntry(
            customer_id="cust-001",
            job_id="job-001",
            task=WorkflowTask(
                job_id="job-001", expected_revision=0, command=WorkflowCommand.ASSESS_RESOURCE
            ),
        )
        repository.create_assessment_workflow(
            Assessment(
                assessment_id="asm-001",
                customer_id="cust-001",
                job_id="job-001",
                repository_id="repo-001",
                policy_profile_id="profile-001",
                policy_profile_version="v1",
            ),
            create_job(
                job_id="job-001",
                customer_id="cust-001",
                job_type="ASSESSMENT",
                initial_step=JobCurrentStep.LOAD_IAC,
                requested_by="subject-001",
                assessment_id="asm-001",
            ),
            entry,
        )
        return repository, entry

    def test_a_dispatched_entry_leaves_the_pending_index(self) -> None:
        table = InMemoryTable()
        repository, entry = self._entry(table)
        self.assertEqual(len(repository.list_pending_outbox(limit=10)), 1)

        repository.mark_outbox_dispatched(entry)

        stored = table.items[("CUSTOMER#cust-001", "OUTBOX#JOB#job-001")]
        self.assertEqual(_attr(stored, "status"), "DISPATCHED")
        self.assertNotIn("GSI2PK", stored)
        self.assertEqual(repository.list_pending_outbox(limit=10), ())

    def test_a_failed_dispatch_counts_an_attempt_and_stays_pending(self) -> None:
        table = InMemoryTable()
        repository, entry = self._entry(table)

        repository.record_outbox_dispatch_failure(entry)

        stored = table.items[("CUSTOMER#cust-001", "OUTBOX#JOB#job-001")]
        self.assertEqual(_attr(stored, "status"), "PENDING")
        self.assertEqual(int(_attr(stored, "dispatch_attempts")), 1)
        self.assertEqual(len(repository.list_pending_outbox(limit=10)), 1)

    def test_marking_twice_is_refused_by_the_condition(self) -> None:
        """이미 DISPATCHED인 entry를 다시 표시하는 것은 재시도가 아니라 상태 오류다."""
        table = InMemoryTable()
        repository, entry = self._entry(table)
        repository.mark_outbox_dispatched(entry)

        from apps.backend.repositories.errors import RepositoryError

        with self.assertRaises(RepositoryError):
            repository.mark_outbox_dispatched(entry)


if __name__ == "__main__":
    unittest.main()
