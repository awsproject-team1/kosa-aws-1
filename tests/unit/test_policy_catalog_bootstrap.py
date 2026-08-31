"""Approved Registry publication to the customer DynamoDB catalog is immutable."""

import unittest
from collections.abc import Mapping
from pathlib import Path

from apps.backend.policy import (
    DynamoDbPolicyCatalog,
    DynamoDbPolicyCatalogBootstrap,
    PolicyCatalogBootstrapError,
    PolicyContextResolver,
    load_rule_registry,
)
from packages.contracts import AssessmentPhase

REGISTRY = load_rule_registry(Path(__file__).parents[2] / "fixtures" / "rules")


class ConditionalFailure(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class Table:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, object]] = {}

    def put_item(self, **kwargs: object) -> None:
        item = kwargs["Item"]
        assert isinstance(item, dict)
        key = (item["PK"], item["SK"])
        if key in self.items:
            raise ConditionalFailure()
        self.items[key] = dict(item)

    def get_item(self, **kwargs: object) -> Mapping[str, object]:
        key = kwargs["Key"]
        assert isinstance(key, dict)
        item = self.items.get((key["PK"], key["SK"]))
        return {} if item is None else {"Item": item}


class PolicyCatalogBootstrapTest(unittest.TestCase):
    def test_publishes_registry_once_and_catalog_resolves_customer_rules(self) -> None:
        table = Table()
        bootstrap = DynamoDbPolicyCatalogBootstrap(table, customer_id="cust-001")

        self.assertEqual(
            bootstrap.publish(REGISTRY),
            len(REGISTRY.sources) + len(REGISTRY.rules) + len(REGISTRY.profiles),
        )
        self.assertEqual(bootstrap.publish(REGISTRY), 0)
        catalog = DynamoDbPolicyCatalog(table, customer_id="cust-001")
        resolved = catalog.get_profile("profile-mvp-baseline")
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(len(resolved.rule_references), 6)
        self.assertEqual(
            len(
                PolicyContextResolver(catalog)
                .resolve(
                    policy_profile_id="profile-mvp-baseline",
                    phase=AssessmentPhase.INITIAL,
                    resource_type="AWS::S3::Bucket",
                )
                .rules
            ),
            6,
        )

    def test_rejects_different_content_at_an_immutable_key(self) -> None:
        table = Table()
        bootstrap = DynamoDbPolicyCatalogBootstrap(table, customer_id="cust-001")
        bootstrap.publish(REGISTRY)
        item = table.items[("CUSTOMER#cust-001", "RULE#S3-PUBLIC-001#VERSION#2026-08-31")]
        item["title"] = "tampered"

        with self.assertRaisesRegex(PolicyCatalogBootstrapError, "different immutable"):
            bootstrap.publish(REGISTRY)
