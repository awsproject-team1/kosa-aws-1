"""XLSX and DOCX parsers built on `zipfile` and `xml.etree`. No third-party runtime.

Backend는 서드파티 런타임 의존성이 없는 ZIP Lambda로 배포된다 (`docs/POLICY_INGESTION.md`
Format policy). openpyxl/python-docx를 쓰지 않는 이유는 취향이 아니라 배포 구조다.

`scripts/policy_source_digest.py`의 XLSX 원형이 다루지 못하던 세 가지를 여기서 처리한다:
inline string(`t="inlineStr"`), 병합 셀, `xl/workbook.xml` 기반 시트 이름 locator.

`xml.etree.ElementTree`는 내부 DTD 엔티티를 확장하며 Python 문서가 billion laughs에 취약하다고
명시한다. zip 압축 한도는 이를 막지 못한다 — 증폭이 압축 해제 **이후** XML Parser 안에서
일어나므로 선언 크기도 읽은 바이트도 작다. 정상 OOXML은 DTD를 쓰지 않으므로
`_parse_xml()`이 파싱 전에 DOCTYPE 선언을 fail-closed로 거부한다.
"""

from __future__ import annotations

import re
import zipfile
from xml.etree import ElementTree

from apps.backend.policy.ingestion.formats import (
    open_archive,
    read_archive_member,
    require_safe_archive,
)
from apps.backend.policy.ingestion.normalization import (
    DocumentBuilder,
    DocumentParseError,
    ParsedPolicyDocument,
    slug,
)
from packages.contracts.policy_ingestion import (
    DocumentUnitKind,
    ExtractionWarningCode,
    IngestionFailureCode,
)

XLSX_PARSER_ID = "xlsx-parser"
XLSX_PARSER_VERSION = "1.0.1"
DOCX_PARSER_ID = "docx-parser"
DOCX_PARSER_VERSION = "1.0.1"

SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
OFFICE_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

WORKBOOK_PART = "xl/workbook.xml"
WORKBOOK_RELS_PART = "xl/_rels/workbook.xml.rels"
SHARED_STRINGS_PART = "xl/sharedStrings.xml"
DOCUMENT_PART = "word/document.xml"

MAX_SHEET_ROWS = 50_000
MAX_COLUMN_INDEX = 1_024

_CELL_REFERENCE = re.compile(r"^(?P<column>[A-Z]+)(?P<row>\d+)$")
_ROW_REFERENCE = re.compile(r"^\+?[0-9]+$")
_HEADING_STYLE = re.compile(r"^heading\s*(?P<level>[1-6])$", re.IGNORECASE)
# OOXML XML parts may use UTF-8 or UTF-16, so block every supported byte representation.
_DOCTYPE_DECLARATIONS = (
    b"<!DOCTYPE",
    "<!DOCTYPE".encode("utf-16-le"),
    "<!DOCTYPE".encode("utf-16-be"),
)


def parse_xlsx(payload: bytes) -> ParsedPolicyDocument:
    """Normalize a workbook into `sheet/{slug}/row/{n}` units.

    Locator는 시트 이름과 물리 행 번호에서 나온다. 시트 이름 slug이 충돌하면 같은 locator가
    두 행을 가리키게 되므로 `AMBIGUOUS_LOCATOR`로 fail-closed 종료한다.
    """
    require_safe_archive(payload)
    builder = DocumentBuilder()
    with open_archive(payload) as archive:
        names = set(archive.namelist())
        if WORKBOOK_PART not in names:
            raise DocumentParseError(
                IngestionFailureCode.CORRUPTED_DOCUMENT, "the workbook has no xl/workbook.xml"
            )
        shared = _shared_strings(archive, names)
        sheets = _worksheet_parts(archive, names)
        seen_slugs: dict[str, str] = {}
        for sheet_name, part in sheets:
            sheet_slug = slug(sheet_name)
            if sheet_slug in seen_slugs:
                raise DocumentParseError(
                    IngestionFailureCode.AMBIGUOUS_LOCATOR,
                    f"two worksheets share the locator segment {sheet_slug!r}",
                )
            seen_slugs[sheet_slug] = sheet_name
            _add_sheet_rows(builder, archive, part, sheet_slug, shared)
    return builder.build()


def parse_docx(payload: bytes) -> ParsedPolicyDocument:
    """Normalize a document into heading-scoped paragraphs and `table/{n}/row/{m}` rows.

    문단 locator는 Markdown과 같은 `heading/{slug}/item/{n}` 체계를 쓴다. 두 형식이 같은
    체계를 공유해야 같은 사내 정책이 형식만 바뀌었을 때 Evidence가 이어진다.
    """
    require_safe_archive(payload)
    builder = DocumentBuilder()
    with open_archive(payload) as archive:
        if DOCUMENT_PART not in set(archive.namelist()):
            raise DocumentParseError(
                IngestionFailureCode.CORRUPTED_DOCUMENT, "the document has no word/document.xml"
            )
        root = _parse_xml(read_archive_member(archive, DOCUMENT_PART))

    body = root.find(f"{WORD_NS}body")
    if body is None:
        raise DocumentParseError(
            IngestionFailureCode.CORRUPTED_DOCUMENT, "the document body is missing"
        )

    path: list[str] = []
    section = "preamble"
    ordinal = 0
    table_index = 0
    saw_heading = False

    for index, element in enumerate(body, start=1):
        if element.tag == f"{WORD_NS}p":
            text = _paragraph_text(element)
            if not text.strip():
                continue
            level = _heading_level(element)
            if level is None:
                ordinal += 1
                builder.add(
                    locator=f"heading/{section}/item/{ordinal}",
                    kind=DocumentUnitKind.PARAGRAPH,
                    origin=f"body/{index}",
                    text=text,
                )
                continue
            saw_heading = True
            del path[level - 1 :]
            path.append(slug(text))
            section = "/".join(path)
            ordinal = 0
            builder.add(
                locator=f"heading/{section}",
                kind=DocumentUnitKind.SECTION,
                origin=f"body/{index}",
                text=text,
            )
        elif element.tag == f"{WORD_NS}tbl":
            table_index += 1
            for row_index, row in enumerate(element.findall(f"{WORD_NS}tr"), start=1):
                cells = [_cell_text(cell).strip() for cell in row.findall(f"{WORD_NS}tc")]
                builder.add(
                    locator=f"table/{table_index}/row/{row_index}",
                    kind=DocumentUnitKind.TABLE_ROW,
                    origin=f"body/{index}/row/{row_index}",
                    text=" | ".join(cells),
                )

    if not saw_heading:
        builder.warn(ExtractionWarningCode.UNSTRUCTURED_DOCUMENT)
    return builder.build()


def _add_sheet_rows(
    builder: DocumentBuilder,
    archive: zipfile.ZipFile,
    part: str,
    sheet_slug: str,
    shared: list[str],
) -> None:
    sheet = _parse_xml(read_archive_member(archive, part))
    merges = _merged_ranges(sheet)
    if merges:
        builder.warn(ExtractionWarningCode.MERGED_CELLS_EXPANDED)

    rows = list(sheet.iter(f"{SHEET_NS}row"))
    if len(rows) > MAX_SHEET_ROWS:
        raise DocumentParseError(
            IngestionFailureCode.EXPANSION_LIMIT_EXCEEDED,
            f"worksheet {sheet_slug!r} declares more than {MAX_SHEET_ROWS} rows",
        )

    # 병합 범위는 세로로도 걸치므로, 행을 하나씩 내보내면서는 앵커 값을 볼 수 없다.
    # 먼저 시트 전체를 읽고 나서 병합을 적용한다.
    grid: dict[int, dict[int, str]] = {}
    inline = False
    for fallback_index, row in enumerate(rows, start=1):
        row_index = _row_index(row.get("r"), fallback_index, sheet_slug)
        cells: dict[int, str] = {}
        seen_columns: set[int] = set()
        next_column = 1
        for cell in row.findall(f"{SHEET_NS}c"):
            column = _cell_column(cell.get("r"), next_column, row_index, sheet_slug)
            if column in seen_columns:
                raise DocumentParseError(
                    IngestionFailureCode.AMBIGUOUS_LOCATOR,
                    f"worksheet {sheet_slug!r} declares cell column {column} "
                    f"more than once in row {row_index}",
                )
            seen_columns.add(column)
            next_column = column + 1
            value, is_inline = _cell_value(cell, shared)
            inline = inline or is_inline
            if value:
                cells[column] = value
        if row_index in grid:
            # 같은 `r`을 가진 행이 둘이면 locator 하나가 두 행을 가리킨다. 덮어쓰면 정책
            # 문서의 한 행이 경고 없이 사라지므로, 시트 이름 충돌과 같게 fail-closed로 막는다.
            raise DocumentParseError(
                IngestionFailureCode.AMBIGUOUS_LOCATOR,
                f"worksheet {sheet_slug!r} declares row {row_index} more than once",
            )
        grid[row_index] = cells
    if inline:
        builder.warn(ExtractionWarningCode.INLINE_STRINGS_PRESENT)

    _apply_merges(grid, merges)

    widths: set[int] = set()
    for row_index in sorted(grid):
        cells = grid[row_index]
        if not cells:
            continue
        widths.add(max(cells))
        builder.add(
            locator=f"sheet/{sheet_slug}/row/{row_index}",
            kind=DocumentUnitKind.SHEET_ROW,
            origin=f"{part}#row/{row_index}",
            text=" | ".join(cells[column] for column in sorted(cells)),
        )
    if len(widths) > 1:
        builder.warn(ExtractionWarningCode.RAGGED_ROWS)


def _apply_merges(grid: dict[int, dict[int, str]], merges: list[tuple[int, int, int, int]]) -> None:
    """Copy each merged range's anchor value into every cell the range covers.

    병합 셀의 값은 좌상단 셀에만 저장된다. 그대로 두면 병합으로 덮인 행이 분류 축을 잃고,
    사람이 원문에서 보는 행과 정규화 결과가 달라진다. 세로 병합에서는 앵커가 다른 행에
    있으므로 시트를 모두 읽은 뒤에만 채울 수 있다.
    """
    for first_row, first_column, last_row, last_column in merges:
        anchor = grid.get(first_row, {}).get(first_column)
        if not anchor:
            continue
        for row_index in range(first_row, last_row + 1):
            cells = grid.get(row_index)
            if cells is None:
                continue
            for column in range(first_column, min(last_column, MAX_COLUMN_INDEX) + 1):
                cells.setdefault(column, anchor)


def _merged_ranges(sheet: ElementTree.Element) -> list[tuple[int, int, int, int]]:
    ranges: list[tuple[int, int, int, int]] = []
    for merge in sheet.iter(f"{SHEET_NS}mergeCell"):
        reference = merge.get("ref")
        if not reference or ":" not in reference:
            continue
        start, end = reference.split(":", 1)
        first = _CELL_REFERENCE.match(start)
        last = _CELL_REFERENCE.match(end)
        if first is None or last is None:
            continue
        ranges.append(
            (
                int(first.group("row")),
                _column_number(first.group("column")),
                int(last.group("row")),
                _column_number(last.group("column")),
            )
        )
    return ranges


def _cell_value(cell: ElementTree.Element, shared: list[str]) -> tuple[str, bool]:
    """Return one cell's text and whether it came from an inline string."""
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        node = cell.find(f"{SHEET_NS}is")
        return ("" if node is None else _joined_text(node, f"{SHEET_NS}t")), True
    value = cell.find(f"{SHEET_NS}v")
    if value is None or value.text is None:
        return "", False
    if cell_type == "s":
        try:
            return shared[int(value.text)], False
        except (ValueError, IndexError):
            # sharedStrings 인덱스가 깨진 워크북은 셀 하나로 문서 전체를 버리지 않는다.
            return "", False
    return value.text, False


def _shared_strings(archive: zipfile.ZipFile, names: set[str]) -> list[str]:
    if SHARED_STRINGS_PART not in names:
        return []
    table = _parse_xml(read_archive_member(archive, SHARED_STRINGS_PART))
    return [_joined_text(item, f"{SHEET_NS}t") for item in table.findall(f"{SHEET_NS}si")]


def _worksheet_parts(archive: zipfile.ZipFile, names: set[str]) -> list[tuple[str, str]]:
    """Resolve (sheet name, part path) pairs in workbook order.

    `xl/worksheets/sheet1.xml` 파일 순서는 시트 순서도 이름도 알려주지 않는다. 이름은
    `xl/workbook.xml`에, 파일 경로는 relationship에 있으므로 둘을 이어야 한다.
    """
    workbook = _parse_xml(read_archive_member(archive, WORKBOOK_PART))
    targets = _relationship_targets(archive, names)
    sheets: list[tuple[str, str]] = []
    for index, sheet in enumerate(workbook.iter(f"{SHEET_NS}sheet"), start=1):
        name = sheet.get("name") or f"sheet{index}"
        relationship_id = sheet.get(f"{OFFICE_REL_NS}id")
        target = targets.get(relationship_id or "")
        part = f"xl/{target}" if target else f"xl/worksheets/sheet{index}.xml"
        if part not in names:
            continue
        sheets.append((name, part))
    if not sheets:
        raise DocumentParseError(
            IngestionFailureCode.CORRUPTED_DOCUMENT, "the workbook declares no readable worksheet"
        )
    return sheets


def _relationship_targets(archive: zipfile.ZipFile, names: set[str]) -> dict[str, str]:
    if WORKBOOK_RELS_PART not in names:
        return {}
    root = _parse_xml(read_archive_member(archive, WORKBOOK_RELS_PART))
    targets: dict[str, str] = {}
    for relationship in root.iter(f"{REL_NS}Relationship"):
        identifier = relationship.get("Id")
        target = relationship.get("Target")
        if identifier and target:
            targets[identifier] = target.lstrip("/")
    return targets


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    return _joined_text(paragraph, f"{WORD_NS}t")


def _cell_text(cell: ElementTree.Element) -> str:
    return " ".join(
        text for text in (_paragraph_text(p) for p in cell.iter(f"{WORD_NS}p")) if text.strip()
    )


def _heading_level(paragraph: ElementTree.Element) -> int | None:
    style = paragraph.find(f"{WORD_NS}pPr/{WORD_NS}pStyle")
    if style is None:
        return None
    value = style.get(f"{WORD_NS}val") or ""
    match = _HEADING_STYLE.match(value.replace("-", " ").strip())
    return int(match.group("level")) if match else None


def _joined_text(node: ElementTree.Element, tag: str) -> str:
    return "".join(child.text or "" for child in node.iter(tag))


def _parse_xml(data: bytes) -> ElementTree.Element:
    _require_no_dtd(data)
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as error:
        raise DocumentParseError(
            IngestionFailureCode.CORRUPTED_DOCUMENT, "an OOXML part is not well-formed XML"
        ) from error


def _require_no_dtd(data: bytes) -> None:
    """Refuse UTF-8 or UTF-16 OOXML parts that declare a DTD before parsing."""
    if any(declaration in data for declaration in _DOCTYPE_DECLARATIONS):
        raise DocumentParseError(
            IngestionFailureCode.XML_DTD_NOT_ALLOWED,
            "an OOXML part declares a DTD, which is not allowed",
        )


def _cell_column(reference: str | None, fallback: int, row: int, sheet_slug: str) -> int:
    if reference is None:
        return fallback
    match = _CELL_REFERENCE.match(reference)
    if match is None:
        raise DocumentParseError(
            IngestionFailureCode.CORRUPTED_DOCUMENT,
            f"worksheet {sheet_slug!r} contains an invalid cell reference {reference!r}",
        )
    referenced_row = int(match.group("row"))
    if referenced_row != row:
        raise DocumentParseError(
            IngestionFailureCode.AMBIGUOUS_LOCATOR,
            f"worksheet {sheet_slug!r} row {row} contains a cell that references "
            f"row {referenced_row}",
        )
    return _column_number(match.group("column"))


def _column_number(letters: str) -> int:
    number = 0
    for character in letters:
        number = number * 26 + (ord(character) - ord("A") + 1)
    return min(number, MAX_COLUMN_INDEX)


def _row_index(reference: str | None, fallback: int, sheet_slug: str) -> int:
    if reference is None:
        return fallback
    normalized = reference.strip()
    if _ROW_REFERENCE.match(normalized) is None or int(normalized) < 1:
        raise DocumentParseError(
            IngestionFailureCode.CORRUPTED_DOCUMENT,
            f"worksheet {sheet_slug!r} contains an invalid row reference {reference!r}",
        )
    return int(normalized)


__all__ = [
    "DOCX_PARSER_ID",
    "DOCX_PARSER_VERSION",
    "XLSX_PARSER_ID",
    "XLSX_PARSER_VERSION",
    "parse_docx",
    "parse_xlsx",
]
