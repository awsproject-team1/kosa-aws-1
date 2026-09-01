"""Regression tests for parser hardening found in the PR #15 self-review.

네 건 모두 리뷰 전에는 통과하던 입력이다. 각 테스트는 그 입력이 이제 fail-closed로 끝나거나
사람 검토를 거치게 됐음을 고정한다.
"""

import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "unit"))

from ingestion_fixtures import (  # noqa: E402
    build_docx,
    build_docx_part,
    build_xlsx,
    entity_bomb_document,
    inline_cell,
    paragraph,
    sheet_row,
)

from apps.backend.policy.ingestion import UploadedPolicyOriginal, normalize_upload  # noqa: E402
from apps.backend.policy.ingestion.normalization import (  # noqa: E402
    MAX_UNITS,
    DocumentParseError,
)
from apps.backend.policy.ingestion.parsers.ooxml import parse_docx, parse_xlsx  # noqa: E402
from packages.contracts import (  # noqa: E402
    FORMAT_MEDIA_TYPES,
    ExtractionWarningCode,
    IngestionFailureCode,
    IngestionStatus,
    PolicySourceFormat,
)

DOCX_MEDIA_TYPE = FORMAT_MEDIA_TYPES[PolicySourceFormat.DOCX]


def _original(payload: bytes, media_type: str, filename: str) -> UploadedPolicyOriginal:
    return UploadedPolicyOriginal(
        source_id="internal-cloud-security-checklist",
        source_version="2026-09-01",
        artifact_id="artifact-001",
        s3_version_id="s3-version-001",
        content_sha256=hashlib.sha256(payload).hexdigest(),
        filename=filename,
        declared_media_type=media_type,
        byte_size=len(payload),
    )


class XmlEntityExpansionTest(unittest.TestCase):
    """zip 한도는 XML 엔티티 확장을 막지 못한다. 증폭이 압축 해제 뒤에 일어나기 때문이다."""

    def test_rejects_an_entity_bomb_that_zip_limits_cannot_catch(self) -> None:
        payload = build_docx_part(entity_bomb_document(6))

        # 리뷰 전에는 이 344바이트 업로드가 100만 자짜리 unit을 만들었다.
        self.assertLess(len(payload), 1024)
        with self.assertRaises(DocumentParseError) as raised:
            parse_docx(payload)
        self.assertEqual(raised.exception.failure_code, IngestionFailureCode.XML_DTD_NOT_ALLOWED)

    def test_the_pipeline_reports_the_dtd_refusal_as_a_failure_code(self) -> None:
        payload = build_docx_part(entity_bomb_document(6))

        outcome = normalize_upload(_original(payload, DOCX_MEDIA_TYPE, "policy.docx"), payload)
        self.assertEqual(outcome.document.status, IngestionStatus.FAILED)
        self.assertEqual(outcome.document.failure_code, IngestionFailureCode.XML_DTD_NOT_ALLOWED)
        self.assertIsNone(outcome.normalized_payload)

    def test_rejects_a_dtd_in_a_worksheet_part_too(self) -> None:
        """DOCX만 막고 XLSX를 놓치면 같은 구멍이 남는다."""
        workbook = build_xlsx(
            sheets=[("Security", sheet_row(1, inline_cell("A", 1, "5.2.1")))],
            extra={"xl/sharedStrings.xml": entity_bomb_document(4)},
        )

        with self.assertRaises(DocumentParseError) as raised:
            parse_xlsx(workbook)
        self.assertEqual(raised.exception.failure_code, IngestionFailureCode.XML_DTD_NOT_ALLOWED)

    def test_rejects_a_utf16_encoded_dtd(self) -> None:
        payload = build_docx_part(entity_bomb_document(4).encode("utf-16"))

        with self.assertRaises(DocumentParseError) as raised:
            parse_docx(payload)
        self.assertEqual(raised.exception.failure_code, IngestionFailureCode.XML_DTD_NOT_ALLOWED)

    def test_a_comment_before_the_dtd_cannot_hide_it(self) -> None:
        document = entity_bomb_document(4).replace(
            '<?xml version="1.0"?>',
            '<?xml version="1.0"?>\n<!-- <fake> -->',
            1,
        )

        with self.assertRaises(DocumentParseError) as raised:
            parse_docx(build_docx_part(document))
        self.assertEqual(raised.exception.failure_code, IngestionFailureCode.XML_DTD_NOT_ALLOWED)

    def test_an_ordinary_document_without_a_dtd_still_parses(self) -> None:
        payload = build_docx(paragraph("Controls", style="Heading1") + paragraph("A rule."))

        self.assertEqual(len(parse_docx(payload).units), 2)

    def test_an_ordinary_utf16_document_still_parses(self) -> None:
        document = (
            '<?xml version="1.0" encoding="UTF-16"?>'
            '<document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body>'
            f"{paragraph('Controls', style='Heading1')}{paragraph('A rule.')}"
            "</w:body></document>"
        )

        self.assertEqual(len(parse_docx(build_docx_part(document.encode("utf-16"))).units), 2)


class UnitBudgetTest(unittest.TestCase):
    """unit 상한은 Parser가 아니라 `DocumentBuilder`가 강제해야 형식마다 빠뜨리지 않는다."""

    def test_a_docx_beyond_the_unit_budget_is_refused(self) -> None:
        payload = build_docx("".join(paragraph(f"rule {index}") for index in range(MAX_UNITS + 50)))

        with self.assertRaises(DocumentParseError) as raised:
            parse_docx(payload)
        self.assertEqual(
            raised.exception.failure_code, IngestionFailureCode.EXPANSION_LIMIT_EXCEEDED
        )

    def test_a_workbook_beyond_the_unit_budget_is_refused(self) -> None:
        rows = "".join(
            sheet_row(index, inline_cell("A", index, f"rule {index}"))
            for index in range(1, MAX_UNITS + 50)
        )

        with self.assertRaises(DocumentParseError) as raised:
            parse_xlsx(build_xlsx(sheets=[("Security", rows)]))
        self.assertEqual(
            raised.exception.failure_code, IngestionFailureCode.EXPANSION_LIMIT_EXCEEDED
        )

    def test_a_document_within_the_budget_is_unaffected(self) -> None:
        payload = build_docx("".join(paragraph(f"rule {index}") for index in range(50)))

        self.assertEqual(len(parse_docx(payload).units), 50)


class AmbiguousWorksheetCoordinateTest(unittest.TestCase):
    """같은 행 번호가 둘이면 locator 하나가 두 행을 가리킨다. 조용히 덮어쓰면 근거가 사라진다."""

    def test_refuses_a_worksheet_that_declares_a_row_twice(self) -> None:
        workbook = build_xlsx(
            sheets=[
                (
                    "Security",
                    sheet_row(1, inline_cell("A", 1, "first policy"))
                    + sheet_row(1, inline_cell("A", 1, "overwriting policy")),
                )
            ]
        )

        with self.assertRaises(DocumentParseError) as raised:
            parse_xlsx(workbook)
        self.assertEqual(raised.exception.failure_code, IngestionFailureCode.AMBIGUOUS_LOCATOR)

    def test_distinct_row_numbers_are_unaffected(self) -> None:
        workbook = build_xlsx(
            sheets=[
                (
                    "Security",
                    sheet_row(1, inline_cell("A", 1, "first policy"))
                    + sheet_row(2, inline_cell("A", 2, "second policy")),
                )
            ]
        )

        self.assertEqual(
            [unit.locator for unit in parse_xlsx(workbook).units],
            ["sheet/security/row/1", "sheet/security/row/2"],
        )

    def test_refuses_duplicate_cell_coordinates(self) -> None:
        workbook = build_xlsx(
            sheets=[
                (
                    "Security",
                    sheet_row(
                        1,
                        inline_cell("A", 1, "first policy")
                        + inline_cell("A", 1, "overwriting policy"),
                    ),
                )
            ]
        )

        with self.assertRaises(DocumentParseError) as raised:
            parse_xlsx(workbook)
        self.assertEqual(raised.exception.failure_code, IngestionFailureCode.AMBIGUOUS_LOCATOR)

    def test_refuses_a_cell_that_references_a_different_row(self) -> None:
        workbook = build_xlsx(
            sheets=[
                (
                    "Security",
                    sheet_row(1, inline_cell("A", 2, "misplaced policy")),
                )
            ]
        )

        with self.assertRaises(DocumentParseError) as raised:
            parse_xlsx(workbook)
        self.assertEqual(raised.exception.failure_code, IngestionFailureCode.AMBIGUOUS_LOCATOR)


class InferredDelimiterNeedsReviewTest(unittest.TestCase):
    """delimiter를 추론했다는 사실 자체가 사람이 확인해야 할 신호다."""

    def test_an_inferred_delimiter_does_not_reach_ready(self) -> None:
        payload = b"control;requirement\n5.2.1;public buckets are prohibited\n"

        outcome = normalize_upload(_original(payload, "text/csv", "policy.csv"), payload)
        self.assertIn(ExtractionWarningCode.DELIMITER_INFERRED, outcome.document.warnings)
        self.assertEqual(outcome.document.status, IngestionStatus.REVIEW_REQUIRED)
        self.assertFalse(outcome.document.is_approvable)

    def test_a_plain_comma_separated_file_is_still_ready(self) -> None:
        payload = b"control,requirement\n5.2.1,public buckets are prohibited\n"

        outcome = normalize_upload(_original(payload, "text/csv", "policy.csv"), payload)
        self.assertNotIn(ExtractionWarningCode.DELIMITER_INFERRED, outcome.document.warnings)
        self.assertEqual(outcome.document.status, IngestionStatus.READY)


if __name__ == "__main__":
    unittest.main()
