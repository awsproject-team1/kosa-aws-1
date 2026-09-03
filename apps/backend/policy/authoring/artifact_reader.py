"""Read the protected normalized artifact and prove it is the approved version's text.

Extractor에 넘어가는 텍스트는 이 모듈을 통해서만 생긴다. 그리고 이 모듈은 **읽은 바이트가
승인 경계를 통과한 그 판본의 정규화 결과가 맞는지**를 전부 확인한 뒤에만 텍스트를 내놓는다.
확인 항목 중 하나라도 실패하면 후보 하나를 버리는 것이 아니라 추출 전체를 중단한다 — 어느
문서를 읽고 있는지 더 이상 말할 수 없는 상태이기 때문이다.

    READY 상태 확인
    → normalized S3 object read
    → 최대 바이트 크기 확인
    → payload SHA-256 확인
    → exact JSON schema 확인
    → unit 수와 순서 확인
    → locator/kind/origin 확인
    → normalize_text(text) digest 확인
    → ExtractionUnit 생성

`ExtractionUnit`은 `to_dict()`를 제공하지 않는다. 정책 원문이 DynamoDB item·API 응답·log로
흘러가는 경로를 규율이 아니라 **구조**로 막는다. 직렬화가 없으면 실수로 저장할 수 없다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from apps.backend.policy.ingestion.normalization import (
    NORMALIZED_SCHEMA_VERSION,
    normalize_text,
)
from apps.backend.policy.ingestion.storage_keys import normalized_object_key
from packages.contracts import (
    ArtifactReadFailureCode,
    DocumentUnitKind,
    NormalizedPolicyDocument,
)

#: 한 번에 읽어 메모리에 올릴 정규화 artifact의 상한. 정규화 단계의 unit 상한(20,000)과
#: unit당 텍스트 크기를 함께 감당할 수 있으면서, worker 한 번의 메모리를 고정한다.
MAX_NORMALIZED_ARTIFACT_BYTES = 16 * 1024 * 1024

_UNIT_FIELDS = frozenset({"locator", "kind", "origin", "text_sha256", "text"})
_DOCUMENT_FIELDS = frozenset({"schema_version", "units"})


class ArtifactReadError(RuntimeError):
    """Raised when the normalized artifact cannot be trusted as extraction input.

    사유는 열거값으로만 표현한다. 메시지에 정책 문장이나 locator 내용을 담지 않는다.
    """

    def __init__(self, code: ArtifactReadFailureCode) -> None:
        super().__init__(code.value)
        self.code = code


class ObjectReader(Protocol):
    def get_object(self, **kwargs: object) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtractionUnit:
    """One verified unit of policy text, addressable only inside the extraction worker.

    **직렬화 메서드가 없는 것은 실수가 아니다.** `to_dict()`나 `__str__` 재정의를 추가하면
    이 값이 저장·응답·로그 경로로 나갈 수 있게 된다. 리뷰어에게 보여줄 문장은 모델이 쓴
    `ExtractedRequirement.requirement`이고, 원문은 이 타입 안에만 있다.
    """

    locator: str
    kind: DocumentUnitKind
    origin: str
    text: str
    text_sha256: str

    def __post_init__(self) -> None:
        for name in ("locator", "origin", "text", "text_sha256"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.kind, DocumentUnitKind):
            raise TypeError("kind must be a DocumentUnitKind")

    def __repr__(self) -> str:
        """Redact the text. `repr()`은 예외 메시지와 로그로 가장 쉽게 새는 경로다."""
        return f"ExtractionUnit(locator={self.locator!r}, text=<redacted>)"


class NormalizedArtifactReader:
    """Fetch and verify the normalized artifact for one exact source version."""

    def __init__(
        self,
        *,
        reader: ObjectReader,
        bucket: str,
        max_bytes: int = MAX_NORMALIZED_ARTIFACT_BYTES,
    ) -> None:
        if reader is None or not hasattr(reader, "get_object"):
            raise TypeError("reader must provide get_object")
        if not isinstance(bucket, str) or not bucket.strip():
            raise ValueError("bucket must be a non-empty string")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self._reader = reader
        self._bucket = bucket
        self._max_bytes = max_bytes

    def read(
        self, *, customer_id: str, document: NormalizedPolicyDocument
    ) -> tuple[ExtractionUnit, ...]:
        if not isinstance(customer_id, str) or not customer_id.strip():
            raise ValueError("customer_id must be a non-empty string")
        if not isinstance(document, NormalizedPolicyDocument):
            raise TypeError("document must be a NormalizedPolicyDocument")
        if not document.is_approvable:
            # READY가 아닌 판본은 사람이 승인할 수 없다. 그런 문서에서 후보를 만들면 승인
            # 경계를 우회한 Rule이 생긴다.
            raise ArtifactReadError(ArtifactReadFailureCode.SOURCE_NOT_READY)

        payload = self._fetch(customer_id, document)
        if sha256(payload).hexdigest() != document.normalized_sha256:
            raise ArtifactReadError(ArtifactReadFailureCode.CONTENT_DIGEST_MISMATCH)
        raw_units = _parse_artifact(payload)
        return _build_units(raw_units, document)

    def _fetch(self, customer_id: str, document: NormalizedPolicyDocument) -> bytes:
        key = normalized_object_key(
            customer_id=customer_id,
            source_id=document.source_id,
            source_version=document.source_version,
        )
        try:
            response = self._reader.get_object(Bucket=self._bucket, Key=key)
        except Exception:
            raise ArtifactReadError(ArtifactReadFailureCode.ARTIFACT_NOT_FOUND) from None
        body = response.get("Body")
        read = getattr(body, "read", None)
        if not callable(read):
            raise ArtifactReadError(ArtifactReadFailureCode.ARTIFACT_NOT_FOUND)
        # 상한보다 한 바이트 더 읽어, 정확히 상한인 객체와 상한을 넘는 객체를 구별한다.
        payload = read(self._max_bytes + 1)
        if not isinstance(payload, bytes) or not payload:
            raise ArtifactReadError(ArtifactReadFailureCode.ARTIFACT_NOT_FOUND)
        if len(payload) > self._max_bytes:
            raise ArtifactReadError(ArtifactReadFailureCode.ARTIFACT_TOO_LARGE)
        return payload


def _parse_artifact(payload: bytes) -> list[Mapping[str, object]]:
    """Require the exact schema the normalizer writes — no extra or missing keys."""
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ArtifactReadError(ArtifactReadFailureCode.ARTIFACT_SCHEMA_INVALID) from None
    if not isinstance(document, dict) or set(document) != _DOCUMENT_FIELDS:
        raise ArtifactReadError(ArtifactReadFailureCode.ARTIFACT_SCHEMA_INVALID)
    if document.get("schema_version") != NORMALIZED_SCHEMA_VERSION:
        raise ArtifactReadError(ArtifactReadFailureCode.ARTIFACT_SCHEMA_INVALID)
    units = document.get("units")
    if not isinstance(units, list) or not units:
        raise ArtifactReadError(ArtifactReadFailureCode.ARTIFACT_SCHEMA_INVALID)
    for entry in units:
        if not isinstance(entry, dict) or set(entry) != _UNIT_FIELDS:
            raise ArtifactReadError(ArtifactReadFailureCode.ARTIFACT_SCHEMA_INVALID)
        for name in _UNIT_FIELDS:
            if not isinstance(entry[name], str) or not entry[name]:
                raise ArtifactReadError(ArtifactReadFailureCode.ARTIFACT_SCHEMA_INVALID)
    return units


def _build_units(
    raw_units: list[Mapping[str, object]], document: NormalizedPolicyDocument
) -> tuple[ExtractionUnit, ...]:
    """Cross-check the artifact against the approved document unit-by-unit, in order.

    집합만 비교하지 않고 **순서까지** 대조한다. 정규화 artifact의 unit 순서는 `text_sha256`
    목록과 짝지어 문서를 재구성하는 근거이며, 순서가 다르면 같은 locator 집합이라도 다른
    문서다.
    """
    if len(raw_units) != len(document.units):
        raise ArtifactReadError(ArtifactReadFailureCode.UNIT_SET_MISMATCH)

    units: list[ExtractionUnit] = []
    for raw, expected in zip(raw_units, document.units, strict=True):
        if raw["locator"] != expected.locator or raw["origin"] != expected.origin:
            raise ArtifactReadError(ArtifactReadFailureCode.UNIT_SET_MISMATCH)
        try:
            kind = DocumentUnitKind(raw["kind"])
        except ValueError:
            raise ArtifactReadError(ArtifactReadFailureCode.ARTIFACT_SCHEMA_INVALID) from None
        if kind is not expected.kind:
            raise ArtifactReadError(ArtifactReadFailureCode.UNIT_SET_MISMATCH)

        text = normalize_text(str(raw["text"]))
        digest = sha256(text.encode("utf-8")).hexdigest()
        # artifact가 스스로 적어 온 digest와, 승인된 문서가 기록한 digest 둘 다와 맞아야 한다.
        # 하나만 보면 artifact 안에서 일관되기만 한 위조 텍스트가 통과한다.
        if digest != raw["text_sha256"] or digest != expected.text_sha256:
            raise ArtifactReadError(ArtifactReadFailureCode.UNIT_DIGEST_MISMATCH)
        units.append(
            ExtractionUnit(
                locator=expected.locator,
                kind=kind,
                origin=expected.origin,
                text=text,
                text_sha256=digest,
            )
        )
    return tuple(units)
