"""Approval must write the Rule Registry, or the approval does not exist at runtime.

승인 record만 쓰고 Rule item을 나중에 쓰면, 그 사이에 게시된 Profile이 참조하는 Rule을 Catalog가
찾지 못한다. 그래서 같은 transaction에 넣는다. 그리고 Catalog는 `entity_type`과 `lifecycle`을
확인해, 승인 경계를 거치지 않고 partition에 들어온 item이 평가에 쓰이지 않게 한다.
"""

import unittest
from datetime import UTC, datetime

from apps.backend.policy import DynamoDbPolicyCatalog
from apps.backend.policy.ingestion import approve_source
from apps.backend.repositories.errors import RepositoryError
from apps.backend.repositories.policy_approval import (
    MAX_RULES_PER_APPROVAL,
    DynamoDbPolicyApprovalRepository,
)
from packages.common.errors import ApprovalConflictError, StoredDataError
from packages.contracts import (
    AuthoringRunStatus,
    RuleEvaluationType,
    RuleLifecycle,
)
from tests.unit.test_authoring_result_persistence import (
    DOCUMENT,
    FakeTable,
    _repository,
    _result,
    store_ingestion_item,
)

CUSTOMER = "cust-001"


def _approve(table: FakeTable) -> tuple[DynamoDbPolicyApprovalRepository, tuple[str, ...]]:
    """Store a READY run, then approve every candidate it produced."""
    repository = _repository(table)
    store_ingestion_item(table)
    result = repository.record_authoring_result(customer_id=CUSTOMER, result=_result())
    assert result.status is AuthoringRunStatus.READY
    candidates = _result().candidates
    approval, approved = approve_source(
        DOCUMENT,
        tuple(candidate for candidate in candidates),
        approved_by="reviewer@example.com",
        approved_at="2026-09-03T00:00:00Z",
    )
    repository.record_approval(customer_id=CUSTOMER, approval=approval, candidates=approved)
    return repository, tuple(candidate.rule.rule_id for candidate in candidates)


class ApprovalWriteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.table = FakeTable()

    def test_approval_writes_one_rule_item_per_approved_candidate(self) -> None:
        _repository_, rule_ids = _approve(self.table)

        stored = {
            sk: item for (_pk, sk), item in self.table.items.items() if sk.startswith("RULE#")
        }
        self.assertEqual(len(stored), len(rule_ids))
        for item in stored.values():
            self.assertEqual(item["entity_type"], "POLICY_RULE")
            self.assertEqual(item["lifecycle"], RuleLifecycle.APPROVED.value)
            self.assertEqual(item["customer_id"], CUSTOMER)

    def test_the_runtime_catalog_reads_back_every_execution_semantics_field(self) -> None:
        """Rule item이 실행 의미를 잃으면 승인된 고객 Rule이 legacy Rule로 평가된다."""
        _repository_, rule_ids = _approve(self.table)
        catalog = DynamoDbPolicyCatalog(self.table, customer_id=CUSTOMER)  # type: ignore[arg-type]

        expected = {candidate.rule.rule_id: candidate.rule for candidate in _result().candidates}
        for rule_id in rule_ids:
            with self.subTest(rule=rule_id):
                restored = catalog.get_rule(rule_id, DOCUMENT.source_version)
                self.assertEqual(restored, expected[rule_id])
                assert restored is not None
                self.assertFalse(restored.is_legacy)
                self.assertIn(
                    restored.evaluation_type,
                    {RuleEvaluationType.AWS, RuleEvaluationType.MANUAL},
                )

    def test_an_identical_re_approval_is_absorbed(self) -> None:
        """승인 API는 at-least-once로 재시도될 수 있다.

        멱등 판정은 write 시각을 제외한다. 포함하면 재시도마다 시각이 달라 **모든** 재시도가
        "다른 내용"으로 보이고, 흡수 경로가 사실상 존재하지 않게 된다.
        """
        repository, _rule_ids = _approve(self.table)
        before = {key: dict(item) for key, item in self.table.items.items()}

        approval, approved = approve_source(
            DOCUMENT,
            _result().candidates,
            approved_by="reviewer@example.com",
            approved_at="2026-09-03T00:00:00Z",
        )
        repository.record_approval(customer_id=CUSTOMER, approval=approval, candidates=approved)

        rules_before = {k: v for k, v in before.items() if k[1].startswith("RULE#")}
        rules_after = {k: dict(v) for k, v in self.table.items.items() if k[1].startswith("RULE#")}
        self.assertEqual(rules_after, rules_before)

    def test_a_re_approval_is_absorbed_even_when_the_write_clock_moved(self) -> None:
        """서버가 만드는 시각이 달라졌다는 이유로 재시도를 거절하면 안 된다."""
        store = FakeTable()
        store_ingestion_item(store)
        clock = iter(
            [
                datetime(2026, 9, 3, 0, 0, 0, tzinfo=UTC),
                datetime(2026, 9, 3, 0, 5, 0, tzinfo=UTC),
            ]
        )
        repository = DynamoDbPolicyApprovalRepository(
            table_name="governance",
            transaction_client=store,  # type: ignore[arg-type]
            table=store,  # type: ignore[arg-type]
            now=lambda: next(clock),
            id_factory=lambda: "fixed",
        )
        repository.record_authoring_result(customer_id=CUSTOMER, result=_result())
        approval, approved = approve_source(
            DOCUMENT,
            _result().candidates,
            approved_by="reviewer@example.com",
            approved_at="2026-09-03T00:00:00Z",
        )
        repository.record_approval(customer_id=CUSTOMER, approval=approval, candidates=approved)

        # 5분 뒤 같은 승인이 재전송된다. 내용은 같고 서버 시각만 다르다.
        repository.record_approval(customer_id=CUSTOMER, approval=approval, candidates=approved)

        rule_items = [item for (_pk, sk), item in store.items.items() if sk.startswith("RULE#")]
        self.assertEqual(len(rule_items), len(_result().candidates))

    def test_a_different_rule_at_the_same_key_fails_closed(self) -> None:
        """같은 Rule key에 다른 내용이 있으면 승인된 Rule이 조용히 바뀌는 것이다."""
        repository, rule_ids = _approve(self.table)
        key = (f"CUSTOMER#{CUSTOMER}", f"RULE#{rule_ids[0]}#VERSION#{DOCUMENT.source_version}")
        self.table.items[key]["title"] = "A title nobody approved"

        approval, approved = approve_source(
            DOCUMENT,
            _result().candidates,
            approved_by="reviewer@example.com",
            approved_at="2026-09-03T00:00:00Z",
        )

        with self.assertRaises(RepositoryError):
            repository.record_approval(customer_id=CUSTOMER, approval=approval, candidates=approved)

    def test_an_unapproved_candidate_never_reaches_the_registry(self) -> None:
        repository = _repository(self.table)
        store_ingestion_item(self.table)
        repository.record_authoring_result(customer_id=CUSTOMER, result=_result())
        approval, _approved = approve_source(
            DOCUMENT,
            _result().candidates,
            approved_by="reviewer@example.com",
            approved_at="2026-09-03T00:00:00Z",
        )

        with self.assertRaisesRegex(RepositoryError, "only an approved candidate"):
            repository.record_approval(
                customer_id=CUSTOMER,
                approval=approval,
                candidates=_result().candidates,  # still CANDIDATE lifecycle
            )

    def test_an_approval_larger_than_the_transaction_budget_is_refused(self) -> None:
        """상한을 넘으면 승인이 원자적이지 않게 된다 — 일부 Rule만 Registry에 남는다."""
        repository = _repository(self.table)
        store_ingestion_item(self.table)
        approval, approved = approve_source(
            DOCUMENT,
            _result().candidates,
            approved_by="reviewer@example.com",
            approved_at="2026-09-03T00:00:00Z",
        )

        with self.assertRaisesRegex(RepositoryError, "must not record more than"):
            repository.record_approval(
                customer_id=CUSTOMER,
                approval=approval,
                candidates=approved * (MAX_RULES_PER_APPROVAL + 1),
            )
        self.assertFalse(any(sk.startswith("RULE#") for _pk, sk in self.table.items))


class ReadyManifestGateTest(unittest.TestCase):
    def test_approval_requires_the_authoring_run_to_be_ready(self) -> None:
        """읽은 시점과 쓰는 시점 사이에 manifest가 바뀌는 경우를 transaction 안에서 막는다."""
        table = FakeTable()
        repository = _repository(table)
        store_ingestion_item(table)
        repository.record_authoring_result(customer_id=CUSTOMER, result=_result())
        manifest_key = (
            f"CUSTOMER#{CUSTOMER}",
            f"POLICY_SOURCE#{DOCUMENT.source_id}#VERSION#{DOCUMENT.source_version}#AUTHORING",
        )
        table.items[manifest_key]["status"] = AuthoringRunStatus.PROCESSING.value

        approval, approved = approve_source(
            DOCUMENT,
            _result().candidates,
            approved_by="reviewer@example.com",
            approved_at="2026-09-03T00:00:00Z",
        )

        with self.assertRaises(RepositoryError):
            repository.record_approval(customer_id=CUSTOMER, approval=approval, candidates=approved)
        self.assertFalse(any(sk.startswith("RULE#") for _pk, sk in table.items))


class RuntimeIsolationTest(unittest.TestCase):
    def test_another_customer_cannot_read_an_approved_rule(self) -> None:
        table = FakeTable()
        _repository_, rule_ids = _approve(table)
        catalog = DynamoDbPolicyCatalog(table, customer_id="cust-002")  # type: ignore[arg-type]

        self.assertIsNone(catalog.get_rule(rule_ids[0], DOCUMENT.source_version))

    def test_a_partition_item_that_is_not_an_approved_rule_is_refused(self) -> None:
        """승인 경계를 거치지 않고 partition에 들어온 item은 Rule로 인정하지 않는다."""
        table = FakeTable()
        _repository_, rule_ids = _approve(table)
        key = (f"CUSTOMER#{CUSTOMER}", f"RULE#{rule_ids[0]}#VERSION#{DOCUMENT.source_version}")
        table.items[key]["lifecycle"] = RuleLifecycle.CANDIDATE.value
        catalog = DynamoDbPolicyCatalog(table, customer_id=CUSTOMER)  # type: ignore[arg-type]

        with self.assertRaisesRegex(StoredDataError, "is not approved"):
            catalog.get_rule(rule_ids[0], DOCUMENT.source_version)


class AdditiveApprovalTest(unittest.TestCase):
    """한 판본에 대한 승인은 더해진다.

    예전에는 두 번째 승인이 첫 번째와 다르면 조건부 write가 실패해 503으로 새어 나갔다 — 한
    문서에서 두 번째 Profile을 만드는 순간 게시가 막혔다. 빼는 것은 허용하지 않는다: 이미 승인된
    Rule은 게시된 Profile이 인용한다.
    """

    def setUp(self) -> None:
        self.table = FakeTable()
        self.repository = _repository(self.table)
        store_ingestion_item(self.table)
        self.repository.record_authoring_result(customer_id=CUSTOMER, result=_result())
        self.candidates = _result().candidates
        assert len(self.candidates) >= 2

    def _approve(self, subset, *, approved_at: str = "2026-09-03T00:00:00Z") -> None:
        approval, approved = approve_source(
            DOCUMENT, subset, approved_by="reviewer@example.com", approved_at=approved_at
        )
        self.repository.record_approval(
            customer_id=CUSTOMER, approval=approval, candidates=approved
        )

    def _stored_rule_ids(self) -> set[str]:
        item = self.table.items[
            (
                f"CUSTOMER#{CUSTOMER}",
                f"POLICY_SOURCE#{DOCUMENT.source_id}#VERSION#{DOCUMENT.source_version}#APPROVAL",
            )
        ]
        return {r["rule_id"] for r in item["approved_rules"]}  # type: ignore[index]

    def test_a_second_approval_adds_rules_instead_of_failing(self) -> None:
        first, second = self.candidates[0], self.candidates[1]
        self._approve((first,))

        self._approve((second,), approved_at="2026-09-04T00:00:00Z")

        self.assertEqual(self._stored_rule_ids(), {first.rule.rule_id, second.rule.rule_id})
        rule_keys = {sk for (_pk, sk) in self.table.items if sk.startswith("RULE#")}
        self.assertEqual(len(rule_keys), 2)

    def test_the_earlier_rules_are_never_removed(self) -> None:
        """부분집합을 다시 승인해도 앞서 승인된 Rule은 그대로다."""
        first, second = self.candidates[0], self.candidates[1]
        self._approve((first, second))

        self._approve((first,), approved_at="2026-09-04T00:00:00Z")

        self.assertEqual(self._stored_rule_ids(), {first.rule.rule_id, second.rule.rule_id})

    def test_publication_sees_the_union(self) -> None:
        first, second = self.candidates[0], self.candidates[1]
        self._approve((first,))
        self._approve((second,), approved_at="2026-09-04T00:00:00Z")

        candidates, (approval,), _ = self.repository.load_publication(
            customer_id=CUSTOMER,
            source_id=DOCUMENT.source_id,
            source_version=DOCUMENT.source_version,
        )

        self.assertEqual(
            {c.rule.rule_id for c in candidates}, {first.rule.rule_id, second.rule.rule_id}
        )
        self.assertEqual(len(approval.approved_rules), 2)

    def test_an_approval_bound_to_a_different_original_is_refused(self) -> None:
        """binding이 다르면 같은 판본이 아니다. 다른 문서에 대한 승인을 보탤 수는 없다."""
        from dataclasses import replace

        first, second = self.candidates[0], self.candidates[1]
        self._approve((first,))
        approval, approved = approve_source(
            DOCUMENT,
            (second,),
            approved_by="reviewer@example.com",
            approved_at="2026-09-04T00:00:00Z",
        )
        relabelled = replace(approval, content_sha256="somebody-elses-bytes")

        with self.assertRaises(ApprovalConflictError):
            self.repository.record_approval(
                customer_id=CUSTOMER, approval=relabelled, candidates=approved
            )


class RetireProfileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.table = FakeTable()
        self.repository, _ = _approve(self.table)
        candidates, approvals, sources = self.repository.load_publication(
            customer_id=CUSTOMER,
            source_id=DOCUMENT.source_id,
            source_version=DOCUMENT.source_version,
        )
        from apps.backend.policy.ingestion import publish_profile

        profile = publish_profile(
            policy_profile_id="profile-1",
            version="v1",
            candidates=candidates,
            approvals=approvals,
            sources=sources,
        )
        self.repository.record_profile(
            customer_id=CUSTOMER,
            profile=profile,
            published_by="admin",
            published_at="2026-09-03T00:00:00Z",
        )

    def test_retiring_removes_the_pointer_and_keeps_the_version(self) -> None:
        """Assessment가 판본을 고정하고 보고서가 그것을 읽는다. 사라지는 것은 "현재" 자리뿐이다."""
        self.repository.retire_profile(
            customer_id=CUSTOMER, policy_profile_id="profile-1", retired_by="admin"
        )

        keys = {sk for (_pk, sk) in self.table.items}
        self.assertNotIn("POLICY_PROFILE#profile-1", keys)
        self.assertIn("POLICY_PROFILE#profile-1#VERSION#v1", keys)
        events = [
            item
            for (_pk, sk), item in self.table.items.items()
            if sk.startswith("AUDIT#") and item.get("event_type") == "POLICY_PROFILE_RETIRED"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(self.repository.list_profiles(customer_id=CUSTOMER), ())

    def test_retiring_an_unknown_profile_is_not_found(self) -> None:
        from packages.common.errors import PolicyProfileNotFound

        with self.assertRaises(PolicyProfileNotFound):
            self.repository.retire_profile(
                customer_id=CUSTOMER, policy_profile_id="nope", retired_by="admin"
            )


if __name__ == "__main__":
    unittest.main()
