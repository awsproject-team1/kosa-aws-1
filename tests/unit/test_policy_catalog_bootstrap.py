"""Approved Registry publication to the customer DynamoDB catalog is immutable."""

import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from apps.backend.policy import (
    DynamoDbPolicyCatalog,
    DynamoDbPolicyCatalogBootstrap,
    PolicyCatalogBootstrapError,
    PolicyContextResolver,
    load_rule_registry,
)
from packages.contracts import AssessmentPhase, RuleLifecycle

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
        expression = str(kwargs.get("ConditionExpression", ""))
        if expression.startswith("current_version = "):
            # pointer 이동. 지금 가리키는 판본이 읽은 값과 같을 때만 덮어쓴다.
            values = kwargs["ExpressionAttributeValues"]
            assert isinstance(values, dict)
            existing = self.items.get(key)
            if existing is None or existing.get("current_version") != values[":current"]:
                raise ConditionalFailure()
            self.items[key] = dict(item)
            return
        if key in self.items:
            raise ConditionalFailure()
        self.items[key] = dict(item)

    def get_item(self, **kwargs: object) -> Mapping[str, object]:
        key = kwargs["Key"]
        assert isinstance(key, dict)
        item = self.items.get((key["PK"], key["SK"]))
        return {} if item is None else {"Item": dict(item)}


class PolicyCatalogBootstrapTest(unittest.TestCase):
    def test_publishes_registry_once_and_catalog_resolves_customer_rules(self) -> None:
        table = Table()
        bootstrap = DynamoDbPolicyCatalogBootstrap(table, customer_id="cust-001")

        # Profile마다 판본 이력과 current pointer 두 item이 생긴다.
        self.assertEqual(
            bootstrap.publish(REGISTRY),
            len(REGISTRY.sources) + len(REGISTRY.rules) + 2 * len(REGISTRY.profiles),
        )
        self.assertEqual(bootstrap.publish(REGISTRY), 0)
        rule_item = table.items[("CUSTOMER#cust-001", "RULE#S3-PUBLIC-001#VERSION#2026-08-31")]
        self.assertEqual(rule_item["lifecycle"], RuleLifecycle.APPROVED.value)
        catalog = DynamoDbPolicyCatalog(table, customer_id="cust-001")
        resolved = catalog.get_profile("profile-mvp-baseline")
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(len(resolved.rule_references), 6)
        # 고정한 판본을 직접 읽을 수 있어야 한다. pointer만 있으면 실행 중 게시된 새 Profile이
        # 이미 계획된 평가를 완료 불가능하게 만든다.
        pinned = catalog.get_profile("profile-mvp-baseline", resolved.version)
        self.assertEqual(pinned, resolved)
        self.assertIsNone(catalog.get_profile("profile-mvp-baseline", "a-version-never-published"))
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

    def test_accepts_legacy_rule_item_without_lifecycle_on_republish(self) -> None:
        table = Table()
        bootstrap = DynamoDbPolicyCatalogBootstrap(table, customer_id="cust-001")
        bootstrap.publish(REGISTRY)
        for key, item in table.items.items():
            if key[1].startswith("RULE#"):
                item.pop("lifecycle", None)

        self.assertEqual(bootstrap.publish(REGISTRY), 0)


class ProfilePointerTest(unittest.TestCase):
    """A registry that declares a new Profile version moves the pointer; nothing else moves."""

    @staticmethod
    def _with_profile_version(version: str):
        profiles = tuple(replace(profile, version=version) for profile in REGISTRY.profiles)
        return replace(REGISTRY, profiles=profiles)

    def test_a_new_profile_version_moves_the_pointer_and_keeps_the_old_version(self) -> None:
        table = Table()
        bootstrap = DynamoDbPolicyCatalogBootstrap(table, customer_id="cust-001")
        bootstrap.publish(REGISTRY)

        published = bootstrap.publish(self._with_profile_version("v9"))

        # 새 판본 item 하나 + pointer 이동 하나, Profile마다.
        self.assertEqual(published, 2 * len(REGISTRY.profiles))
        pointer = table.items[("CUSTOMER#cust-001", "POLICY_PROFILE#profile-mvp-baseline")]
        self.assertEqual(pointer["current_version"], "v9")
        self.assertIn(
            ("CUSTOMER#cust-001", "POLICY_PROFILE#profile-mvp-baseline#VERSION#v2"), table.items
        )
        self.assertIn(
            ("CUSTOMER#cust-001", "POLICY_PROFILE#profile-mvp-baseline#VERSION#v9"), table.items
        )
        # 같은 Registry를 다시 게시하면 아무것도 움직이지 않는다.
        self.assertEqual(bootstrap.publish(self._with_profile_version("v9")), 0)

    def test_a_different_content_at_the_same_profile_version_still_fails_closed(self) -> None:
        table = Table()
        bootstrap = DynamoDbPolicyCatalogBootstrap(table, customer_id="cust-001")
        bootstrap.publish(REGISTRY)
        version_key = ("CUSTOMER#cust-001", "POLICY_PROFILE#profile-mvp-baseline#VERSION#v2")
        table.items[version_key]["rule_references"] = []

        with self.assertRaisesRegex(PolicyCatalogBootstrapError, "different immutable"):
            bootstrap.publish(REGISTRY)

    def test_a_pointer_moved_by_someone_else_is_not_overwritten(self) -> None:
        table = Table()
        bootstrap = DynamoDbPolicyCatalogBootstrap(table, customer_id="cust-001")
        bootstrap.publish(REGISTRY)
        pointer_key = ("CUSTOMER#cust-001", "POLICY_PROFILE#profile-mvp-baseline")
        # 읽기와 쓰기 사이에 다른 게시가 pointer를 옮긴 상황을 흉내 낸다.
        original_get = table.get_item

        def racing_get(**kwargs: object):
            result = original_get(**kwargs)
            key = kwargs["Key"]
            assert isinstance(key, dict)
            if (key["PK"], key["SK"]) == pointer_key:
                # 읽기가 끝난 직후, 쓰기 전에 다른 게시가 pointer를 옮겼다.
                table.items[pointer_key]["current_version"] = "v-elsewhere"
            return result

        table.get_item = racing_get  # type: ignore[method-assign]

        with self.assertRaisesRegex(PolicyCatalogBootstrapError, "moved concurrently"):
            bootstrap.publish(self._with_profile_version("v9"))
