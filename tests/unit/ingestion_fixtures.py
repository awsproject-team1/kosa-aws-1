"""Builders for synthetic OOXML payloads used by the ingestion tests.

실제 정책 원문은 저장소에 없다 (ADR-0004). 테스트는 원문 대신 구조만 재현한 최소 XLSX/DOCX를
바이트로 만들어 Parser의 구조 처리(inline string, 병합 셀, 시트 이름, 표)를 고정한다.
"""

from __future__ import annotations

import zipfile
from io import BytesIO

SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def build_xlsx(
    *,
    sheets: list[tuple[str, str]],
    shared_strings: list[str] | None = None,
    extra: dict[str, str] | None = None,
) -> bytes:
    """Build a workbook from (sheet name, `<sheetData>` XML) pairs."""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        sheet_entries = "".join(
            f'<sheet name="{name}" sheetId="{index}" r:id="rId{index}"/>'
            for index, (name, _) in enumerate(sheets, start=1)
        )
        archive.writestr(
            "xl/workbook.xml",
            f'<?xml version="1.0"?><workbook xmlns="{SHEET_NS}" '
            f'xmlns:r="{OFFICE_REL_NS}"><sheets>{sheet_entries}</sheets></workbook>',
        )
        relationships = "".join(
            f'<Relationship Id="rId{index}" Target="worksheets/sheet{index}.xml" '
            f'Type="{OFFICE_REL_NS}/worksheet"/>'
            for index in range(1, len(sheets) + 1)
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            f'<?xml version="1.0"?><Relationships xmlns="{REL_NS}">{relationships}</Relationships>',
        )
        for index, (_, sheet_data) in enumerate(sheets, start=1):
            archive.writestr(
                f"xl/worksheets/sheet{index}.xml",
                f'<?xml version="1.0"?><worksheet xmlns="{SHEET_NS}">{sheet_data}</worksheet>',
            )
        if shared_strings is not None:
            items = "".join(f"<si><t>{value}</t></si>" for value in shared_strings)
            archive.writestr(
                "xl/sharedStrings.xml",
                f'<?xml version="1.0"?><sst xmlns="{SHEET_NS}">{items}</sst>',
            )
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


def shared_cell(column: str, row: int, index: int) -> str:
    return f'<c r="{column}{row}" t="s"><v>{index}</v></c>'


def inline_cell(column: str, row: int, value: str) -> str:
    return f'<c r="{column}{row}" t="inlineStr"><is><t>{value}</t></is></c>'


def sheet_row(row: int, cells: str) -> str:
    return f'<row r="{row}">{cells}</row>'


def entity_bomb_document(levels: int) -> str:
    """Build a `word/document.xml` whose internal DTD entities expand exponentially.

    각 엔티티가 이전 엔티티를 10번 참조하므로 `levels` 단계면 10**levels 자로 불어난다.
    zip으로 압축하면 수백 바이트다.
    """
    entities = ['<!ENTITY e0 "AAAAAAAAAA">']
    for level in range(1, levels):
        entities.append(f'<!ENTITY e{level} "{("&e" + str(level - 1) + ";") * 10}">')
    return "\n".join(
        (
            '<?xml version="1.0"?>',
            "<!DOCTYPE document [",
            *entities,
            "]>",
            f'<document xmlns:w="{WORD_NS}"><w:body><w:p><w:r>'
            f"<w:t>&e{levels - 1};</w:t></w:r></w:p></w:body></document>",
        )
    )


def build_docx(body: str) -> bytes:
    document = (
        f'<?xml version="1.0"?><document xmlns:w="{WORD_NS}"><w:body>{body}</w:body></document>'
    )
    return build_docx_part(document)


def build_docx_part(document_xml: str) -> bytes:
    """Package a raw `word/document.xml` so a test can control the XML exactly."""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


def paragraph(text: str, *, style: str | None = None) -> str:
    properties = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style is not None else ""
    return f"<w:p>{properties}<w:r><w:t>{text}</w:t></w:r></w:p>"


def table(rows: list[list[str]]) -> str:
    cells = "".join(
        "<w:tr>" + "".join(f"<w:tc>{paragraph(cell)}</w:tc>" for cell in row) + "</w:tr>"
        for row in rows
    )
    return f"<w:tbl>{cells}</w:tbl>"
