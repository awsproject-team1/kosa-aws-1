"""Customer policy ingestion contracts: supported formats and normalized documents.

`docs/POLICY_INGESTION.md`(ADR-0015)의 수집 경계 중 문서 의미 부분을 실행 가능한 Contract로
고정한다. 업로드 세션과 저장 인프라는 A가, AI 후보 추출은 C가 담당하며 둘 다 여기서 정의한
`NormalizedPolicyDocument`를 소비한다.

이 Contract는 **원문도 추출 텍스트도 담지 않는다.** unit은 locator와 정규화 text hash만 갖고,
텍스트는 별도 정규화 Artifact 바이트로만 존재한다. 경고와 실패 사유 역시 자유 문자열이 아니라
열거값이다. Queue payload와 DynamoDB item이 이 Contract를 그대로 직렬화하므로, 텍스트 노출
금지를 규율이 아니라 구조로 강제한다.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from packages.contracts._validation import (
    require_non_empty_string,
    require_offset_aware_timestamp,
    require_optional_non_empty_string,
)


class PolicySourceFormat(StrEnum):
    """The supported customer policy file formats.

    이 목록이 지원 형식의 전부다 (`docs/POLICY_INGESTION.md` Format policy). 목록에 없는
    형식은 업로드가 성공하더라도 `UNSUPPORTED_FORMAT`으로 종료한다. 파일 형식은 Source
    종류(`PolicySourceKind`)와 별개 개념이며 서로 대체하지 않는다.
    """

    MARKDOWN = "MARKDOWN"
    PLAIN_TEXT = "PLAIN_TEXT"
    CSV = "CSV"
    XLSX = "XLSX"
    DOCX = "DOCX"


# 형식 → 선언 media type. Backend와 Frontend는 이 매핑을 정본으로 사용한다.
FORMAT_MEDIA_TYPES: dict[PolicySourceFormat, str] = {
    PolicySourceFormat.MARKDOWN: "text/markdown",
    PolicySourceFormat.PLAIN_TEXT: "text/plain",
    PolicySourceFormat.CSV: "text/csv",
    PolicySourceFormat.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    PolicySourceFormat.DOCX: (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
}

SUPPORTED_MEDIA_TYPES: dict[str, PolicySourceFormat] = {
    media_type: source_format for source_format, media_type in FORMAT_MEDIA_TYPES.items()
}


class IngestionStatus(StrEnum):
    """Processing state of one exact customer policy source version."""

    UPLOADED = "UPLOADED"
    VALIDATING = "VALIDATING"
    PARSING = "PARSING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    READY = "READY"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"


# 사람 승인이 붙을 수 있는 유일한 처리 상태. Rule과 Profile은 이 상태의 Source version만
# 참조할 수 있다 (`docs/POLICY_INGESTION.md` Normalized document contract).
APPROVABLE_STATUSES: frozenset[IngestionStatus] = frozenset({IngestionStatus.READY})

TERMINAL_STATUSES: frozenset[IngestionStatus] = frozenset(
    {IngestionStatus.READY, IngestionStatus.FAILED, IngestionStatus.SUPERSEDED}
)


class IngestionFailureCode(StrEnum):
    """Why a source version could not be normalized.

    실패 사유는 자유 문장이 아니라 이 열거값으로만 표현한다. 원문 내용이 오류 메시지를 통해
    로그나 API 응답으로 새는 경로를 막는다.
    """

    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    MEDIA_TYPE_MISMATCH = "MEDIA_TYPE_MISMATCH"
    ENCRYPTED_DOCUMENT = "ENCRYPTED_DOCUMENT"
    CORRUPTED_DOCUMENT = "CORRUPTED_DOCUMENT"
    ENCODING_NOT_SUPPORTED = "ENCODING_NOT_SUPPORTED"
    NO_TEXT_EXTRACTED = "NO_TEXT_EXTRACTED"
    DOCUMENT_TOO_LARGE = "DOCUMENT_TOO_LARGE"
    EXPANSION_LIMIT_EXCEEDED = "EXPANSION_LIMIT_EXCEEDED"
    AMBIGUOUS_LOCATOR = "AMBIGUOUS_LOCATOR"
    XML_DTD_NOT_ALLOWED = "XML_DTD_NOT_ALLOWED"


class ExtractionWarningCode(StrEnum):
    """Non-fatal extraction observations a human reviewer should see."""

    BYTE_ORDER_MARK_STRIPPED = "BYTE_ORDER_MARK_STRIPPED"
    DELIMITER_INFERRED = "DELIMITER_INFERRED"
    RAGGED_ROWS = "RAGGED_ROWS"
    MERGED_CELLS_EXPANDED = "MERGED_CELLS_EXPANDED"
    INLINE_STRINGS_PRESENT = "INLINE_STRINGS_PRESENT"
    EMPTY_UNITS_SKIPPED = "EMPTY_UNITS_SKIPPED"
    UNSTRUCTURED_DOCUMENT = "UNSTRUCTURED_DOCUMENT"


class DocumentUnitKind(StrEnum):
    """The structural unit a locator addresses."""

    SECTION = "SECTION"
    PARAGRAPH = "PARAGRAPH"
    LIST_ITEM = "LIST_ITEM"
    TABLE_ROW = "TABLE_ROW"
    SHEET_ROW = "SHEET_ROW"


@dataclass(frozen=True, slots=True, kw_only=True)
class NormalizedDocumentUnit:
    """One addressable unit of a normalized policy document.

    `locator`는 문서 구조에서 나오므로 원문이 재조판돼도 같은 단위를 다시 가리킨다.
    `text_sha256`은 정규화된 unit 텍스트의 SHA-256이며, 그대로 `SourceReference`의
    `content_sha256`이 된다. 텍스트 자체는 여기에 없다.
    """

    locator: str
    kind: DocumentUnitKind
    text_sha256: str
    text_length: int
    origin: str

    def __post_init__(self) -> None:
        for name in ("locator", "text_sha256", "origin"):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.kind, DocumentUnitKind):
            raise TypeError("kind must be a DocumentUnitKind")
        if isinstance(self.text_length, bool) or not isinstance(self.text_length, int):
            raise TypeError("text_length must be an integer")
        if self.text_length <= 0:
            raise ValueError("text_length must be positive")

    def to_dict(self) -> dict[str, object]:
        return {
            "locator": self.locator,
            "kind": self.kind.value,
            "text_sha256": self.text_sha256,
            "text_length": self.text_length,
            "origin": self.origin,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class NormalizedPolicyDocument:
    """The common parser output every supported format must produce.

    원본 identity 5-tuple(`source_id`, `source_version`, `artifact_id`, `s3_version_id`,
    `content_sha256`)은 `docs/POLICY_INGESTION.md`의 Original finalization 규칙이 상태 전이와
    승인을 묶는 값이다. Parser는 이 tuple을 인용만 하고 만들지 않는다.

    탐지 결과와 Parser/정규화 Artifact 항목은 `FAILED`에서만 비어 있을 수 있다. 미지원 형식은
    Parser에 도달하지 못하므로 그 값들이 존재한다고 가정하면 실패 경로를 표현할 수 없다.
    """

    source_id: str
    source_version: str
    artifact_id: str
    s3_version_id: str
    content_sha256: str
    filename: str
    declared_media_type: str
    byte_size: int
    status: IngestionStatus
    detected_media_type: str | None = None
    source_format: PolicySourceFormat | None = None
    parser_id: str | None = None
    parser_version: str | None = None
    normalized_artifact_id: str | None = None
    normalized_sha256: str | None = None
    units: tuple[NormalizedDocumentUnit, ...] = ()
    warnings: tuple[ExtractionWarningCode, ...] = ()
    failure_code: IngestionFailureCode | None = None
    #: `REVIEW_REQUIRED`를 사람이 확인해 `READY`로 올린 기록. 게이트가 의미를 가지려면 **누가
    #: 언제** 판단했는지가 남아야 한다 — 남기지 않으면 그 상태 전이는 자동화와 구별되지 않는다.
    #: 둘은 함께 있거나 함께 없다. 파싱 직후 `READY`가 된 문서는 검토가 필요 없었으므로 비어 있다.
    reviewed_by: str | None = None
    reviewed_at: str | None = None

    # `FAILED`가 아닌 문서가 반드시 채워야 하는 정규화 결과 항목.
    _NORMALIZED_FIELDS = (
        "detected_media_type",
        "parser_id",
        "parser_version",
        "normalized_artifact_id",
        "normalized_sha256",
    )

    def __post_init__(self) -> None:
        for name in (
            "source_id",
            "source_version",
            "artifact_id",
            "s3_version_id",
            "content_sha256",
            "filename",
            "declared_media_type",
        ):
            require_non_empty_string(getattr(self, name), name)
        for name in self._NORMALIZED_FIELDS:
            require_optional_non_empty_string(getattr(self, name), name)
        if self.source_format is not None and not isinstance(
            self.source_format, PolicySourceFormat
        ):
            raise TypeError("source_format must be a PolicySourceFormat")
        if not isinstance(self.status, IngestionStatus):
            raise TypeError("status must be an IngestionStatus")
        if isinstance(self.byte_size, bool) or not isinstance(self.byte_size, int):
            raise TypeError("byte_size must be an integer")
        if self.byte_size <= 0:
            raise ValueError("byte_size must be positive")
        for unit in self.units:
            if not isinstance(unit, NormalizedDocumentUnit):
                raise TypeError("units items must be NormalizedDocumentUnit values")
        for warning in self.warnings:
            if not isinstance(warning, ExtractionWarningCode):
                raise TypeError("warnings items must be ExtractionWarningCode values")
        if self.failure_code is not None and not isinstance(
            self.failure_code, IngestionFailureCode
        ):
            raise TypeError("failure_code must be an IngestionFailureCode")
        self._require_unique_locators()
        self._require_consistent_outcome()
        self._require_review_provenance()

    def _require_unique_locators(self) -> None:
        """Locator는 문서 안에서 유일해야 한다. 중복이면 Evidence가 두 단위를 가리킨다."""
        seen: set[str] = set()
        for unit in self.units:
            if unit.locator in seen:
                raise ValueError(f"duplicate unit locator {unit.locator!r}")
            seen.add(unit.locator)

    def _require_consistent_outcome(self) -> None:
        """실패 상태와 실패 코드, 성공 상태와 정규화 결과를 서로 강제한다."""
        if self.status is IngestionStatus.FAILED:
            if self.failure_code is None:
                raise ValueError("a FAILED document must carry a failure_code")
            if self.units:
                raise ValueError("a FAILED document must not carry units")
            return
        if self.failure_code is not None:
            raise ValueError("failure_code is only valid on a FAILED document")
        if self.source_format is None:
            raise ValueError("a non-FAILED document must carry a source_format")
        missing = [name for name in self._NORMALIZED_FIELDS if getattr(self, name) is None]
        if missing:
            raise ValueError("a non-FAILED document must carry " + ", ".join(sorted(missing)))
        if not self.units:
            raise ValueError("a non-FAILED document must carry at least one unit")

    def _require_review_provenance(self) -> None:
        """검토 기록은 둘이 함께 있어야 하고, 검토가 성립하는 상태에만 붙는다."""
        if (self.reviewed_by is None) != (self.reviewed_at is None):
            raise ValueError("reviewed_by and reviewed_at must be provided together")
        if self.reviewed_by is None:
            return
        require_non_empty_string(self.reviewed_by, "reviewed_by")
        require_offset_aware_timestamp(self.reviewed_at, "reviewed_at")
        if self.status is not IngestionStatus.READY:
            # 검토 기록은 검토의 **결과**다. READY가 아닌 문서가 그것을 들고 있으면 "확인했는데
            # 여전히 확인 대기"라는 모순된 상태가 저장된다.
            raise ValueError("a reviewed document must be READY")
        if not self.warnings:
            # 경고가 없으면 자동으로 READY가 됐을 문서다. 거기에 검토 기록을 붙이면 사람이
            # 판단해야 했던 문서와 그렇지 않은 문서를 더는 구별할 수 없다.
            raise ValueError("only a document that required review may carry review provenance")

    @property
    def is_approvable(self) -> bool:
        """Whether a human approval may attach to this exact source version."""
        return self.status in APPROVABLE_STATUSES

    @property
    def needs_review(self) -> bool:
        """Whether a person still has to confirm the extraction before anything may use it."""
        return self.status is IngestionStatus.REVIEW_REQUIRED

    def confirmed_by_review(self, *, reviewer: str, reviewed_at: str) -> NormalizedPolicyDocument:
        """Return the READY copy of a `REVIEW_REQUIRED` document a person has confirmed.

        **왜 이 전이가 필요한가.** 병합 셀·불규칙 행 같은 경고가 붙은 문서는 자동으로 `READY`가
        되지 않는다(`REVIEW_REQUIRED_WARNINGS`) — locator가 근거로 쓸 만한지는 사람이 추출 결과를
        보고 판단할 일이기 때문이다. 그런데 그 판단을 **입력할 문이 없으면** 게이트가 아니라
        막다른 길이다. 라이브에서 ISMS-P 엑셀(334 unit, 병합 셀 경고)이 그 상태로 멈췄고,
        authoring worker는 `SOURCE_NOT_READY`로 무한 재시도했다.

        경고는 지운다고 없어지는 사실이 아니므로 **그대로 남긴다.** 남는 것은 "이 경고를 보고
        사람이 진행을 결정했다"는 기록이며, 그것이 `reviewed_by`/`reviewed_at`이다.
        """
        if not self.needs_review:
            raise ValueError("only a REVIEW_REQUIRED document can be confirmed by review")
        return replace(
            self,
            status=IngestionStatus.READY,
            reviewed_by=reviewer,
            reviewed_at=reviewed_at,
        )

    def unit(self, locator: str) -> NormalizedDocumentUnit | None:
        for unit in self.units:
            if unit.locator == locator:
                return unit
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "source_version": self.source_version,
            "artifact_id": self.artifact_id,
            "s3_version_id": self.s3_version_id,
            "content_sha256": self.content_sha256,
            "filename": self.filename,
            "declared_media_type": self.declared_media_type,
            "detected_media_type": self.detected_media_type,
            "source_format": None if self.source_format is None else self.source_format.value,
            "byte_size": self.byte_size,
            "parser_id": self.parser_id,
            "parser_version": self.parser_version,
            "normalized_artifact_id": self.normalized_artifact_id,
            "normalized_sha256": self.normalized_sha256,
            "status": self.status.value,
            "units": [unit.to_dict() for unit in self.units],
            "warnings": [warning.value for warning in self.warnings],
            "failure_code": None if self.failure_code is None else self.failure_code.value,
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicySourceUploadRequest:
    """What a client may state when starting an upload session.

    `customer_id`, bucket, object key, `source_id`, `source_version`, 처리 상태는 여기에 없다.
    `docs/POLICY_INGESTION.md`의 Original finalization 규칙상 Backend가 발급한다.
    """

    filename: str
    declared_media_type: str
    byte_size: int
    title: str | None = None

    def __post_init__(self) -> None:
        require_non_empty_string(self.filename, "filename")
        require_non_empty_string(self.declared_media_type, "declared_media_type")
        require_optional_non_empty_string(self.title, "title")
        if isinstance(self.byte_size, bool) or not isinstance(self.byte_size, int):
            raise TypeError("byte_size must be an integer")
        if self.byte_size <= 0:
            raise ValueError("byte_size must be positive")

    def to_dict(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "declared_media_type": self.declared_media_type,
            "byte_size": self.byte_size,
            "title": self.title,
        }
