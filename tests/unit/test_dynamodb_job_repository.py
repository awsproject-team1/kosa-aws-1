"""Verify DynamoDB Job keys follow the current DATABASE.md model."""

import unittest

from apps.backend.assessment import Assessment
from apps.backend.jobs import create_job
from apps.backend.repositories import DynamoDbAssessmentRepository, DynamoDbJobRepository
from packages.contracts import JobCurrentStep


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

    def test_assessment_persists_worker_selectors_and_repository_history_index(self) -> None:
        table = InMemoryTable()
        repository = DynamoDbAssessmentRepository(table)

        repository.create_assessment(
            Assessment(
                assessment_id="asm-001",
                customer_id="cust-001",
                job_id="job-001",
                repository_id="repo-001",
                policy_profile_id="profile-001",
            )
        )

        item = table.items[("CUSTOMER#cust-001", "ASSESSMENT#asm-001")]
        self.assertEqual(item["job_id"], "job-001")
        self.assertEqual(item["repository_id"], "repo-001")
        self.assertEqual(item["policy_profile_id"], "profile-001")
        self.assertEqual(item["GSI3PK"], "REPOSITORY#repo-001")


if __name__ == "__main__":
    unittest.main()
