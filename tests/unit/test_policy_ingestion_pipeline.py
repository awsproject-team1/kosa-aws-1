"""Tests for format detection and the upload → normalized document pipeline."""

import hashlib
import unittest

from ingestion_fixtures import build_docx, build_xlsx, inline_cell, paragraph, sheet_row

from apps.backend.policy.ingestion import (
    DocumentFormatError,
    UploadedPolicyOriginal,
    detect_format,
    format_for_media_type,
    normalize_upload,
    source_reference_for,
)
from packages.contracts import (
    FORMAT_MEDIA_TYPES,
    IngestionFailureCode,
    IngestionStatus,
    PolicySourceFormat,
)

MARKDOWN = b"# Access Control\n\nAccounts must use least privilege.\n"


def _original(
    payload: bytes,
    *,
    media_type: str = "text/markdown",
    filename: str = "policy.md",
    **overrides: object,
) -> UploadedPolicyOriginal:
    fields: dict[str, object] = {
        "source_id": "internal-cloud-security-checklist",
        "source_version": "2026-09-01",
        "artifact_id": "artifact-001",
        "s3_version_id": "s3-version-001",
        "content_sha256": hashlib.sha256(payload).hexdigest(),
        "filename": filename,
        "declared_media_type": media_type,
        "byte_size": len(payload),
    }
    fields.update(overrides)
    return UploadedPolicyOriginal(**fields)  # type: ignore[arg-type]


class MediaTypeAllowListTest(unittest.TestCase):
    def test_maps_every_supported_media_type(self) -> None:
        for source_format, media_type in FORMAT_MEDIA_TYPES.items():
            self.assertEqual(format_for_media_type(media_type), source_format)

    def test_ignores_media_type_parameters(self) -> None:
        self.assertEqual(format_for_media_type("text/CSV; charset=utf-8"), PolicySourceFormat.CSV)

    def test_rejects_a_format_outside_the_allow_list(self) -> None:
        with self.assertRaises(DocumentFormatError) as raised:
            format_for_media_type("application/pdf")

        self.assertEqual(raised.exception.failure_code, IngestionFailureCode.UNSUPPORTED_FORMAT)


class FormatDetectionTest(unittest.TestCase):
    def test_detects_ooxml_by_package_contents_not_extension(self) -> None:
        workbook = build_xlsx(sheets=[("Sheet1", sheet_row(1, inline_cell("A", 1, "5.2.1")))])

        self.assertEqual(
            detect_format(workbook, declared=PolicySourceFormat.XLSX), PolicySourceFormat.XLSX
        )
        self.assertEqual(
            detect_format(build_docx(paragraph("A rule.")), declared=PolicySourceFormat.DOCX),
            PolicySourceFormat.DOCX,
        )

    def test_rejects_a_workbook_declared_as_a_document(self) -> None:
        workbook = build_xlsx(sheets=[("Sheet1", sheet_row(1, inline_cell("A", 1, "5.2.1")))])

        with self.assertRaises(DocumentFormatError) as raised:
            detect_format(workbook, declared=PolicySourceFormat.DOCX)
        self.assertEqual(raised.exception.failure_code, IngestionFailureCode.MEDIA_TYPE_MISMATCH)

    def test_rejects_a_workbook_declared_as_markdown(self) -> None:
        workbook = build_xlsx(sheets=[("Sheet1", sheet_row(1, inline_cell("A", 1, "5.2.1")))])

        with self.assertRaises(DocumentFormatError) as raised:
            detect_format(workbook, declared=PolicySourceFormat.MARKDOWN)
        self.assertEqual(raised.exception.failure_code, IngestionFailureCode.MEDIA_TYPE_MISMATCH)

    def test_rejects_text_declared_as_a_workbook(self) -> None:
        with self.assertRaises(DocumentFormatError) as raised:
            detect_format(MARKDOWN, declared=PolicySourceFormat.XLSX)
        self.assertEqual(raised.exception.failure_code, IngestionFailureCode.MEDIA_TYPE_MISMATCH)

    def test_text_formats_are_taken_from_the_declaration(self) -> None:
        for declared in (
            PolicySourceFormat.MARKDOWN,
            PolicySourceFormat.PLAIN_TEXT,
            PolicySourceFormat.CSV,
        ):
            self.assertEqual(detect_format(MARKDOWN, declared=declared), declared)

    def test_rejects_a_pdf_by_signature(self) -> None:
        with self.assertRaises(DocumentFormatError) as raised:
            detect_format(b"%PDF-1.7\n...", declared=PolicySourceFormat.PLAIN_TEXT)
        self.assertEqual(raised.exception.failure_code, IngestionFailureCode.UNSUPPORTED_FORMAT)

    def test_rejects_an_ole_compound_file_as_encrypted(self) -> None:
        """암호화된 XLSX/DOCX는 OLE 컨테이너로 저장되므로 signature로만 구분된다."""
        payload = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64

        with self.assertRaises(DocumentFormatError) as raised:
            detect_format(payload, declared=PolicySourceFormat.XLSX)
        self.assertEqual(raised.exception.failure_code, IngestionFailureCode.ENCRYPTED_DOCUMENT)

    def test_rejects_an_encrypted_ooxml_package(self) -> None:
        payload = build_xlsx(
            sheets=[("Sheet1", sheet_row(1, inline_cell("A", 1, "5.2.1")))],
            extra={"EncryptedPackage": "opaque"},
        )

        with self.assertRaises(DocumentFormatError) as raised:
            detect_format(payload, declared=PolicySourceFormat.XLSX)
        self.assertEqual(raised.exception.failure_code, IngestionFailureCode.ENCRYPTED_DOCUMENT)

    def test_rejects_an_empty_upload(self) -> None:
        with self.assertRaises(DocumentFormatError) as raised:
            detect_format(b"", declared=PolicySourceFormat.MARKDOWN)
        self.assertEqual(raised.exception.failure_code, IngestionFailureCode.CORRUPTED_DOCUMENT)


class NormalizeUploadTest(unittest.TestCase):
    def test_a_structured_markdown_upload_becomes_ready(self) -> None:
        outcome = normalize_upload(_original(MARKDOWN), MARKDOWN)

        document = outcome.document
        self.assertEqual(document.status, IngestionStatus.READY)
        self.assertTrue(document.is_approvable)
        self.assertEqual(document.source_format, PolicySourceFormat.MARKDOWN)
        self.assertEqual(document.detected_media_type, "text/markdown")
        self.assertEqual(document.parser_id, "markdown-parser")
        self.assertIsNotNone(outcome.normalized_payload)

    def test_the_normalized_digest_describes_the_returned_payload(self) -> None:
        outcome = normalize_upload(_original(MARKDOWN), MARKDOWN)

        assert outcome.normalized_payload is not None
        self.assertEqual(
            document_digest := hashlib.sha256(outcome.normalized_payload).hexdigest(),
            outcome.document.normalized_sha256,
        )
        self.assertEqual(len(document_digest), 64)

    def test_normalization_is_deterministic(self) -> None:
        first = normalize_upload(_original(MARKDOWN), MARKDOWN)
        second = normalize_upload(_original(MARKDOWN), MARKDOWN)

        self.assertEqual(first.normalized_payload, second.normalized_payload)
        self.assertEqual(first.document.to_dict(), second.document.to_dict())

    def test_an_unstructured_upload_requires_review(self) -> None:
        payload = b"Accounts must use least privilege.\n"

        outcome = normalize_upload(_original(payload, media_type="text/plain"), payload)
        self.assertEqual(outcome.document.status, IngestionStatus.REVIEW_REQUIRED)
        self.assertFalse(outcome.document.is_approvable)

    def test_a_merged_workbook_requires_review(self) -> None:
        """병합 확장은 사람이 원문과 대조해야 하므로 자동으로 READY가 되지 않는다."""
        payload = build_xlsx(
            sheets=[
                (
                    "Security",
                    '<mergeCells><mergeCell ref="A1:A2"/></mergeCells>'
                    + sheet_row(1, inline_cell("A", 1, "Access control"))
                    + sheet_row(2, inline_cell("B", 2, "5.2.2")),
                )
            ]
        )
        original = _original(
            payload, media_type=FORMAT_MEDIA_TYPES[PolicySourceFormat.XLSX], filename="policy.xlsx"
        )

        self.assertEqual(
            normalize_upload(original, payload).document.status, IngestionStatus.REVIEW_REQUIRED
        )

    def test_an_unsupported_format_fails_with_a_code_instead_of_raising(self) -> None:
        payload = b"%PDF-1.7\npolicy"

        outcome = normalize_upload(_original(payload, media_type="application/pdf"), payload)
        self.assertFalse(outcome.succeeded)
        self.assertEqual(outcome.document.status, IngestionStatus.FAILED)
        self.assertEqual(outcome.document.failure_code, IngestionFailureCode.UNSUPPORTED_FORMAT)
        self.assertEqual(outcome.document.units, ())
        self.assertIsNone(outcome.normalized_payload)

    def test_rejects_bytes_that_do_not_match_the_finalized_digest(self) -> None:
        """Finalize가 검증한 판본과 Parser가 읽은 바이트가 같은지 여기서도 확인한다."""
        original = _original(MARKDOWN)

        outcome = normalize_upload(original, MARKDOWN + b"tampered\n")
        self.assertEqual(outcome.document.failure_code, IngestionFailureCode.CORRUPTED_DOCUMENT)

    def test_rejects_bytes_whose_size_does_not_match(self) -> None:
        original = _original(MARKDOWN, byte_size=len(MARKDOWN) + 1)

        outcome = normalize_upload(original, MARKDOWN)
        self.assertEqual(outcome.document.failure_code, IngestionFailureCode.CORRUPTED_DOCUMENT)

    def test_the_document_carries_the_server_issued_identity_unchanged(self) -> None:
        original = _original(MARKDOWN)

        document = normalize_upload(original, MARKDOWN).document
        self.assertEqual(document.source_id, original.source_id)
        self.assertEqual(document.source_version, original.source_version)
        self.assertEqual(document.artifact_id, original.artifact_id)
        self.assertEqual(document.s3_version_id, original.s3_version_id)
        self.assertEqual(document.content_sha256, original.content_sha256)


class SourceReferenceBridgeTest(unittest.TestCase):
    def test_builds_the_canonical_evidence_reference_from_a_unit(self) -> None:
        document = normalize_upload(_original(MARKDOWN), MARKDOWN).document

        reference = source_reference_for(document, "heading/access-control/item/1")
        self.assertEqual(
            reference.evidence_reference,
            "internal-cloud-security-checklist@2026-09-01#heading/access-control/item/1",
        )
        unit = document.unit("heading/access-control/item/1")
        assert unit is not None
        self.assertEqual(reference.content_sha256, unit.text_sha256)

    def test_rejects_a_locator_the_document_does_not_contain(self) -> None:
        document = normalize_upload(_original(MARKDOWN), MARKDOWN).document

        with self.assertRaises(KeyError):
            source_reference_for(document, "heading/does-not-exist")

    def test_refuses_to_cite_a_document_awaiting_review(self) -> None:
        """사람 승인 전 Source는 Rule의 근거가 될 수 없다 (`docs/POLICY_INGESTION.md`)."""
        payload = b"Accounts must use least privilege.\n"
        document = normalize_upload(_original(payload, media_type="text/plain"), payload).document

        self.assertEqual(document.status, IngestionStatus.REVIEW_REQUIRED)
        with self.assertRaises(ValueError):
            source_reference_for(document, "block/1")

    def test_refuses_to_cite_a_failed_document(self) -> None:
        payload = b"%PDF-1.7\npolicy"
        document = normalize_upload(
            _original(payload, media_type="application/pdf"), payload
        ).document

        with self.assertRaises(ValueError):
            source_reference_for(document, "block/1")


if __name__ == "__main__":
    unittest.main()
