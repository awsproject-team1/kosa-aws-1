"""Markdown, plain text, and CSV parsers. Standard library only.

세 형식 모두 UTF-8 텍스트에서 출발하지만 locator 체계는 다르다. Markdown은 heading 구조에서
locator가 직접 나오고, plain text는 구조가 없어 블록 순번만 쓸 수 있으며, CSV는 행이 단위다.
"""

from __future__ import annotations

import csv
import io
import re

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

MARKDOWN_PARSER_ID = "markdown-parser"
#: 1.1.0: 빈 줄 없이 이어진 최상위 목록 항목을 각각 별도 unit으로 나눈다. 1.0.1은 tight list 전체를
#: unit 하나로 묶어 요구사항 여러 개가 locator 하나를 공유했다 — 승인·Evidence가 가리키는 단위가
#: "문장"이 아니라 "목록 전체"가 되는 문제다. parser version이 바뀌었으므로 같은 원본도 새 Source
#: version으로 정규화된다(`docs/POLICY_INGESTION.md` Evidence identity).
MARKDOWN_PARSER_VERSION = "1.1.0"
PLAIN_TEXT_PARSER_ID = "plain-text-parser"
PLAIN_TEXT_PARSER_VERSION = "1.0.1"
CSV_PARSER_ID = "csv-parser"
CSV_PARSER_VERSION = "1.0.1"

_HEADING = re.compile(r"^(?P<level>#{1,6})\s+(?P<title>.+?)\s*#*\s*$")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
#: 들여쓰기 없는 목록 마커. 이 줄에서 새 unit이 시작한다. 들여쓴 하위 항목은 상위 항목의 일부다 —
#: 사내 체크리스트의 "확인 방법" 하위 목록이 항목과 떨어지면 locator 하나가 문장 절반만 가리킨다.
_TOP_LEVEL_LIST_ITEM = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
_FENCE = re.compile(r"^\s*(?:```|~~~)")


def decode_text(payload: bytes) -> tuple[str, bool]:
    """Decode UTF-8 strictly, reporting whether a byte order mark was stripped."""
    had_bom = payload.startswith(b"\xef\xbb\xbf")
    try:
        return payload.decode("utf-8-sig"), had_bom
    except UnicodeDecodeError as error:
        raise DocumentParseError(
            IngestionFailureCode.ENCODING_NOT_SUPPORTED, "the document is not valid UTF-8"
        ) from error


def parse_markdown(payload: bytes) -> ParsedPolicyDocument:
    """Normalize Markdown into `heading/{slug}` sections and their items.

    Locator는 heading 경로에서 나오므로 문서가 재조판돼도 같은 절을 다시 가리킨다. 같은
    제목이 여러 번 나오면 상위 heading 경로가 이미 다르며, 그래도 충돌하면
    `DocumentBuilder`가 `AMBIGUOUS_LOCATOR`로 실패한다.
    """
    text, had_bom = decode_text(payload)
    builder = DocumentBuilder()
    if had_bom:
        builder.warn(ExtractionWarningCode.BYTE_ORDER_MARK_STRIPPED)

    path: list[str] = []
    section = "preamble"
    ordinal = 0
    block: list[str] = []
    block_line = 0
    in_fence = False
    saw_heading = False

    def flush(end_line: int) -> None:
        nonlocal ordinal, block
        if not block:
            return
        ordinal += 1
        kind = (
            DocumentUnitKind.LIST_ITEM if _LIST_ITEM.match(block[0]) else DocumentUnitKind.PARAGRAPH
        )
        builder.add(
            locator=f"heading/{section}/item/{ordinal}",
            kind=kind,
            origin=f"line/{block_line}-{end_line}",
            text="\n".join(block),
        )
        block = []

    lines = text.split("\n")
    for index, line in enumerate(lines, start=1):
        if _FENCE.match(line):
            # 코드 fence 안의 `#`는 heading이 아니다.
            in_fence = not in_fence
            block.append(line)
            if not block_line:
                block_line = index
            continue
        heading = None if in_fence else _HEADING.match(line)
        if heading is not None:
            flush(index - 1)
            saw_heading = True
            level = len(heading.group("level"))
            del path[level - 1 :]
            path.append(slug(heading.group("title")))
            section = "/".join(path)
            ordinal = 0
            builder.add(
                locator=f"heading/{section}",
                kind=DocumentUnitKind.SECTION,
                origin=f"line/{index}",
                text=heading.group("title"),
            )
            continue
        if not line.strip() and not in_fence:
            flush(index - 1)
            block_line = 0
            continue
        if block and not in_fence and _TOP_LEVEL_LIST_ITEM.match(line):
            # 빈 줄 없이 이어진 목록: 최상위 항목마다 unit을 끊는다. 항목 하나가 요구사항 하나다.
            flush(index - 1)
            block_line = 0
        if not block:
            block_line = index
        block.append(line)
    flush(len(lines))

    if not saw_heading:
        builder.warn(ExtractionWarningCode.UNSTRUCTURED_DOCUMENT)
    return builder.build()


def parse_plain_text(payload: bytes) -> ParsedPolicyDocument:
    """Normalize plain text into blank-line separated `block/{n}` units.

    구조 정보가 없으므로 locator는 블록 순번뿐이다. 그 한계를 `UNSTRUCTURED_DOCUMENT`
    경고로 검토자에게 알린다 — 원문이 재조판되면 블록 번호가 밀릴 수 있다.
    """
    text, had_bom = decode_text(payload)
    builder = DocumentBuilder()
    if had_bom:
        builder.warn(ExtractionWarningCode.BYTE_ORDER_MARK_STRIPPED)
    builder.warn(ExtractionWarningCode.UNSTRUCTURED_DOCUMENT)

    ordinal = 0
    block: list[str] = []
    block_line = 0
    lines = text.split("\n")

    def flush(end_line: int) -> None:
        nonlocal ordinal, block
        if not block:
            return
        ordinal += 1
        builder.add(
            locator=f"block/{ordinal}",
            kind=DocumentUnitKind.PARAGRAPH,
            origin=f"line/{block_line}-{end_line}",
            text="\n".join(block),
        )
        block = []

    for index, line in enumerate(lines, start=1):
        if not line.strip():
            flush(index - 1)
            block_line = 0
            continue
        if not block:
            block_line = index
        block.append(line)
    flush(len(lines))
    return builder.build()


def parse_csv(payload: bytes) -> ParsedPolicyDocument:
    """Normalize CSV into `row/{n}` units, one per non-empty row.

    행 번호는 1-based 물리 행이다. 헤더 행도 하나의 unit이 되며, 정책 문서의 CSV는 헤더가
    분류 축을 설명하는 경우가 많아 근거로 인용될 수 있다.
    """
    text, had_bom = decode_text(payload)
    builder = DocumentBuilder()
    if had_bom:
        builder.warn(ExtractionWarningCode.BYTE_ORDER_MARK_STRIPPED)

    delimiter = _sniff_delimiter(text)
    if delimiter != ",":
        builder.warn(ExtractionWarningCode.DELIMITER_INFERRED)

    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
    except csv.Error as error:
        raise DocumentParseError(
            IngestionFailureCode.CORRUPTED_DOCUMENT, "the CSV document could not be parsed"
        ) from error

    widths = {len(row) for row in rows if row}
    if len(widths) > 1:
        builder.warn(ExtractionWarningCode.RAGGED_ROWS)

    for index, row in enumerate(rows, start=1):
        builder.add(
            locator=f"row/{index}",
            kind=DocumentUnitKind.TABLE_ROW,
            origin=f"row/{index}",
            text=" | ".join(cell.strip() for cell in row),
        )
    return builder.build()


def _sniff_delimiter(text: str) -> str:
    """Infer the delimiter, falling back to a comma rather than failing the upload."""
    sample = text[:8192]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","
