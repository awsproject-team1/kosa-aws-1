"""Security boundary tests for customer policy ingestion.

두 가지를 고정한다. (1) 압축 폭탄과 과대 업로드가 읽히기 전에 거부된다. (2) 원문 문장이
Contract 직렬화, 실패 코드, 오류 메시지 어디에도 나타나지 않는다 (`docs/POLICY_INGESTION.md`
Acceptance criteria: "정책 원문이나 추출 텍스트가 Git diff, Queue payload, 운영 로그에
노출되지 않는다").
"""

import hashlib
import json
import sys
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "unit"))

from ingestion_fixtures import build_xlsx, inline_cell, sheet_row  # noqa: E402

from apps.backend.policy.ingestion import (  # noqa: E402
    MAX_DOCUMENT_BYTES,
    DocumentFormatError,
    UploadedPolicyOriginal,
    detect_format,
    normalize_upload,
)
from apps.backend.policy.ingestion.formats import (  # noqa: E402
    MAX_ARCHIVE_ENTRIES,
    require_safe_archive,
)
from packages.contracts import (  # noqa: E402
    FORMAT_MEDIA_TYPES,
    IngestionFailureCode,
    IngestionStatus,
    PolicySourceFormat,
)

# 원문에만 있어야 하는 표지 문장. 이 문자열이 Contract나 오류 어디에 나타나면 유출이다.
SECRET_SENTENCE = "INTERNAL-ONLY 사내 정책 문장 do-not-leak"


def _original(payload: bytes, media_type: str, filename: str) -> UploadedPolicyOriginal:
    return UploadedPolicyOriginal(
        source_id="internal-cloud-security-checklist",
        source_version="2026-09-01",
        artifact_id="artifact-001",
        s3_version_id="s3-version-001",
        content_sha256=hashlib.sha256(payload).hexdigest(),
        filename=filename,
        declared_media_type=media_type,
        byte_size=len(payload),
    )


class ArchiveExpansionLimitTest(unittest.TestCase):
    def test_rejects_a_highly_compressible_entry_before_reading_it(self) -> None:
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("xl/workbook.xml", b"\x00" * (8 * 1024 * 1024))

        with self.assertRaises(DocumentFormatError) as raised:
            require_safe_archive(buffer.getvalue())
        self.assertEqual(
            raised.exception.failure_code, IngestionFailureCode.EXPANSION_LIMIT_EXCEEDED
        )

    def test_rejects_an_archive_with_too_many_entries(self) -> None:
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for index in range(MAX_ARCHIVE_ENTRIES + 1):
                archive.writestr(f"part-{index}.xml", f"<x>{index}</x>")

        with self.assertRaises(DocumentFormatError) as raised:
            require_safe_archive(buffer.getvalue())
        self.assertEqual(
            raised.exception.failure_code, IngestionFailureCode.EXPANSION_LIMIT_EXCEEDED
        )

    def test_accepts_an_ordinary_workbook(self) -> None:
        payload = build_xlsx(sheets=[("Security", sheet_row(1, inline_cell("A", 1, "5.2.1")))])

        require_safe_archive(payload)

    def test_rejects_an_upload_beyond_the_document_size_limit(self) -> None:
        with self.assertRaises(DocumentFormatError) as raised:
            detect_format(b"a" * (MAX_DOCUMENT_BYTES + 1), declared=PolicySourceFormat.PLAIN_TEXT)

        self.assertEqual(raised.exception.failure_code, IngestionFailureCode.DOCUMENT_TOO_LARGE)

    def test_rejects_a_corrupted_archive(self) -> None:
        payload = bytearray(
            build_xlsx(sheets=[("Security", sheet_row(1, inline_cell("A", 1, "5.2.1")))])
        )
        payload[8:64] = b"\x00" * 56

        outcome = normalize_upload(
            _original(bytes(payload), FORMAT_MEDIA_TYPES[PolicySourceFormat.XLSX], "policy.xlsx"),
            bytes(payload),
        )
        self.assertEqual(outcome.document.status, IngestionStatus.FAILED)
        self.assertIn(
            outcome.document.failure_code,
            {IngestionFailureCode.CORRUPTED_DOCUMENT, IngestionFailureCode.MEDIA_TYPE_MISMATCH},
        )


class NoPolicyTextLeakTest(unittest.TestCase):
    """Contract 직렬화 어디에도 원문 문장이 없어야 한다."""

    def test_the_normalized_document_carries_hashes_not_text(self) -> None:
        payload = f"# 정책\n\n{SECRET_SENTENCE}\n".encode()

        outcome = normalize_upload(_original(payload, "text/markdown", "policy.md"), payload)
        serialized = json.dumps(outcome.document.to_dict(), ensure_ascii=False)

        self.assertNotIn(SECRET_SENTENCE, serialized)
        self.assertNotIn("do-not-leak", serialized)
        self.assertIn(
            hashlib.sha256(SECRET_SENTENCE.encode()).hexdigest(),
            serialized,
            "the unit digest should still make the sentence verifiable",
        )

    def test_the_text_lives_only_in_the_normalized_artifact(self) -> None:
        payload = f"# 정책\n\n{SECRET_SENTENCE}\n".encode()

        outcome = normalize_upload(_original(payload, "text/markdown", "policy.md"), payload)
        assert outcome.normalized_payload is not None
        self.assertIn(SECRET_SENTENCE, outcome.normalized_payload.decode("utf-8"))

    def test_a_failure_reports_a_code_without_quoting_the_document(self) -> None:
        payload = f"%PDF-1.7\n{SECRET_SENTENCE}\n".encode()

        outcome = normalize_upload(_original(payload, "application/pdf", "policy.pdf"), payload)
        serialized = json.dumps(outcome.document.to_dict(), ensure_ascii=False)

        self.assertEqual(outcome.document.failure_code, IngestionFailureCode.UNSUPPORTED_FORMAT)
        self.assertNotIn(SECRET_SENTENCE, serialized)

    def test_parser_error_messages_do_not_quote_the_document(self) -> None:
        payload = SECRET_SENTENCE.encode("euc-kr")

        try:
            detect_format(payload, declared=PolicySourceFormat.PLAIN_TEXT)
        except DocumentFormatError as error:
            self.assertNotIn(SECRET_SENTENCE, str(error))
            self.assertEqual(error.failure_code, IngestionFailureCode.ENCODING_NOT_SUPPORTED)
        else:  # pragma: no cover - the encoding must be rejected
            self.fail("a non-UTF-8 document must be rejected")


if __name__ == "__main__":
    unittest.main()
