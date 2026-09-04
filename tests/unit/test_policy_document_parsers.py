"""Tests for the customer policy document parsers and their locator schemes."""

import json
import unittest

from ingestion_fixtures import (
    build_docx,
    build_xlsx,
    inline_cell,
    paragraph,
    shared_cell,
    sheet_row,
    table,
)

from apps.backend.policy.ingestion.normalization import (
    DocumentParseError,
    normalize_text,
    text_sha256,
)
from apps.backend.policy.ingestion.parsers.ooxml import parse_docx, parse_xlsx
from apps.backend.policy.ingestion.parsers.text import parse_csv, parse_markdown, parse_plain_text
from packages.contracts import DocumentUnitKind, ExtractionWarningCode, IngestionFailureCode

MARKDOWN = b"""# Access Control

Accounts must use least privilege.

- [ ] **5.2.1.** Public buckets are prohibited.

## Storage

Buckets encrypt objects at rest.
"""


class MarkdownParserTest(unittest.TestCase):
    def test_locators_follow_the_heading_path(self) -> None:
        parsed = parse_markdown(MARKDOWN)

        locators = [unit.locator for unit in parsed.units]
        self.assertEqual(
            locators,
            [
                "heading/access-control",
                "heading/access-control/item/1",
                "heading/access-control/item/2",
                "heading/access-control/storage",
                "heading/access-control/storage/item/1",
            ],
        )

    def test_classifies_sections_and_list_items(self) -> None:
        kinds = {unit.locator: unit.kind for unit in parse_markdown(MARKDOWN).units}

        self.assertEqual(kinds["heading/access-control"], DocumentUnitKind.SECTION)
        self.assertEqual(kinds["heading/access-control/item/1"], DocumentUnitKind.PARAGRAPH)
        self.assertEqual(kinds["heading/access-control/item/2"], DocumentUnitKind.LIST_ITEM)

    def test_a_tight_list_yields_one_unit_per_top_level_item(self) -> None:
        """Company standards are usually tight bullet lists; each bullet is one requirement.

        1.0.1 flushed a block only on a blank line, so `- a / - b / - c` on consecutive
        lines became a single unit and three requirements shared one locator and one
        digest. Indented continuation lines and nested bullets stay with their parent item.
        """
        source = "\n".join(
            (
                "# Standard",
                "",
                "## Network",
                "",
                "- Servers use no public IP.",
                "  Continuation of the first item.",
                "  - nested check",
                "- Databases are not publicly accessible.",
                "- Only approved ports are open.",
                "",
            )
        ).encode("utf-8")
        document = parse_markdown(source)
        network = [unit for unit in document.units if "/network/item/" in unit.locator]
        self.assertEqual(
            [unit.locator for unit in network],
            [
                "heading/standard/network/item/1",
                "heading/standard/network/item/2",
                "heading/standard/network/item/3",
            ],
        )
        self.assertTrue(all(unit.kind is DocumentUnitKind.LIST_ITEM for unit in network))
        units = json.loads(document.normalized_payload.decode("utf-8"))["units"]
        first = next(u["text"] for u in units if u["locator"] == "heading/standard/network/item/1")
        self.assertIn("Continuation of the first item.", first)
        self.assertIn("nested check", first)

    def test_hashes_the_normalized_unit_text(self) -> None:
        units = {unit.locator: unit for unit in parse_markdown(MARKDOWN).units}

        unit = units["heading/access-control/item/1"]
        self.assertEqual(unit.text_sha256, text_sha256("Accounts must use least privilege."))
        self.assertEqual(unit.text_length, len("Accounts must use least privilege."))

    def test_reformatting_does_not_change_the_digest(self) -> None:
        """정규화가 줄바꿈·들여쓰기 차이를 흡수해야 재조판 뒤에도 Evidence가 유지된다."""
        reformatted = MARKDOWN.replace(b"# Access Control\n", b"# Access Control  \n").replace(
            b"Accounts must use least privilege.",
            b"Accounts must   use  least privilege.",
        )

        original = {unit.locator: unit.text_sha256 for unit in parse_markdown(MARKDOWN).units}
        after = {unit.locator: unit.text_sha256 for unit in parse_markdown(reformatted).units}
        self.assertEqual(original, after)

    def test_a_code_fence_does_not_open_a_section(self) -> None:
        payload = b"# Policy\n\n```\n# not a heading\n```\n"

        locators = [unit.locator for unit in parse_markdown(payload).units]
        self.assertEqual(locators, ["heading/policy", "heading/policy/item/1"])

    def test_rejects_a_document_without_extractable_text(self) -> None:
        with self.assertRaises(DocumentParseError) as raised:
            parse_markdown(b"   \n\n \n")

        self.assertEqual(raised.exception.failure_code, IngestionFailureCode.NO_TEXT_EXTRACTED)

    def test_rejects_a_non_utf8_document(self) -> None:
        with self.assertRaises(DocumentParseError) as raised:
            parse_markdown("# 정책".encode("euc-kr"))

        self.assertEqual(raised.exception.failure_code, IngestionFailureCode.ENCODING_NOT_SUPPORTED)


class PlainTextParserTest(unittest.TestCase):
    def test_blocks_are_numbered_and_flagged_unstructured(self) -> None:
        parsed = parse_plain_text(b"First rule.\n\nSecond rule.\nStill second.\n")

        self.assertEqual([unit.locator for unit in parsed.units], ["block/1", "block/2"])
        self.assertIn(ExtractionWarningCode.UNSTRUCTURED_DOCUMENT, parsed.warnings)

    def test_strips_a_byte_order_mark_and_reports_it(self) -> None:
        parsed = parse_plain_text(b"\xef\xbb\xbfFirst rule.\n")

        self.assertIn(ExtractionWarningCode.BYTE_ORDER_MARK_STRIPPED, parsed.warnings)
        self.assertEqual(parsed.units[0].text_sha256, text_sha256("First rule."))


class CsvParserTest(unittest.TestCase):
    def test_rows_become_units_in_physical_order(self) -> None:
        parsed = parse_csv(b"control,requirement\n5.2.1,No public buckets\n")

        self.assertEqual([unit.locator for unit in parsed.units], ["row/1", "row/2"])
        self.assertEqual(parsed.units[1].kind, DocumentUnitKind.TABLE_ROW)
        self.assertEqual(parsed.units[1].text_sha256, text_sha256("5.2.1 | No public buckets"))

    def test_reports_an_inferred_delimiter(self) -> None:
        parsed = parse_csv(b"control;requirement\n5.2.1;No public buckets\n")

        self.assertIn(ExtractionWarningCode.DELIMITER_INFERRED, parsed.warnings)
        self.assertEqual(parsed.units[1].text_sha256, text_sha256("5.2.1 | No public buckets"))

    def test_reports_ragged_rows(self) -> None:
        parsed = parse_csv(b"a,b,c\n1,2\n")

        self.assertIn(ExtractionWarningCode.RAGGED_ROWS, parsed.warnings)


class XlsxParserTest(unittest.TestCase):
    def test_sheet_name_drives_the_locator(self) -> None:
        payload = build_xlsx(
            sheets=[("Security", sheet_row(27, shared_cell("A", 27, 0)))],
            shared_strings=["No public buckets"],
        )

        parsed = parse_xlsx(payload)
        self.assertEqual([unit.locator for unit in parsed.units], ["sheet/security/row/27"])
        self.assertEqual(parsed.units[0].kind, DocumentUnitKind.SHEET_ROW)

    def test_reads_inline_strings(self) -> None:
        """`scripts/policy_source_digest.py` 원형이 놓치던 `t="inlineStr"` 셀."""
        payload = build_xlsx(sheets=[("Sheet1", sheet_row(1, inline_cell("A", 1, "5.2.1")))])

        parsed = parse_xlsx(payload)
        self.assertEqual(parsed.units[0].text_sha256, text_sha256("5.2.1"))
        self.assertIn(ExtractionWarningCode.INLINE_STRINGS_PRESENT, parsed.warnings)

    def test_expands_a_vertical_merge_into_the_covered_rows(self) -> None:
        """세로 병합의 앵커 값은 첫 행에만 있으므로 이후 행이 분류 축을 잃지 않아야 한다."""
        payload = build_xlsx(
            sheets=[
                (
                    "Sheet1",
                    '<mergeCells><mergeCell ref="A1:A2"/></mergeCells>'
                    + sheet_row(1, shared_cell("A", 1, 0) + shared_cell("B", 1, 1))
                    + sheet_row(2, shared_cell("B", 2, 2)),
                )
            ],
            shared_strings=["Access control", "5.2.1", "5.2.2"],
        )

        parsed = parse_xlsx(payload)
        digests = [unit.text_sha256 for unit in parsed.units]
        self.assertEqual(digests[0], text_sha256("Access control | 5.2.1"))
        self.assertEqual(digests[1], text_sha256("Access control | 5.2.2"))
        self.assertIn(ExtractionWarningCode.MERGED_CELLS_EXPANDED, parsed.warnings)

    def test_rejects_worksheets_whose_names_share_a_locator_segment(self) -> None:
        payload = build_xlsx(
            sheets=[
                ("Security", sheet_row(1, inline_cell("A", 1, "one"))),
                ("security ", sheet_row(1, inline_cell("A", 1, "two"))),
            ]
        )

        with self.assertRaises(DocumentParseError) as raised:
            parse_xlsx(payload)
        self.assertEqual(raised.exception.failure_code, IngestionFailureCode.AMBIGUOUS_LOCATOR)

    def test_keeps_korean_sheet_names_addressable(self) -> None:
        payload = build_xlsx(sheets=[("보안 정책", sheet_row(1, inline_cell("A", 1, "5.2.1")))])

        self.assertEqual(parse_xlsx(payload).units[0].locator, "sheet/보안-정책/row/1")

    def test_rejects_a_workbook_without_a_worksheet(self) -> None:
        payload = build_xlsx(sheets=[])

        with self.assertRaises(DocumentParseError) as raised:
            parse_xlsx(payload)
        self.assertEqual(raised.exception.failure_code, IngestionFailureCode.CORRUPTED_DOCUMENT)


class DocxParserTest(unittest.TestCase):
    def test_paragraphs_use_the_markdown_locator_scheme(self) -> None:
        payload = build_docx(
            paragraph("Access Control", style="Heading1")
            + paragraph("Accounts must use least privilege.")
            + paragraph("Storage", style="Heading2")
            + paragraph("Buckets encrypt objects at rest.")
        )

        locators = [unit.locator for unit in parse_docx(payload).units]
        self.assertEqual(
            locators,
            [
                "heading/access-control",
                "heading/access-control/item/1",
                "heading/access-control/storage",
                "heading/access-control/storage/item/1",
            ],
        )

    def test_docx_and_markdown_agree_on_the_same_sentence(self) -> None:
        """같은 문장이 형식만 바뀌었을 때 Evidence hash가 이어져야 한다."""
        sentence = "Accounts must use least privilege."
        payload = build_docx(paragraph("Access Control", style="Heading1") + paragraph(sentence))

        docx_units = {unit.locator: unit.text_sha256 for unit in parse_docx(payload).units}
        markdown_units = {unit.locator: unit.text_sha256 for unit in parse_markdown(MARKDOWN).units}
        self.assertEqual(
            docx_units["heading/access-control/item/1"],
            markdown_units["heading/access-control/item/1"],
        )

    def test_tables_are_addressed_by_table_and_row(self) -> None:
        payload = build_docx(
            paragraph("Controls", style="Heading1")
            + table([["control", "requirement"], ["5.2.1", "No public buckets"]])
        )

        units = {unit.locator: unit for unit in parse_docx(payload).units}
        self.assertIn("table/1/row/2", units)
        self.assertEqual(units["table/1/row/2"].kind, DocumentUnitKind.TABLE_ROW)
        self.assertEqual(
            units["table/1/row/2"].text_sha256, text_sha256("5.2.1 | No public buckets")
        )

    def test_flags_a_document_without_headings(self) -> None:
        parsed = parse_docx(build_docx(paragraph("A single rule.")))

        self.assertIn(ExtractionWarningCode.UNSTRUCTURED_DOCUMENT, parsed.warnings)
        self.assertEqual(parsed.units[0].locator, "heading/preamble/item/1")


class NormalizationTest(unittest.TestCase):
    def test_collapses_horizontal_whitespace_but_keeps_lines(self) -> None:
        self.assertEqual(normalize_text("  a\t\tb  \n\n c \n"), "a b\n\nc")

    def test_composes_unicode_so_the_digest_is_stable(self) -> None:
        """macOS 업로드는 NFD로 오는 경우가 있어 같은 한글이 다른 hash가 될 수 있다."""
        self.assertEqual(normalize_text("정책"), normalize_text("정책"))
        self.assertEqual(text_sha256(normalize_text("가")), text_sha256("가"))


if __name__ == "__main__":
    unittest.main()
