"""Contract tests for the customer policy ingestion boundary."""

import json
import re
import unittest
from pathlib import Path

from packages.contracts import (
    APPROVABLE_STATUSES,
    FORMAT_MEDIA_TYPES,
    SUPPORTED_MEDIA_TYPES,
    DocumentUnitKind,
    ExtractionWarningCode,
    IngestionFailureCode,
    IngestionStatus,
    NormalizedDocumentUnit,
    NormalizedPolicyDocument,
    PolicySourceFormat,
    PolicySourceUploadRequest,
)

INGESTION_DOC = Path(__file__).parents[2] / "docs" / "POLICY_INGESTION.md"

UNIT = NormalizedDocumentUnit(
    locator="heading/access-control/item/1",
    kind=DocumentUnitKind.LIST_ITEM,
    text_sha256="a" * 64,
    text_length=34,
    origin="line/3-3",
)

BASE = {
    "source_id": "internal-cloud-security-checklist",
    "source_version": "2026-09-01",
    "artifact_id": "artifact-001",
    "s3_version_id": "s3-version-001",
    "content_sha256": "b" * 64,
    "filename": "policy.md",
    "declared_media_type": "text/markdown",
    "byte_size": 128,
}

NORMALIZED = {
    "detected_media_type": "text/markdown",
    "source_format": PolicySourceFormat.MARKDOWN,
    "parser_id": "markdown-parser",
    "parser_version": "1.0.0",
    "normalized_artifact_id": "artifact-001#normalized",
    "normalized_sha256": "c" * 64,
}


def _document(**overrides: object) -> NormalizedPolicyDocument:
    fields: dict[str, object] = {
        **BASE,
        **NORMALIZED,
        "status": IngestionStatus.READY,
        "units": (UNIT,),
    }
    fields.update(overrides)
    return NormalizedPolicyDocument(**fields)  # type: ignore[arg-type]


class FormatAllowListContractTest(unittest.TestCase):
    def test_the_allow_list_matches_the_documented_format_policy(self) -> None:
        """Contract와 `docs/POLICY_INGESTION.md`가 같은 형식 목록을 말해야 한다."""
        section = INGESTION_DOC.read_text(encoding="utf-8").split("## Format policy", 1)[1]
        section = section.split("\n## ", 1)[0]
        documented = set()
        for line in section.splitlines():
            if not line.startswith("|"):
                continue
            columns = line.split("|")
            if len(columns) < 4:
                continue
            documented.update(re.findall(r"`([^`]+)`", columns[2]))

        self.assertEqual(documented, set(SUPPORTED_MEDIA_TYPES))

    def test_every_format_has_exactly_one_media_type(self) -> None:
        self.assertEqual(len(FORMAT_MEDIA_TYPES), len(PolicySourceFormat))
        self.assertEqual(len(SUPPORTED_MEDIA_TYPES), len(FORMAT_MEDIA_TYPES))

    def test_pdf_is_not_on_the_allow_list(self) -> None:
        self.assertNotIn("application/pdf", SUPPORTED_MEDIA_TYPES)

    def test_only_ready_may_be_approved(self) -> None:
        self.assertEqual(APPROVABLE_STATUSES, frozenset({IngestionStatus.READY}))


class NormalizedDocumentContractTest(unittest.TestCase):
    def test_serializes_every_documented_field(self) -> None:
        payload = _document(warnings=(ExtractionWarningCode.EMPTY_UNITS_SKIPPED,)).to_dict()

        self.assertEqual(
            set(payload),
            {
                "source_id",
                "source_version",
                "artifact_id",
                "s3_version_id",
                "content_sha256",
                "filename",
                "declared_media_type",
                "detected_media_type",
                "source_format",
                "byte_size",
                "parser_id",
                "parser_version",
                "normalized_artifact_id",
                "normalized_sha256",
                "status",
                "units",
                "warnings",
                "failure_code",
                "reviewed_by",
                "reviewed_at",
            },
        )
        self.assertEqual(payload["warnings"], ["EMPTY_UNITS_SKIPPED"])
        self.assertEqual(payload["units"][0]["locator"], UNIT.locator)
        # 검토가 필요 없었던 문서에는 검토 기록이 없다.
        self.assertEqual((payload["reviewed_by"], payload["reviewed_at"]), (None, None))

    def test_a_review_required_document_is_not_approvable_until_a_person_confirms(self) -> None:
        """게이트가 있으면 그 게이트를 통과할 문이 있어야 한다 — 없으면 막다른 길이다."""
        pending = _document(
            status=IngestionStatus.REVIEW_REQUIRED,
            warnings=(ExtractionWarningCode.MERGED_CELLS_EXPANDED,),
        )

        self.assertFalse(pending.is_approvable)
        self.assertTrue(pending.needs_review)

        confirmed = pending.confirmed_by_review(
            reviewer="admin-1", reviewed_at="2026-09-05T03:00:00+00:00"
        )

        self.assertIs(confirmed.status, IngestionStatus.READY)
        self.assertTrue(confirmed.is_approvable)
        self.assertFalse(confirmed.needs_review)
        self.assertEqual(confirmed.reviewed_by, "admin-1")
        # 경고는 지워지지 않는다. 사람이 본 것이 무엇이었는지가 남아야 한다.
        self.assertEqual(confirmed.warnings, (ExtractionWarningCode.MERGED_CELLS_EXPANDED,))

    def test_only_a_review_required_document_can_be_confirmed(self) -> None:
        for status in (IngestionStatus.READY, IngestionStatus.UPLOADED, IngestionStatus.PARSING):
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "REVIEW_REQUIRED"):
                _document(status=status).confirmed_by_review(
                    reviewer="admin-1", reviewed_at="2026-09-05T03:00:00+00:00"
                )

    def test_review_provenance_is_paired_and_only_on_a_reviewed_document(self) -> None:
        warned = {"warnings": (ExtractionWarningCode.MERGED_CELLS_EXPANDED,)}
        with self.assertRaisesRegex(ValueError, "provided together"):
            _document(**warned, reviewed_by="admin-1")
        with self.assertRaisesRegex(ValueError, "must be READY"):
            _document(
                **warned,
                status=IngestionStatus.REVIEW_REQUIRED,
                reviewed_by="admin-1",
                reviewed_at="2026-09-05T03:00:00+00:00",
            )
        # 경고가 없었던 문서는 애초에 사람 판단을 요구하지 않았다.
        with self.assertRaisesRegex(ValueError, "required review"):
            _document(reviewed_by="admin-1", reviewed_at="2026-09-05T03:00:00+00:00")

    def test_the_serialized_document_is_json_encodable(self) -> None:
        """Queue payload와 DynamoDB item이 이 형태를 그대로 나른다."""
        json.dumps(_document().to_dict())

    def test_a_failed_document_requires_a_failure_code(self) -> None:
        with self.assertRaises(ValueError):
            _document(status=IngestionStatus.FAILED, units=())

    def test_a_failed_document_may_not_carry_units(self) -> None:
        with self.assertRaises(ValueError):
            _document(
                status=IngestionStatus.FAILED,
                failure_code=IngestionFailureCode.CORRUPTED_DOCUMENT,
            )

    def test_a_successful_document_may_not_carry_a_failure_code(self) -> None:
        with self.assertRaises(ValueError):
            _document(failure_code=IngestionFailureCode.CORRUPTED_DOCUMENT)

    def test_a_successful_document_requires_the_normalization_result(self) -> None:
        for field in NORMALIZED:
            with self.subTest(field=field), self.assertRaises(ValueError):
                _document(**{field: None})

    def test_a_successful_document_requires_at_least_one_unit(self) -> None:
        with self.assertRaises(ValueError):
            _document(units=())

    def test_rejects_duplicate_unit_locators(self) -> None:
        with self.assertRaises(ValueError):
            _document(units=(UNIT, UNIT))

    def test_a_failed_document_needs_no_parser(self) -> None:
        document = NormalizedPolicyDocument(
            **BASE,
            status=IngestionStatus.FAILED,
            failure_code=IngestionFailureCode.UNSUPPORTED_FORMAT,
        )

        self.assertFalse(document.is_approvable)
        self.assertIsNone(document.to_dict()["source_format"])


class UploadRequestContractTest(unittest.TestCase):
    def test_the_client_cannot_state_tenant_or_storage_identity(self) -> None:
        """업로드 요청은 `customer_id`, bucket, key, 처리 상태를 받지 않는다."""
        fields = set(
            PolicySourceUploadRequest(
                filename="policy.md", declared_media_type="text/markdown", byte_size=128
            ).to_dict()
        )

        self.assertEqual(fields, {"filename", "declared_media_type", "byte_size", "title"})
        for forbidden in ("customer_id", "bucket", "object_key", "status", "source_id"):
            self.assertNotIn(forbidden, fields)

    def test_rejects_a_non_positive_byte_size(self) -> None:
        with self.assertRaises(ValueError):
            PolicySourceUploadRequest(
                filename="policy.md", declared_media_type="text/markdown", byte_size=0
            )


if __name__ == "__main__":
    unittest.main()
