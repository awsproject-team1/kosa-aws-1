"""Verify DynamoDB Job keys follow the current DATABASE.md model."""

import unittest

from apps.backend.jobs import create_job
from apps.backend.repositories import DynamoDbJobRepository
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


if __name__ == "__main__":
    unittest.main()
