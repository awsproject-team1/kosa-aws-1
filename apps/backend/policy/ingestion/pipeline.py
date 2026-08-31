"""Turn one finalized upload into a Normalized Policy Document, fail-closed.

이 모듈이 B 소유 경계의 진입점이다. A의 검증/처리 Job이 원본 바이트와 서버가 발급한 identity를
넘기면, 형식 판정 → Parser → 정규화 Contract까지가 여기서 끝난다. 저장, 상태 write, 승인은
호출자의 몫이며 이 모듈은 아무것도 영속화하지 않는다.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from apps.backend.policy.ingestion.formats import (
    DocumentFormatError,
    detect_format,
    format_for_media_type,
)
from apps.backend.policy.ingestion.normalization import DocumentParseError, ParsedPolicyDocument
from apps.backend.policy.ingestion.parsers.ooxml import (
    DOCX_PARSER_ID,
    DOCX_PARSER_VERSION,
    XLSX_PARSER_ID,
    XLSX_PARSER_VERSION,
    parse_docx,
    parse_xlsx,
)
from apps.backend.policy.ingestion.parsers.text import (
    CSV_PARSER_ID,
    CSV_PARSER_VERSION,
    MARKDOWN_PARSER_ID,
    MARKDOWN_PARSER_VERSION,
    PLAIN_TEXT_PARSER_ID,
    PLAIN_TEXT_PARSER_VERSION,
    parse_csv,
    parse_markdown,
    parse_plain_text,
)
from packages.contracts import SourceReference
from packages.contracts.policy_ingestion import (
    FORMAT_MEDIA_TYPES,
    ExtractionWarningCode,
    IngestionFailureCode,
    IngestionStatus,
    NormalizedPolicyDocument,
    PolicySourceFormat,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ParserRegistration:
    parser_id: str
    parser_version: str
    parse: Callable[[bytes], ParsedPolicyDocument]


# 지원 형식과 Parser의 1:1 대응. 이 표에 없는 형식은 코드 경로 자체가 없다.
PARSERS: dict[PolicySourceFormat, ParserRegistration] = {
    PolicySourceFormat.MARKDOWN: ParserRegistration(
        parser_id=MARKDOWN_PARSER_ID, parser_version=MARKDOWN_PARSER_VERSION, parse=parse_markdown
    ),
    PolicySourceFormat.PLAIN_TEXT: ParserRegistration(
        parser_id=PLAIN_TEXT_PARSER_ID,
        parser_version=PLAIN_TEXT_PARSER_VERSION,
        parse=parse_plain_text,
    ),
    PolicySourceFormat.CSV: ParserRegistration(
        parser_id=CSV_PARSER_ID, parser_version=CSV_PARSER_VERSION, parse=parse_csv
    ),
    PolicySourceFormat.XLSX: ParserRegistration(
        parser_id=XLSX_PARSER_ID, parser_version=XLSX_PARSER_VERSION, parse=parse_xlsx
    ),
    PolicySourceFormat.DOCX: ParserRegistration(
        parser_id=DOCX_PARSER_ID, parser_version=DOCX_PARSER_VERSION, parse=parse_docx
    ),
}

# 이 경고가 붙은 문서는 자동으로 `READY`가 되지 않는다. 사람이 추출 결과를 보고 판단해야
# locator가 근거로 쓸 만한지 결정할 수 있다.
REVIEW_REQUIRED_WARNINGS: frozenset[ExtractionWarningCode] = frozenset(
    {
        ExtractionWarningCode.UNSTRUCTURED_DOCUMENT,
        ExtractionWarningCode.RAGGED_ROWS,
        ExtractionWarningCode.MERGED_CELLS_EXPANDED,
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class UploadedPolicyOriginal:
    """Server-issued identity and metadata of one finalized upload.

    Client는 이 값 중 어느 것도 제안하지 않는다 (`docs/POLICY_INGESTION.md` Original
    finalization). Parser는 여기 담긴 checksum과 byte size를 실제 바이트로 다시 검증한다.
    """

    source_id: str
    source_version: str
    artifact_id: str
    s3_version_id: str
    content_sha256: str
    filename: str
    declared_media_type: str
    byte_size: int


@dataclass(frozen=True, slots=True, kw_only=True)
class NormalizationOutcome:
    """The normalized document plus the artifact bytes it describes.

    `normalized_payload`는 추출 텍스트를 담는 유일한 값이고 S3 Artifact로만 나간다. 실패한
    문서에는 없다.
    """

    document: NormalizedPolicyDocument
    normalized_payload: bytes | None = None

    @property
    def succeeded(self) -> bool:
        return self.document.status is not IngestionStatus.FAILED


def normalize_upload(original: UploadedPolicyOriginal, payload: bytes) -> NormalizationOutcome:
    """Normalize one uploaded original, returning a FAILED document instead of raising.

    실패를 예외가 아니라 상태로 돌려주는 이유는, 지원하지 않는 문서도 고객에게 사유를 보여줘야
    하는 정상 결과이기 때문이다. 호출자가 실패 코드를 상태 전이에 그대로 쓴다.
    """
    if not isinstance(original, UploadedPolicyOriginal):
        raise TypeError("original must be an UploadedPolicyOriginal")
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")

    try:
        return _normalize(original, payload)
    except (DocumentFormatError, DocumentParseError) as error:
        return NormalizationOutcome(document=_failed(original, error.failure_code))


def source_reference_for(document: NormalizedPolicyDocument, locator: str) -> SourceReference:
    """Build the Rule/Control `SourceReference` for one normalized unit.

    Rule 작성자가 locator와 hash를 손으로 옮겨 적으면 둘이 어긋날 수 있다. 정규화 결과에서
    직접 만들면 `{source_id}@{source_version}#{locator}`와 `content_sha256`이 같은 판본에서
    나온 값임이 보장된다.
    """
    if document.status is IngestionStatus.FAILED:
        raise ValueError("a FAILED document has no citable units")
    if not document.is_approvable:
        raise ValueError(
            f"source references may only cite an approvable document, not {document.status.value}"
        )
    unit = document.unit(locator)
    if unit is None:
        raise KeyError(f"locator {locator!r} is not part of the normalized document")
    return SourceReference(
        source_id=document.source_id,
        source_version=document.source_version,
        locator=unit.locator,
        content_sha256=unit.text_sha256,
    )


def _normalize(original: UploadedPolicyOriginal, payload: bytes) -> NormalizationOutcome:
    _require_finalized_bytes(original, payload)
    declared = format_for_media_type(original.declared_media_type)
    source_format = detect_format(payload, declared=declared)
    registration = PARSERS[source_format]
    parsed = registration.parse(payload)
    status = (
        IngestionStatus.REVIEW_REQUIRED
        if REVIEW_REQUIRED_WARNINGS.intersection(parsed.warnings)
        else IngestionStatus.READY
    )
    document = NormalizedPolicyDocument(
        source_id=original.source_id,
        source_version=original.source_version,
        artifact_id=original.artifact_id,
        s3_version_id=original.s3_version_id,
        content_sha256=original.content_sha256,
        filename=original.filename,
        declared_media_type=original.declared_media_type,
        detected_media_type=FORMAT_MEDIA_TYPES[source_format],
        source_format=source_format,
        byte_size=original.byte_size,
        parser_id=registration.parser_id,
        parser_version=registration.parser_version,
        normalized_artifact_id=_normalized_artifact_id(original),
        normalized_sha256=parsed.normalized_sha256,
        status=status,
        units=parsed.units,
        warnings=parsed.warnings,
    )
    return NormalizationOutcome(document=document, normalized_payload=parsed.normalized_payload)


def _require_finalized_bytes(original: UploadedPolicyOriginal, payload: bytes) -> None:
    """Re-verify the finalized checksum and size before interpreting the bytes.

    Finalize 단계가 확인한 것과 Parser가 읽은 것이 같은 바이트임을 Parser 쪽에서도 확인한다.
    `s3_version_id`가 같아도 잘못된 object를 넘기는 호출 실수는 여기서 걸린다.
    """
    if len(payload) != original.byte_size:
        raise DocumentFormatError(
            IngestionFailureCode.CORRUPTED_DOCUMENT,
            "the payload size does not match the finalized byte size",
        )
    if hashlib.sha256(payload).hexdigest() != original.content_sha256:
        raise DocumentFormatError(
            IngestionFailureCode.CORRUPTED_DOCUMENT,
            "the payload digest does not match the finalized content_sha256",
        )


def _normalized_artifact_id(original: UploadedPolicyOriginal) -> str:
    """Derive the normalized artifact ID from the original it was produced from."""
    return f"{original.artifact_id}#normalized"


def _failed(
    original: UploadedPolicyOriginal, failure_code: IngestionFailureCode
) -> NormalizedPolicyDocument:
    return NormalizedPolicyDocument(
        source_id=original.source_id,
        source_version=original.source_version,
        artifact_id=original.artifact_id,
        s3_version_id=original.s3_version_id,
        content_sha256=original.content_sha256,
        filename=original.filename,
        declared_media_type=original.declared_media_type,
        byte_size=original.byte_size,
        status=IngestionStatus.FAILED,
        failure_code=failure_code,
    )
