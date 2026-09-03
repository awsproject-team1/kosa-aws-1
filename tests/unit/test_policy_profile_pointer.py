"""Profile publication keeps an immutable history and one guarded current pointer.

두 가지를 고정한다.

1. **판본은 지워지지 않는다.** Assessment가 고정한 version을 나중에 직접 읽을 수 있어야 한다.
   그래야 실행 중 새 Profile이 게시돼도 이미 계획된 평가가 끝까지 같은 Rule 집합을 쓴다.
2. **pointer 교체는 낙관적 동시성으로 보호한다.** 조건이 없으면 동시에 게시된 두 Profile 중
   나중 것이 앞의 것을 조용히 덮어쓰고, 두 게시자 모두 자기 Profile이 현재 판본이라고 믿는다.
"""

import unittest

from apps.backend.policy import DynamoDbPolicyCatalog
from apps.backend.repositories.policy_approval import (
    DynamoDbPolicyApprovalRepository,
    ProfileConcurrentlyUpdatedError,
)
from packages.contracts import (
    ApprovalRejectionCode,
    PolicyProfile,
    PolicyRuleReference,
)
from tests.unit.test_authoring_result_persistence import FakeTable

CUSTOMER = "cust-001"
PROFILE_ID = "profile-customer-baseline"


def _profile(version: str, *, rule_id: str = "CUST-RULE-1") -> PolicyProfile:
    return PolicyProfile(
        policy_profile_id=PROFILE_ID,
        version=version,
        rule_references=(PolicyRuleReference(rule_id=rule_id, version="2026-09-01"),),
    )


def _repository(table: FakeTable) -> DynamoDbPolicyApprovalRepository:
    return DynamoDbPolicyApprovalRepository(
        table_name="governance",
        transaction_client=table,  # type: ignore[arg-type]
        table=table,  # type: ignore[arg-type]
    )


def _publish(
    repository: DynamoDbPolicyApprovalRepository,
    profile: PolicyProfile,
    *,
    expected_current_version: str | None = None,
) -> None:
    repository.record_profile(
        customer_id=CUSTOMER,
        profile=profile,
        published_by="admin@example.com",
        published_at="2026-09-03T00:00:00Z",
        expected_current_version=expected_current_version,
    )


class VersionHistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.table = FakeTable()
        self.repository = _repository(self.table)

    def test_publishing_writes_both_the_version_item_and_the_pointer(self) -> None:
        _publish(self.repository, _profile("v1"))

        keys = sorted(sk for _pk, sk in self.table.items if sk.startswith("POLICY_PROFILE#"))
        self.assertEqual(
            keys,
            [f"POLICY_PROFILE#{PROFILE_ID}", f"POLICY_PROFILE#{PROFILE_ID}#VERSION#v1"],
        )

    def test_a_replaced_version_stays_readable(self) -> None:
        """실행 중인 Assessment는 게시가 바뀌어도 자기가 고정한 판본으로 끝난다."""
        _publish(self.repository, _profile("v1"))
        _publish(
            self.repository, _profile("v2", rule_id="CUST-RULE-2"), expected_current_version="v1"
        )
        catalog = DynamoDbPolicyCatalog(self.table, customer_id=CUSTOMER)  # type: ignore[arg-type]

        self.assertEqual(catalog.get_profile(PROFILE_ID, "v1"), _profile("v1"))
        self.assertEqual(
            catalog.get_profile(PROFILE_ID, "v2"), _profile("v2", rule_id="CUST-RULE-2")
        )
        current = catalog.get_profile(PROFILE_ID)
        assert current is not None
        self.assertEqual(current.version, "v2")

    def test_a_version_that_was_never_published_is_not_found(self) -> None:
        _publish(self.repository, _profile("v1"))
        catalog = DynamoDbPolicyCatalog(self.table, customer_id=CUSTOMER)  # type: ignore[arg-type]

        self.assertIsNone(catalog.get_profile(PROFILE_ID, "v9"))

    def test_republishing_the_same_version_with_different_rules_fails_closed(self) -> None:
        """판본은 immutable하다. 같은 version이 다른 Rule 집합을 가리키면 Evidence가 무의미해진다."""
        _publish(self.repository, _profile("v1"))

        with self.assertRaises(ProfileConcurrentlyUpdatedError):
            _publish(
                self.repository,
                _profile("v1", rule_id="CUST-RULE-OTHER"),
                expected_current_version="v1",
            )


class PointerConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.table = FakeTable()
        self.repository = _repository(self.table)

    def test_the_first_publication_requires_no_existing_pointer(self) -> None:
        _publish(self.repository, _profile("v1"))

        with self.assertRaises(ProfileConcurrentlyUpdatedError):
            # 최초 게시로 다시 시도하면 이미 pointer가 있으므로 거절된다.
            _publish(self.repository, _profile("v2", rule_id="CUST-RULE-2"))

    def test_a_replacement_must_name_the_version_it_replaces(self) -> None:
        _publish(self.repository, _profile("v1"))
        _publish(
            self.repository, _profile("v2", rule_id="CUST-RULE-2"), expected_current_version="v1"
        )

        with self.assertRaises(ProfileConcurrentlyUpdatedError) as caught:
            _publish(
                self.repository,
                _profile("v3", rule_id="CUST-RULE-3"),
                expected_current_version="v1",
            )

        self.assertEqual(
            str(caught.exception), ApprovalRejectionCode.PROFILE_CONCURRENTLY_UPDATED.value
        )

    def test_a_losing_concurrent_publication_does_not_move_the_pointer(self) -> None:
        _publish(self.repository, _profile("v1"))
        _publish(
            self.repository, _profile("v2", rule_id="CUST-RULE-2"), expected_current_version="v1"
        )

        with self.assertRaises(ProfileConcurrentlyUpdatedError):
            _publish(
                self.repository,
                _profile("v3", rule_id="CUST-RULE-3"),
                expected_current_version="v1",
            )

        pointer = self.table.items[(f"CUSTOMER#{CUSTOMER}", f"POLICY_PROFILE#{PROFILE_ID}")]
        self.assertEqual(pointer["current_version"], "v2")
        self.assertNotIn(
            (f"CUSTOMER#{CUSTOMER}", f"POLICY_PROFILE#{PROFILE_ID}#VERSION#v3"), self.table.items
        )

    def test_an_identical_republication_is_absorbed(self) -> None:
        """게시 API도 at-least-once로 재시도될 수 있다. 같은 판본을 다시 게시하면 성공이다."""
        _publish(self.repository, _profile("v1"))
        before = {key: dict(item) for key, item in self.table.items.items()}

        _publish(self.repository, _profile("v1"), expected_current_version="v1")

        profiles_after = {
            key: dict(item)
            for key, item in self.table.items.items()
            if key[1].startswith("POLICY_PROFILE#")
        }
        profiles_before = {
            key: value for key, value in before.items() if key[1].startswith("POLICY_PROFILE#")
        }
        self.assertEqual(profiles_after, profiles_before)


if __name__ == "__main__":
    unittest.main()
