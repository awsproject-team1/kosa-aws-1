"""Verify DynamoDB Policy Catalog keys and tenant scope follow DATABASE.md."""

import json
import unittest
from pathlib import Path

from apps.backend.policy import DynamoDbPolicyCatalog, PolicyContextResolver
from packages.common.errors import RepositoryError, StoredDataError
from packages.contracts import AssessmentPhase

REGISTRY_PATH = Path(__file__).parents[2] / "fixtures" / "rules"
CUSTOMER = "cus_001"
OTHER_CUSTOMER = "cus_002"


def _registry_entries(name: str) -> list[dict[str, object]]:
    return json.loads((REGISTRY_PATH / name).read_text(encoding="utf-8"))


class InMemoryTable:
    """Minimal stand-in for the shared metadata table."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, object]] = {}
        self.reads: list[dict[str, object]] = []
        self.fail = False

    def put(self, customer_id: str, sort_key: str, payload: dict[str, object]) -> None:
        item = dict(payload)
        item.update({"PK": f"CUSTOMER#{customer_id}", "SK": sort_key, "customer_id": customer_id})
        self.items[(item["PK"], item["SK"])] = item

    def get_item(self, **kwargs: object) -> dict[str, object]:
        if self.fail:
            raise RuntimeError("provider unavailable")
        key = kwargs["Key"]
        assert isinstance(key, dict)
        self.reads.append(key)
        item = self.items.get((key["PK"], key["SK"]))
        return {} if item is None else {"Item": item}


def _seeded_table() -> InMemoryTable:
    table = InMemoryTable()
    profile = _registry_entries("profiles.json")[0]
    table.put(CUSTOMER, f"POLICY_PROFILE#{profile['policy_profile_id']}", profile)
    for rule in _registry_entries("rules.s3.json"):
        table.put(CUSTOMER, f"RULE#{rule['rule_id']}#VERSION#{rule['version']}", rule)
    for source in _registry_entries("sources.json"):
        table.put(
            CUSTOMER, f"POLICY_SOURCE#{source['source_id']}#VERSION#{source['version']}", source
        )
    return table


class DynamoDbPolicyCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.table = _seeded_table()
        self.catalog = DynamoDbPolicyCatalog(self.table, customer_id=CUSTOMER)

    def test_reads_a_profile_rule_and_source_with_the_documented_keys(self) -> None:
        self.assertIsNotNone(self.catalog.get_profile("profile-mvp-baseline"))
        self.assertIsNotNone(self.catalog.get_rule("S3-PUBLIC-001", "2026-08-31"))
        self.assertIsNotNone(self.catalog.get_source("isms-p-2023", "2023-10-31"))

        self.assertEqual(
            self.table.reads,
            [
                {"PK": f"CUSTOMER#{CUSTOMER}", "SK": "POLICY_PROFILE#profile-mvp-baseline"},
                {"PK": f"CUSTOMER#{CUSTOMER}", "SK": "RULE#S3-PUBLIC-001#VERSION#2026-08-31"},
                {
                    "PK": f"CUSTOMER#{CUSTOMER}",
                    "SK": "POLICY_SOURCE#isms-p-2023#VERSION#2023-10-31",
                },
            ],
        )

    def test_resolves_a_policy_context_through_the_catalog_protocol(self) -> None:
        context = PolicyContextResolver(self.catalog).resolve(
            policy_profile_id="profile-mvp-baseline",
            phase=AssessmentPhase.INITIAL,
            resource_type="AWS::S3::Bucket",
        )

        self.assertEqual(len(context.rules), 6)
        self.assertEqual(context.rules[0].rule_id, "S3-PUBLIC-001")

    def test_returns_none_for_an_absent_item(self) -> None:
        self.assertIsNone(self.catalog.get_profile("profile-unknown"))
        self.assertIsNone(self.catalog.get_rule("S3-PUBLIC-001", "1999-01-01"))

    def test_cannot_read_another_customers_partition(self) -> None:
        """Catalog는 자신의 customer partition만 조회한다."""
        other = DynamoDbPolicyCatalog(self.table, customer_id=OTHER_CUSTOMER)

        self.assertIsNone(other.get_profile("profile-mvp-baseline"))
        self.assertEqual(
            self.table.reads,
            [{"PK": f"CUSTOMER#{OTHER_CUSTOMER}", "SK": "POLICY_PROFILE#profile-mvp-baseline"}],
        )

    def test_rejects_an_item_whose_customer_scope_does_not_match(self) -> None:
        """PK가 맞아도 항목의 customer_id가 다르면 읽지 않는다."""
        profile = _registry_entries("profiles.json")[0]
        self.table.put(OTHER_CUSTOMER, "POLICY_PROFILE#profile-leaked", profile)
        leaked = self.table.items[(f"CUSTOMER#{OTHER_CUSTOMER}", "POLICY_PROFILE#profile-leaked")]
        self.table.items[(f"CUSTOMER#{CUSTOMER}", "POLICY_PROFILE#profile-leaked")] = leaked

        with self.assertRaisesRegex(StoredDataError, "customer scope is invalid"):
            self.catalog.get_profile("profile-leaked")

    def test_rejects_a_rule_stored_outside_its_version_pin(self) -> None:
        rule = dict(_registry_entries("rules.s3.json")[0])
        rule["version"] = "2026-01-01"
        self.table.put(CUSTOMER, "RULE#S3-PUBLIC-001#VERSION#2026-08-31", rule)

        with self.assertRaisesRegex(StoredDataError, "version pin is invalid"):
            self.catalog.get_rule("S3-PUBLIC-001", "2026-08-31")

    def test_rejects_a_stored_item_that_is_not_a_valid_rule(self) -> None:
        self.table.put(CUSTOMER, "RULE#S3-BROKEN-001#VERSION#v1", {"rule_id": "S3-BROKEN-001"})

        with self.assertRaisesRegex(StoredDataError, "stored policy rule is invalid"):
            self.catalog.get_rule("S3-BROKEN-001", "v1")

    def test_reports_a_provider_failure_as_a_repository_error(self) -> None:
        self.table.fail = True

        with self.assertRaisesRegex(RepositoryError, "policy catalog read failed"):
            self.catalog.get_profile("profile-mvp-baseline")

    def test_requires_a_customer_scope(self) -> None:
        with self.assertRaisesRegex(ValueError, "customer_id must be a non-empty string"):
            DynamoDbPolicyCatalog(self.table, customer_id="  ")


if __name__ == "__main__":
    unittest.main()
