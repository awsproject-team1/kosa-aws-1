"""Shared builders for the policy authoring tests.

정책 원문은 저장소에 없다 (ADR-0004). 여기서 만드는 문장은 원문이 아니라 구조만 재현한 합성
텍스트이며, 정규화 artifact 바이트와 그 digest를 실제 계산해 Artifact Reader의 검증 경로를
그대로 통과시킨다. digest를 손으로 적으면 검증이 무엇을 확인하는지 테스트가 증명하지 못한다.
"""

from __future__ import annotations

import json
from hashlib import sha256

from apps.backend.policy.ingestion.normalization import (
    NORMALIZED_SCHEMA_VERSION,
    normalize_text,
    text_sha256,
)
from packages.contracts import (
    DocumentUnitKind,
    IngestionStatus,
    NormalizedDocumentUnit,
    NormalizedPolicyDocument,
    PolicySourceFormat,
)

SOURCE_ID = "internal-cloud-security-checklist"
SOURCE_VERSION = "2026-09-01"
ARTIFACT_ID = "artifact-001"
S3_VERSION_ID = "s3-version-001"

# 합성 정책 문장. 첫 두 개는 자동 평가 가능한 통제를, 세 번째는 사람 검토 통제를 흉내 낸다.
UNIT_TEXTS: tuple[tuple[str, DocumentUnitKind, str], ...] = (
    (
        "heading/access-control/item/1",
        DocumentUnitKind.LIST_ITEM,
        "All object storage buckets must block every form of public access.",
    ),
    (
        "heading/encryption/item/1",
        DocumentUnitKind.LIST_ITEM,
        "Data stored in managed databases must be encrypted at rest.",
    ),
    (
        "heading/governance/item/1",
        DocumentUnitKind.LIST_ITEM,
        "The security officer reviews third-party processor agreements every year.",
    ),
    (
        "heading/facilities/item/1",
        DocumentUnitKind.LIST_ITEM,
        "Physical entry to the data centre is logged and reviewed monthly.",
    ),
)


def unit_text(locator: str) -> str:
    for candidate, _, text in UNIT_TEXTS:
        if candidate == locator:
            return text
    raise KeyError(locator)


def normalized_artifact_bytes(
    units: tuple[tuple[str, DocumentUnitKind, str], ...] = UNIT_TEXTS,
) -> bytes:
    """The exact normalized artifact object the writer produces and the reader accepts.

    직렬화 형태를 `_normalized_artifact()`와 같게 유지한다 — key 집합·순서·구분자까지 같아야
    Reader의 exact schema 검사와 digest 비교가 실제 경로를 검사하는 것이 된다.
    """
    payload = {
        "schema_version": NORMALIZED_SCHEMA_VERSION,
        "units": [
            {
                "locator": locator,
                "kind": kind.value,
                "origin": f"line/{index}-{index}",
                "text_sha256": text_sha256(text),
                "text": normalize_text(text),
            }
            for index, (locator, kind, text) in enumerate(units, start=1)
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    return encoded.encode("utf-8") + b"\n"


def ready_document(**overrides: object) -> NormalizedPolicyDocument:
    """A READY normalized document whose unit digests match `normalized_artifact_bytes()`."""
    artifact = normalized_artifact_bytes()
    units = tuple(
        NormalizedDocumentUnit(
            locator=locator,
            kind=kind,
            text_sha256=text_sha256(text),
            text_length=len(normalize_text(text)),
            origin=f"line/{index}-{index}",
        )
        for index, (locator, kind, text) in enumerate(UNIT_TEXTS, start=1)
    )
    fields: dict[str, object] = {
        "source_id": SOURCE_ID,
        "source_version": SOURCE_VERSION,
        "artifact_id": ARTIFACT_ID,
        "s3_version_id": S3_VERSION_ID,
        "content_sha256": "b" * 64,
        "filename": "policy.md",
        "declared_media_type": "text/markdown",
        "byte_size": 512,
        "status": IngestionStatus.READY,
        "detected_media_type": "text/markdown",
        "source_format": PolicySourceFormat.MARKDOWN,
        "parser_id": "markdown-parser",
        "parser_version": "1.0.0",
        "normalized_artifact_id": f"{ARTIFACT_ID}#normalized",
        "normalized_sha256": sha256(artifact).hexdigest(),
        "units": units,
    }
    fields.update(overrides)
    return NormalizedPolicyDocument(**fields)  # type: ignore[arg-type]
