"""Storing an authoring run must never leave a half-written candidate set readable.

후보를 여러 item에 나눠 쓰면 "일부만 써진 상태"가 생긴다. manifest가 그 경계다 — Review와
Approval은 READY manifest만 읽고, READY 전환 전에 저장된 내용을 digest까지 대조한다. 개수만
세면 "다른 후보가 같은 개수만큼 써진" 경우를 통과시킨다.

같은 source version을 다른 extractor·prompt·Catalog로 재추출하면 identity가 달라지므로
재시도가 아니라 **다른 추출**로 보아 fail-closed한다. 조용히 덮어쓰면, 리뷰어가 보던 후보 집합이
설명 없이 바뀐다.
"""

import unittest
from collections.abc import Mapping
from io import BytesIO

from apps.backend.policy.authoring import (
    ExtractorIdentity,
    FakePolicyCandidateExtractor,
    NormalizedArtifactReader,
    extract_policy_candidates,
)
from apps.backend.policy.control_catalog import MVP_CONTROL_CATALOG
from apps.backend.repositories.errors import RepositoryError
from apps.backend.repositories.policy_approval import (
    MAX_AUTHORING_RESULTS_PER_RUN,
    DynamoDbPolicyApprovalRepository,
)
from packages.contracts import AuthoringRunStatus, PolicyAuthoringResult
from tests.authoring_fixtures import UNIT_TEXTS, normalized_artifact_bytes, ready_document
from tests.unit.test_policy_authoring_pipeline import automatable, manual, unsupported

CUSTOMER = "cust-001"
TABLE = "governance"
DOCUMENT = ready_document()


class FakeTable:
    """A minimal DynamoDB table: conditional puts, consistent gets, prefix queries."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, object]] = {}
        self.query_calls = 0

    # --- write side, driven through the transaction client ---
    def transact_write_items(self, **kwargs: object) -> None:
        """Apply the whole transaction or none of it, honouring both condition forms.

        `ConditionCheck`를 무시하는 fake는 조건이 실제로 걸렸는지 증명하지 못한다 — 조건을
        지워도 테스트가 통과한다. 그래서 여기서 실제로 평가한다.
        """
        entries = list(kwargs["TransactItems"])  # type: ignore[arg-type]
        staged: dict[tuple[str, str], dict[str, object]] = {}
        for entry in entries:
            check = entry.get("ConditionCheck")
            if check is not None:
                self._require_condition(check)
                continue
            put = entry.get("Put")
            if put is None:
                continue
            item = _unmarshal(put["Item"])
            key = (str(item["PK"]), str(item["SK"]))
            expression = put.get("ConditionExpression")
            if expression is not None:
                self._require_put_condition(key, put, str(expression))
            staged[key] = item
        self.items.update(staged)

    def _require_put_condition(
        self, key: tuple[str, str], put: Mapping[str, object], expression: str
    ) -> None:
        """Distinguish the two condition forms the repository writes.

        `attribute_not_exists`는 "새로 만든다"이고 `current_version = :expected`는 "이 판본을
        교체한다"다. 둘을 같게 처리하는 fake는 pointer 교체 경로를 전혀 검사하지 못한다.
        """
        if "attribute_not_exists" in expression:
            if key in self.items:
                raise _ConditionalCheckFailed()
            return
        existing = self.items.get(key)
        if existing is None:
            raise _ConditionalCheckFailed()
        _require_equalities(existing, put, expression)

    def _require_condition(self, check: Mapping[str, object]) -> None:
        """Evaluate the `field = :value` conjunctions the repository actually writes."""
        key = _unmarshal(check["Key"])
        item = self.items.get((str(key["PK"]), str(key["SK"])))
        if item is None:
            raise _ConditionalCheckFailed()
        _require_equalities(item, check, str(check["ConditionExpression"]))

    # --- read side ---
    def get_item(self, **kwargs: object) -> dict[str, object]:
        key = kwargs["Key"]
        found = self.items.get((str(key["PK"]), str(key["SK"])))  # type: ignore[index]
        return {} if found is None else {"Item": dict(found)}

    def query(self, **kwargs: object) -> dict[str, object]:
        self.query_calls += 1
        values = kwargs["ExpressionAttributeValues"]
        pk, prefix = str(values[":pk"]), str(values[":prefix"])  # type: ignore[index]
        matched = [
            dict(item)
            for (item_pk, sk), item in sorted(self.items.items())
            if item_pk == pk and sk.startswith(prefix)
        ]
        return {"Items": matched}


def _require_equalities(
    item: Mapping[str, object], expression_source: Mapping[str, object], expression: str
) -> None:
    names = expression_source.get("ExpressionAttributeNames") or {}
    values = _unmarshal(expression_source.get("ExpressionAttributeValues") or {})
    for clause in expression.split(" AND "):
        field, _, placeholder = (part.strip() for part in clause.partition("="))
        field = names.get(field, field)  # type: ignore[union-attr]
        if item.get(field) != values.get(placeholder):
            raise _ConditionalCheckFailed()


class _ConditionalCheckFailed(Exception):
    def __init__(self) -> None:
        super().__init__("ConditionalCheckFailedException")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


def _unmarshal(value: object) -> dict[str, object]:
    """Reverse `marshal_item` far enough for these assertions."""
    assert isinstance(value, dict)
    return {name: _unmarshal_value(entry) for name, entry in value.items()}


def _unmarshal_value(entry: object) -> object:
    if not isinstance(entry, dict) or len(entry) != 1:
        return entry
    ((tag, payload),) = entry.items()
    if tag == "S":
        return payload
    if tag == "N":
        text = str(payload)
        return int(text) if "." not in text else float(text)
    if tag == "BOOL":
        return payload
    if tag == "NULL":
        return None
    if tag == "L":
        return [_unmarshal_value(item) for item in payload]  # type: ignore[union-attr]
    if tag == "M":
        return {name: _unmarshal_value(item) for name, item in payload.items()}  # type: ignore[union-attr]
    return entry


class _Source:
    def get_object(self, **_kwargs: object) -> dict[str, object]:
        return {"Body": BytesIO(normalized_artifact_bytes())}


def _result(*, prompt_version: str = "policy-authoring/fake") -> PolicyAuthoringResult:
    identity = ExtractorIdentity(
        extractor_id="fake-policy-candidate-extractor",
        extractor_version="1.0.0",
        model_id="fake",
        model_version="1",
        prompt_version=prompt_version,
    )
    return extract_policy_candidates(
        customer_id=CUSTOMER,
        document=DOCUMENT,
        artifact_reader=NormalizedArtifactReader(reader=_Source(), bucket="artifacts"),  # type: ignore[arg-type]
        extractor=FakePolicyCandidateExtractor(
            (automatable(), manual(), unsupported()), identity=identity
        ),
        catalog=MVP_CONTROL_CATALOG,
        authoring_run_id="run-1",
        requested_at="2026-09-03T00:00:00+00:00",
    )


def store_ingestion_item(table: FakeTable, *, customer_id: str = CUSTOMER) -> None:
    """Seed the READY ingestion record every approval write conditions on.

    승인은 원본 바인딩(`artifact_id`/`s3_version_id`/`content_sha256`)이 READY 상태로 저장돼
    있을 때만 성립한다. 그 item이 없으면 조건 검사가 걸려 승인 자체가 실패한다 — 그것이 이
    fake가 재현해야 하는 실제 동작이다.
    """
    sort_key = f"POLICY_INGESTION#{DOCUMENT.source_id}#VERSION#{DOCUMENT.source_version}"
    table.items[(f"CUSTOMER#{customer_id}", sort_key)] = {
        "PK": f"CUSTOMER#{customer_id}",
        "SK": sort_key,
        "customer_id": customer_id,
        **DOCUMENT.to_dict(),
    }


def _repository(table: FakeTable) -> DynamoDbPolicyApprovalRepository:
    return DynamoDbPolicyApprovalRepository(
        table_name=TABLE,
        transaction_client=table,  # type: ignore[arg-type]
        table=table,  # type: ignore[arg-type]
    )


class WriteOrderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.table = FakeTable()
        self.repository = _repository(self.table)

    def test_a_run_is_stored_as_a_manifest_plus_one_item_per_outcome(self) -> None:
        manifest = self.repository.record_authoring_result(customer_id=CUSTOMER, result=_result())

        suffixes = sorted(
            sk.split("#VERSION#")[-1] for _pk, sk in self.table.items if "#VERSION#" in sk
        )
        self.assertIs(manifest.status, AuthoringRunStatus.READY)
        self.assertEqual(manifest.counts["accepted"], 1)
        self.assertEqual(manifest.counts["manual"], 1)
        self.assertEqual(manifest.counts["unsupported"], 1)
        self.assertEqual(sum("#CANDIDATE#" in suffix for suffix in suffixes), 2)
        self.assertEqual(sum("#UNSUPPORTED#" in suffix for suffix in suffixes), 1)

    def test_the_manifest_ends_ready_and_carries_the_result_digest(self) -> None:
        result = _result()

        manifest = self.repository.record_authoring_result(customer_id=CUSTOMER, result=result)

        self.assertEqual(manifest.result_digest, result.result_digest)
        stored = self.repository.load_authoring_manifest(
            customer_id=CUSTOMER,
            source_id=DOCUMENT.source_id,
            source_version=DOCUMENT.source_version,
        )
        self.assertIs(stored.status, AuthoringRunStatus.READY)

    def test_a_retry_of_the_same_run_is_absorbed(self) -> None:
        """worker는 at-least-once다. 같은 실행의 재시도는 성공으로 흡수한다."""
        result = _result()

        first = self.repository.record_authoring_result(customer_id=CUSTOMER, result=result)
        before = dict(self.table.items)
        second = self.repository.record_authoring_result(customer_id=CUSTOMER, result=result)

        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(self.table.items, before)

    def test_a_different_extraction_of_the_same_version_fails_closed(self) -> None:
        """다른 prompt로 다시 추출한 결과가 조용히 덮어쓰면 리뷰 중인 후보 집합이 바뀐다."""
        self.repository.record_authoring_result(customer_id=CUSTOMER, result=_result())

        with self.assertRaisesRegex(RepositoryError, "a different extraction already exists"):
            self.repository.record_authoring_result(
                customer_id=CUSTOMER, result=_result(prompt_version="policy-authoring/v2")
            )

    def test_an_oversized_run_is_refused_before_anything_is_written(self) -> None:
        """상한이 없으면 문서 하나가 partition을 채우고, 그 중간에 실패하면 어디까지 저장됐는지
        말할 수 없다. 그래서 manifest를 쓰기 **전에** 거절한다."""
        oversized = PolicyAuthoringResult(
            document=DOCUMENT,
            unsupported=tuple(
                unsupported(requirement=f"Out of scope requirement number {index}.")
                for index in range(MAX_AUTHORING_RESULTS_PER_RUN + 1)
            ),
            provenance=_result().provenance,
        )

        with self.assertRaisesRegex(RepositoryError, "must not produce more than"):
            self.repository.record_authoring_result(customer_id=CUSTOMER, result=oversized)

        self.assertEqual(self.table.items, {})


class ReadGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.table = FakeTable()
        self.repository = _repository(self.table)

    def test_review_reads_only_the_approvable_candidates(self) -> None:
        self.repository.record_authoring_result(customer_id=CUSTOMER, result=_result())
        self._store_ingestion_item()

        _document, candidates = self.repository.load_review(
            customer_id=CUSTOMER,
            source_id=DOCUMENT.source_id,
            source_version=DOCUMENT.source_version,
        )

        control_keys = sorted(candidate.rule.control_key or "" for candidate in candidates)
        self.assertEqual(
            control_keys, ["ORGANIZATIONAL_CONTROL_MANUAL_REVIEW", "S3_BLOCK_PUBLIC_ACCESS"]
        )

    def test_review_refuses_a_run_that_is_not_ready(self) -> None:
        """일부만 쓰인 후보 집합을 완전한 것으로 읽지 않는다."""
        self.repository.record_authoring_result(customer_id=CUSTOMER, result=_result())
        self._store_ingestion_item()
        key = (
            f"CUSTOMER#{CUSTOMER}",
            f"POLICY_SOURCE#{DOCUMENT.source_id}#VERSION#{DOCUMENT.source_version}#AUTHORING",
        )
        self.table.items[key]["status"] = AuthoringRunStatus.PROCESSING.value
        self.table.items[key]["result_digest"] = None
        self.table.items[key]["counts"] = {}

        with self.assertRaisesRegex(RepositoryError, "not ready for review"):
            self.repository.load_review(
                customer_id=CUSTOMER,
                source_id=DOCUMENT.source_id,
                source_version=DOCUMENT.source_version,
            )

    def test_restored_candidates_keep_their_execution_semantics(self) -> None:
        """저장을 왕복한 뒤 실행 의미를 잃으면 승인된 Rule이 legacy Rule로 평가된다."""
        result = _result()
        self.repository.record_authoring_result(customer_id=CUSTOMER, result=result)
        self._store_ingestion_item()

        _document, candidates = self.repository.load_review(
            customer_id=CUSTOMER,
            source_id=DOCUMENT.source_id,
            source_version=DOCUMENT.source_version,
        )

        restored = {candidate.rule.rule_id: candidate.rule for candidate in candidates}
        for original in result.candidates:
            with self.subTest(rule=original.rule.rule_id):
                self.assertEqual(restored[original.rule.rule_id], original.rule)
                self.assertFalse(restored[original.rule.rule_id].is_legacy)

    def test_the_stored_results_read_path_requires_a_ready_manifest(self) -> None:
        self.repository.record_authoring_result(customer_id=CUSTOMER, result=_result())

        manifest, items = self.repository.load_authoring_results(
            customer_id=CUSTOMER,
            source_id=DOCUMENT.source_id,
            source_version=DOCUMENT.source_version,
        )

        self.assertIs(manifest.status, AuthoringRunStatus.READY)
        self.assertEqual(len(items), 3)

    def _store_ingestion_item(self) -> None:
        store_ingestion_item(self.table)


class TextContainmentTest(unittest.TestCase):
    def test_no_stored_item_carries_a_verbatim_source_sentence(self) -> None:
        """저장되는 문장은 모델이 쓴 재진술이지 정규화 artifact의 원문이 아니다.

        원문은 `ExtractionUnit` 안에만 존재하고 그 타입에는 직렬화가 없다. 이 검사는 그 성질이
        저장 경로 끝까지 유지되는지 확인한다.
        """
        table = FakeTable()
        _repository(table).record_authoring_result(customer_id=CUSTOMER, result=_result())

        rendered = repr(table.items)
        for _locator, _kind, text in UNIT_TEXTS:
            with self.subTest(text=text[:32]):
                self.assertNotIn(text, rendered)


if __name__ == "__main__":
    unittest.main()
