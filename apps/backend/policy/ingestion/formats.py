"""Fail-closed format detection for uploaded customer policy documents.

`docs/POLICY_INGESTION.md` Format policy: 확장자만 신뢰하지 않는다. 선언한 media type, 파일
signature로 탐지한 형식, Parser가 실제로 지원하는 형식을 함께 검증하고, 셋 중 하나라도
어긋나면 처리하지 않는다. 지원 목록에 없는 형식은 업로드가 성공했더라도 거부한다.
"""

from __future__ import annotations

import zipfile
from io import BytesIO

from packages.contracts.policy_ingestion import (
    SUPPORTED_MEDIA_TYPES,
    IngestionFailureCode,
    PolicySourceFormat,
)

# signature가 없는 형식. 셋의 구분은 선언 media type이 결정한다.
TEXT_FORMATS: frozenset[PolicySourceFormat] = frozenset(
    {PolicySourceFormat.MARKDOWN, PolicySourceFormat.PLAIN_TEXT, PolicySourceFormat.CSV}
)

# 업로드 원본 바이트 상한. 이 경계는 Parser가 메모리에 올리는 양을 제한하며, A의 업로드
# quota와는 별개로 Parser 쪽에서도 fail-closed로 다시 검사한다.
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024

# zip 기반 형식(XLSX/DOCX)의 압축 폭탄 한도. central directory의 선언 크기로 **읽기 전에**
# 판정한다. 실제 읽은 바이트도 같은 한도로 다시 확인한다.
MAX_ARCHIVE_ENTRIES = 512
MAX_ARCHIVE_TOTAL_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_ENTRY_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 200

_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

# 지원하지 않지만 명시적으로 구분할 가치가 있는 signature.
# OLE compound file은 legacy .doc/.xls이면서 동시에 **암호화된 OOXML**의 컨테이너다.
_OLE_COMPOUND_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_PDF_SIGNATURE = b"%PDF-"

_XLSX_MARKER = "xl/workbook.xml"
_DOCX_MARKER = "word/document.xml"
_OOXML_ENCRYPTION_MARKERS = ("EncryptedPackage", "EncryptionInfo")


class DocumentFormatError(ValueError):
    """Raised when an upload cannot be assigned to a supported format.

    사유는 `failure_code`로만 표현한다. 메시지에 원문 내용을 담지 않는다.
    """

    def __init__(self, failure_code: IngestionFailureCode, message: str) -> None:
        super().__init__(message)
        self.failure_code = failure_code


def format_for_media_type(declared_media_type: str) -> PolicySourceFormat:
    """Map a declared media type onto the supported allow-list."""
    if not isinstance(declared_media_type, str):
        raise TypeError("declared_media_type must be a string")
    # `text/csv; charset=utf-8` 같은 parameter는 형식 판정에 쓰지 않는다.
    essence = declared_media_type.split(";", 1)[0].strip().lower()
    source_format = SUPPORTED_MEDIA_TYPES.get(essence)
    if source_format is None:
        raise DocumentFormatError(
            IngestionFailureCode.UNSUPPORTED_FORMAT,
            f"declared media type {essence!r} is not on the supported allow-list",
        )
    return source_format


def detect_format(payload: bytes, *, declared: PolicySourceFormat) -> PolicySourceFormat:
    """Detect the format from the file signature, cross-checked against the declaration.

    텍스트 형식(Markdown/Plain text/CSV)은 signature가 없으므로 "지원 바이너리 형식이 아니고
    UTF-8로 디코딩되는가"만 판정하고 세 형식의 구분은 선언값을 따른다. 셋 사이의 혼동은
    Parser가 같은 정규화 Contract를 만들기 때문에 안전하지만, 바이너리를 텍스트로 선언하는
    것은 안전하지 않으므로 여기서 막는다.
    """
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    if not isinstance(declared, PolicySourceFormat):
        raise TypeError("declared must be a PolicySourceFormat")
    if not payload:
        raise DocumentFormatError(
            IngestionFailureCode.CORRUPTED_DOCUMENT, "the uploaded document is empty"
        )
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise DocumentFormatError(
            IngestionFailureCode.DOCUMENT_TOO_LARGE,
            f"the uploaded document exceeds {MAX_DOCUMENT_BYTES} bytes",
        )

    detected = _detect(payload)
    if detected is None:
        # 디코딩 가능한 텍스트. 세 텍스트 형식은 signature로 구분되지 않으므로 선언값을 쓴다.
        if declared not in TEXT_FORMATS:
            raise DocumentFormatError(
                IngestionFailureCode.MEDIA_TYPE_MISMATCH,
                f"declared {declared.value} but the document is plain text",
            )
        return declared
    if detected is not declared:
        raise DocumentFormatError(
            IngestionFailureCode.MEDIA_TYPE_MISMATCH,
            f"declared {declared.value} but the file signature is {detected.value}",
        )
    return detected


def require_safe_archive(payload: bytes) -> None:
    """Reject archive expansion bombs before any entry is read.

    `zipfile`의 `read()`는 압축 해제 크기를 제한하지 않으므로, central directory가 선언한
    크기와 압축비를 먼저 검사한다. 선언 크기를 위조한 아카이브는 Parser가 읽은 실제 바이트를
    `read_archive_member()`에서 다시 검사해 잡는다.
    """
    with _open_archive(payload) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise DocumentFormatError(
                IngestionFailureCode.EXPANSION_LIMIT_EXCEEDED,
                f"the archive declares more than {MAX_ARCHIVE_ENTRIES} entries",
            )
        total = 0
        for info in infos:
            if info.file_size > MAX_ARCHIVE_ENTRY_BYTES:
                raise DocumentFormatError(
                    IngestionFailureCode.EXPANSION_LIMIT_EXCEEDED,
                    f"archive entry {info.filename!r} declares an oversized expansion",
                )
            if info.compress_size > 0:
                ratio = info.file_size / info.compress_size
                if ratio > MAX_ARCHIVE_COMPRESSION_RATIO:
                    raise DocumentFormatError(
                        IngestionFailureCode.EXPANSION_LIMIT_EXCEEDED,
                        f"archive entry {info.filename!r} exceeds the compression ratio limit",
                    )
            total += info.file_size
            if total > MAX_ARCHIVE_TOTAL_BYTES:
                raise DocumentFormatError(
                    IngestionFailureCode.EXPANSION_LIMIT_EXCEEDED,
                    f"the archive expands beyond {MAX_ARCHIVE_TOTAL_BYTES} bytes",
                )


def read_archive_member(archive: zipfile.ZipFile, name: str) -> bytes:
    """Read one archive member, capping the bytes actually produced.

    선언 크기와 실제 크기가 다를 수 있으므로 한도를 넘는 순간 중단한다.
    """
    try:
        with archive.open(name) as member:
            data = member.read(MAX_ARCHIVE_ENTRY_BYTES + 1)
    except (KeyError, zipfile.BadZipFile, EOFError, ValueError) as error:
        raise DocumentFormatError(
            IngestionFailureCode.CORRUPTED_DOCUMENT,
            f"archive entry {name!r} could not be read",
        ) from error
    if len(data) > MAX_ARCHIVE_ENTRY_BYTES:
        raise DocumentFormatError(
            IngestionFailureCode.EXPANSION_LIMIT_EXCEEDED,
            f"archive entry {name!r} expanded beyond the entry limit",
        )
    return data


def open_archive(payload: bytes) -> zipfile.ZipFile:
    """Open a validated archive. Call `require_safe_archive()` first."""
    return _open_archive(payload)


def _open_archive(payload: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(BytesIO(payload))
    except zipfile.BadZipFile as error:
        raise DocumentFormatError(
            IngestionFailureCode.CORRUPTED_DOCUMENT, "the document is not a readable archive"
        ) from error


def _detect(payload: bytes) -> PolicySourceFormat | None:
    """Return the signature-detected format, or `None` for decodable plain text."""
    if payload.startswith(_OLE_COMPOUND_SIGNATURE):
        # 암호화된 XLSX/DOCX도 이 컨테이너로 저장된다. 어느 쪽이든 처리하지 않는다.
        raise DocumentFormatError(
            IngestionFailureCode.ENCRYPTED_DOCUMENT,
            "the document is an OLE compound file (legacy or encrypted Office format)",
        )
    if payload.startswith(_PDF_SIGNATURE):
        raise DocumentFormatError(
            IngestionFailureCode.UNSUPPORTED_FORMAT,
            "PDF is not on the supported allow-list (ADR-0015)",
        )
    if payload.startswith(_ZIP_SIGNATURES):
        return _detect_ooxml(payload)
    return _detect_text(payload)


def _detect_ooxml(payload: bytes) -> PolicySourceFormat:
    with _open_archive(payload) as archive:
        names = set(archive.namelist())
    if any(marker in name for name in names for marker in _OOXML_ENCRYPTION_MARKERS):
        raise DocumentFormatError(
            IngestionFailureCode.ENCRYPTED_DOCUMENT, "the OOXML package is encrypted"
        )
    if _XLSX_MARKER in names:
        return PolicySourceFormat.XLSX
    if _DOCX_MARKER in names:
        return PolicySourceFormat.DOCX
    raise DocumentFormatError(
        IngestionFailureCode.UNSUPPORTED_FORMAT,
        "the archive is neither an XLSX workbook nor a DOCX document",
    )


def _detect_text(payload: bytes) -> None:
    """Confirm the payload is decodable text; the caller's declaration picks the flavour."""
    if b"\x00" in payload:
        raise DocumentFormatError(
            IngestionFailureCode.UNSUPPORTED_FORMAT,
            "the document contains NUL bytes and is not plain text",
        )
    try:
        payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise DocumentFormatError(
            IngestionFailureCode.ENCODING_NOT_SUPPORTED,
            "the document is not valid UTF-8",
        ) from error
    return None
