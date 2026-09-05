"""A finding beyond the first DynamoDB page is still found.

`query`는 페이지당 1 MB를 읽은 뒤에야 FilterExpression을 적용한다. 평가 이력이 그보다 커지자
뒤쪽 평가의 finding이 "없음"으로 읽혀 조치 요청이 503이었다(라이브, ISMS-P 기준선 실행 뒤).
이 파일은 페이지를 끝까지 따라가는 것과, 같은 위반의 최신 발생을 페이지에 상관없이 고르는 것을
고정한다.
"""

import unittest
from collections.abc import Mapping

from apps.backend.repositories.ports import RepositoryError, StoredDataError
from apps.backend.repositories.remediation_context import DynamoDbRemediationContextReader

CUSTOMER = "cust-001"
PK = f"CUSTOMER#{CUSTOMER}"
FINDING = "finding-7dc078795f230bc5b84de733"


def _finding_item(assessment_id: str, evaluated_at: str) -> dict[str, object]:
    return {
        "PK": PK,
        "SK": f"ASSESSMENT#{assessment_id}#FINDING#{FINDING}",
        "entity_type": "FINDING",
        "customer_id": CUSTOMER,
        "assessment_id": assessment_id,
        "finding_id": FINDING,
        "resource_id": "tfsbx-20260903-7f3a-a91c",
        "rule_id": "ISMSP-S3_BLOCK_PUBLIC_ACCESS",
        "rule_version": "2023-10-31.r2",
        "perspective": "AWS_ACTUAL",
        "status": "FAIL",
        "severity": "CRITICAL",
        "score": 0,
        "rationale": "Block public access is not fully enabled.",
        "evidence_references": ["aws:s3:bucket/tfsbx-20260903-7f3a-a91c#read-resource"],
        "assessed_commit_sha": "a3e6467eebe922bd2411884806ae794761b9bc87",
        "evaluated_at": evaluated_at,
    }


def _filler(assessment_id: str, index: int) -> dict[str, object]:
    return {
        "PK": PK,
        "SK": f"ASSESSMENT#{assessment_id}#RESULT#{index:04d}",
        "entity_type": "ASSESSMENT_RESULT",
        "customer_id": CUSTOMER,
        "assessment_id": assessment_id,
    }


class PagingTable:
    """A table whose query returns fixed-size pages and filters *after* paging, like DynamoDB."""

    def __init__(self, items: list[dict[str, object]], page_size: int) -> None:
        self.items = sorted(items, key=lambda i: str(i["SK"]))
        self.page_size = page_size
        self.pages_served = 0

    def get_item(self, **kwargs: object) -> Mapping[str, object]:
        return {}

    def query(self, **kwargs: object) -> Mapping[str, object]:
        self.pages_served += 1
        values = kwargs["ExpressionAttributeValues"]
        assert isinstance(values, dict)
        prefix = str(values[":prefix"])
        rows = [i for i in self.items if str(i["SK"]).startswith(prefix)]
        start = 0
        exclusive = kwargs.get("ExclusiveStartKey")
        if isinstance(exclusive, dict):
            start = next(i for i, row in enumerate(rows) if row["SK"] == exclusive["SK"]) + 1
        page = rows[start : start + self.page_size]
        filtered = [
            row
            for row in page
            if row.get("entity_type") == values[":finding"]
            and row.get("finding_id") == values[":fid"]
        ]
        response: dict[str, object] = {"Items": filtered}
        if start + self.page_size < len(rows):
            response["LastEvaluatedKey"] = {"PK": page[-1]["PK"], "SK": page[-1]["SK"]}
        return response


class FindingPagingTest(unittest.TestCase):
    def test_a_finding_on_the_last_page_is_found(self) -> None:
        """첫 페이지에 없다고 없는 것이 아니다."""
        items = [_filler("asm-0001", i) for i in range(9)] + [
            _finding_item("asm-0002", "2026-09-05T05:00:00Z")
        ]
        table = PagingTable(items, page_size=4)
        reader = DynamoDbRemediationContextReader(table)  # type: ignore[arg-type]

        finding, assessment_id = reader._load_finding(CUSTOMER, FINDING)

        self.assertEqual(finding.finding_id, FINDING)
        self.assertEqual(assessment_id, "asm-0002")
        self.assertEqual(table.pages_served, 3)

    def test_the_newest_occurrence_wins_even_when_it_is_on_a_later_page(self) -> None:
        items = (
            [_finding_item("asm-0001", "2026-09-05T03:00:00Z")]
            + [_filler("asm-0001", i) for i in range(6)]
            + [_finding_item("asm-0009", "2026-09-05T05:00:00Z")]
        )
        table = PagingTable(items, page_size=3)
        reader = DynamoDbRemediationContextReader(table)  # type: ignore[arg-type]

        _finding, assessment_id = reader._load_finding(CUSTOMER, FINDING)

        self.assertEqual(assessment_id, "asm-0009")

    def test_a_missing_finding_is_still_not_found_after_every_page(self) -> None:
        table = PagingTable([_filler("asm-0001", i) for i in range(5)], page_size=2)
        reader = DynamoDbRemediationContextReader(table)  # type: ignore[arg-type]

        with self.assertRaisesRegex(StoredDataError, "not found"):
            reader._load_finding(CUSTOMER, FINDING)
        self.assertEqual(table.pages_served, 3)

    def test_pagination_that_never_ends_is_refused(self) -> None:
        class Endless(PagingTable):
            def query(self, **kwargs: object) -> Mapping[str, object]:
                self.pages_served += 1
                return {"Items": [], "LastEvaluatedKey": {"PK": PK, "SK": "ASSESSMENT#loop"}}

        reader = DynamoDbRemediationContextReader(Endless([], page_size=1))  # type: ignore[arg-type]

        with self.assertRaisesRegex(RepositoryError, "did not terminate"):
            reader._load_finding(CUSTOMER, FINDING)


if __name__ == "__main__":
    unittest.main()
