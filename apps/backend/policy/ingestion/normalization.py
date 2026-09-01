"""Shared normalization primitives every policy document parser uses.

정규화 규칙을 한 곳에 모으는 이유는 hash 비교 가능성이다. 같은 문장이 Markdown과 DOCX에서
나왔을 때 같은 `text_sha256`을 갖지 않으면 Evidence를 형식 간에 대조할 수 없고, Parser마다
정규화가 다르면 원문 개정 없이도 hash가 흔들린다.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field

from packages.contracts.policy_ingestion import (
    DocumentUnitKind,
    ExtractionWarningCode,
    IngestionFailureCode,
    NormalizedDocumentUnit,
)

NORMALIZED_SCHEMA_VERSION = "policy-normalized-document/1"

# 한 문서가 만들 수 있는 unit 상한. `DocumentBuilder`가 강제하므로 형식과 무관하게 적용된다.
# Parser마다 따로 걸면 새 형식을 추가할 때 빠뜨리기 쉽다.
MAX_UNITS = 20_000

_WHITESPACE_RUN = re.compile(r"[ \t 　]+")
_SLUG_STRIP = re.compile(r"[^0-9A-Za-z가-힣]+")


class DocumentParseError(ValueError):
    """Raised when a supported format cannot be normalized.

    사유는 `failure_code`로만 표현하며 메시지에 원문 내용을 담지 않는다.
    """

    def __init__(self, failure_code: IngestionFailureCode, message: str) -> None:
        super().__init__(message)
        self.failure_code = failure_code


def normalize_text(text: str) -> str:
    """Normalize one unit's text so its digest survives reformatting.

    NFC 정규화, 줄바꿈 통일, 가로 공백 축약, 줄 단위 trim, 앞뒤 빈 줄 제거만 한다. 단어를
    지우거나 대소문자를 바꾸지 않는다 — 정규화가 내용을 바꾸면 사람이 승인한 문장과 hash가
    가리키는 문장이 달라진다.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    normalized = unicodedata.normalize("NFC", text)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_WHITESPACE_RUN.sub(" ", line).strip() for line in normalized.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def text_sha256(text: str) -> str:
    """Digest of normalized unit text. `SourceReference.content_sha256`와 같은 값이다."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slug(value: str) -> str:
    """Build a stable locator segment from a heading or sheet name.

    한글을 보존한다. ISMS-P와 사내 체크리스트가 한글 제목을 쓰므로 ASCII만 남기면 서로 다른
    절이 모두 빈 slug로 무너진다.
    """
    normalized = unicodedata.normalize("NFC", value).strip().lower()
    collapsed = _SLUG_STRIP.sub("-", normalized).strip("-")
    return collapsed or "section"


@dataclass(slots=True)
class DocumentBuilder:
    """Accumulate normalized units and the normalized artifact payload together.

    Contract가 텍스트를 담지 않으므로 텍스트는 여기서만 살아 있다가 정규화 Artifact 바이트로
    빠져나간다. unit metadata와 artifact가 같은 호출로 만들어져 서로 어긋날 수 없다.
    """

    units: list[NormalizedDocumentUnit] = field(default_factory=list)
    warnings: list[ExtractionWarningCode] = field(default_factory=list)
    _texts: list[str] = field(default_factory=list)
    _locators: set[str] = field(default_factory=set)
    _skipped_empty: bool = False

    def add(self, *, locator: str, kind: DocumentUnitKind, origin: str, text: str) -> bool:
        """Add one unit. Empty units are skipped, not stored as zero-length evidence."""
        normalized = normalize_text(text)
        if not normalized:
            self._skipped_empty = True
            return False
        if len(self.units) >= MAX_UNITS:
            raise DocumentParseError(
                IngestionFailureCode.EXPANSION_LIMIT_EXCEEDED,
                f"the document produces more than {MAX_UNITS} units",
            )
        if locator in self._locators:
            raise DocumentParseError(
                IngestionFailureCode.AMBIGUOUS_LOCATOR,
                f"locator {locator!r} is produced by more than one document unit",
            )
        self._locators.add(locator)
        self.units.append(
            NormalizedDocumentUnit(
                locator=locator,
                kind=kind,
                text_sha256=text_sha256(normalized),
                text_length=len(normalized),
                origin=origin,
            )
        )
        self._texts.append(normalized)
        return True

    def warn(self, code: ExtractionWarningCode) -> None:
        if code not in self.warnings:
            self.warnings.append(code)

    def build(self) -> ParsedPolicyDocument:
        if self._skipped_empty:
            self.warn(ExtractionWarningCode.EMPTY_UNITS_SKIPPED)
        if not self.units:
            raise DocumentParseError(
                IngestionFailureCode.NO_TEXT_EXTRACTED,
                "the document produced no extractable text units",
            )
        payload = _normalized_artifact(self.units, self._texts)
        return ParsedPolicyDocument(
            units=tuple(self.units),
            warnings=tuple(self.warnings),
            normalized_payload=payload,
            normalized_sha256=hashlib.sha256(payload).hexdigest(),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ParsedPolicyDocument:
    """A parser result: unit metadata plus the normalized artifact bytes.

    `normalized_payload`가 추출 텍스트를 담는 **유일한** 값이며 S3 Artifact로만 나간다.
    `units`와 `normalized_sha256`은 Contract/DynamoDB/Queue로 흐른다.
    """

    units: tuple[NormalizedDocumentUnit, ...]
    warnings: tuple[ExtractionWarningCode, ...]
    normalized_payload: bytes
    normalized_sha256: str


def _normalized_artifact(units: list[NormalizedDocumentUnit], texts: list[str]) -> bytes:
    """Serialize the normalized document deterministically.

    같은 원본이 같은 바이트를 내야 `normalized_sha256`으로 재현성을 확인할 수 있다.
    """
    document = {
        "schema_version": NORMALIZED_SCHEMA_VERSION,
        "units": [
            {
                "locator": unit.locator,
                "kind": unit.kind.value,
                "origin": unit.origin,
                "text_sha256": unit.text_sha256,
                "text": text,
            }
            for unit, text in zip(units, texts, strict=True)
        ],
    }
    encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    return encoded.encode("utf-8") + b"\n"
