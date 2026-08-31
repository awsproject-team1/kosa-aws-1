"""Customer policy ingestion: format allow-list, parsers, and normalization.

`docs/POLICY_INGESTION.md`(ADR-0015)의 B 소유 경계다. 업로드 세션·저장·상태 write는 A가,
AI 후보 추출은 C가 담당하며 둘 다 여기서 나오는 `NormalizedPolicyDocument`를 소비한다.
"""

from apps.backend.policy.ingestion.formats import (
    MAX_ARCHIVE_TOTAL_BYTES,
    MAX_DOCUMENT_BYTES,
    DocumentFormatError,
    detect_format,
    format_for_media_type,
)
from apps.backend.policy.ingestion.normalization import (
    NORMALIZED_SCHEMA_VERSION,
    DocumentParseError,
    ParsedPolicyDocument,
    normalize_text,
    text_sha256,
)
from apps.backend.policy.ingestion.pipeline import (
    PARSERS,
    NormalizationOutcome,
    UploadedPolicyOriginal,
    normalize_upload,
    source_reference_for,
)

__all__ = [
    "DocumentFormatError",
    "DocumentParseError",
    "MAX_ARCHIVE_TOTAL_BYTES",
    "MAX_DOCUMENT_BYTES",
    "NORMALIZED_SCHEMA_VERSION",
    "NormalizationOutcome",
    "PARSERS",
    "ParsedPolicyDocument",
    "UploadedPolicyOriginal",
    "detect_format",
    "format_for_media_type",
    "normalize_text",
    "normalize_upload",
    "source_reference_for",
    "text_sha256",
]
