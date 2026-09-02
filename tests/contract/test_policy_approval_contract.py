"""Contract tests for the policy approval and publication boundary."""

import json
import unittest

from packages.contracts import (
    ApprovalRejectionCode,
    AssessmentPhase,
    DocumentUnitKind,
    IngestionStatus,
    NormalizedDocumentUnit,
    NormalizedPolicyDocument,
    PolicyCandidateExtraction,
    PolicyRule,
    PolicyRuleReference,
    PolicySourceApproval,
    PolicySourceFormat,
    RuleCandidate,
    RuleLifecycle,
    RuleSeverity,
    SourceReference,
)

REFERENCE = SourceReference(
    source_id="internal-cloud-security-checklist",
    source_version="2026-09-01",
    locator="heading/access-control/item/1",
    content_sha256="a" * 64,
)

RULE = PolicyRule(
    rule_id="S3-PUBLIC-100",
    version="2026-09-01",
    title="Public buckets are prohibited",
    severity=RuleSeverity.HIGH,
    applicable_phases=(AssessmentPhase.INITIAL,),
    resource_types=("AWS::S3::Bucket",),
    source_references=(REFERENCE,),
)

DOCUMENT = NormalizedPolicyDocument(
    source_id=REFERENCE.source_id,
    source_version=REFERENCE.source_version,
    artifact_id="artifact-001",
    s3_version_id="s3-version-001",
    content_sha256="b" * 64,
    filename="policy.md",
    declared_media_type="text/markdown",
    byte_size=128,
    status=IngestionStatus.READY,
    detected_media_type="text/markdown",
    source_format=PolicySourceFormat.MARKDOWN,
    parser_id="markdown-parser",
    parser_version="1.0.0",
    normalized_artifact_id="artifact-001#normalized",
    normalized_sha256="c" * 64,
    units=(
        NormalizedDocumentUnit(
            locator=REFERENCE.locator,
            kind=DocumentUnitKind.LIST_ITEM,
            text_sha256=REFERENCE.content_sha256,
            text_length=34,
            origin="line/3-3",
        ),
    ),
)

APPROVAL_FIELDS = {
    "source_id": "internal-cloud-security-checklist",
    "source_version": "2026-09-01",
    "artifact_id": "artifact-001",
    "s3_version_id": "s3-version-001",
    "content_sha256": "b" * 64,
    "normalized_artifact_id": "artifact-001#normalized",
    "normalized_sha256": "c" * 64,
    "approved_rules": (PolicyRuleReference(rule_id="S3-PUBLIC-100", version="2026-09-01"),),
    "approved_by": "policy-owner",
    "approved_at": "2026-09-01T00:00:00Z",
}


def _approval(**overrides: object) -> PolicySourceApproval:
    fields: dict[str, object] = {**APPROVAL_FIELDS}
    fields.update(overrides)
    return PolicySourceApproval(**fields)  # type: ignore[arg-type]


class RuleCandidateContractTest(unittest.TestCase):
    def test_a_candidate_starts_unapproved(self) -> None:
        candidate = RuleCandidate(rule=RULE)

        self.assertEqual(candidate.lifecycle, RuleLifecycle.CANDIDATE)
        self.assertFalse(candidate.is_approved)

    def test_approving_produces_a_new_value(self) -> None:
        """Contract는 frozen이다. 승인이 후보를 제자리에서 바꾸지 않는다."""
        candidate = RuleCandidate(rule=RULE)

        approved = candidate.approved()
        self.assertTrue(approved.is_approved)
        self.assertFalse(candidate.is_approved)
        self.assertIsNot(approved, candidate)

    def test_the_reference_pins_the_exact_rule_version(self) -> None:
        reference = RuleCandidate(rule=RULE).reference

        self.assertEqual((reference.rule_id, reference.version), (RULE.rule_id, RULE.version))

    def test_serializes_the_rule_and_lifecycle(self) -> None:
        payload = RuleCandidate(rule=RULE).rejected().to_dict()

        self.assertEqual(payload["lifecycle"], "REJECTED")
        json.dumps(payload)


class PolicyCandidateExtractionContractTest(unittest.TestCase):
    def test_handoff_pins_ready_document_candidate_provenance_without_text(self) -> None:
        result = PolicyCandidateExtraction(
            document=DOCUMENT,
            candidates=(RuleCandidate(rule=RULE),),
            extractor_id="policy-candidate-extractor",
            extractor_version="v1",
        )

        payload = result.to_dict()

        self.assertEqual(payload["document"]["source_id"], REFERENCE.source_id)
        self.assertEqual(payload["candidates"][0]["lifecycle"], "CANDIDATE")
        self.assertNotIn("normalized_payload", payload)
        json.dumps(payload)

    def test_requires_ready_document_and_undecided_unique_candidates(self) -> None:
        review_required = NormalizedPolicyDocument(
            source_id=DOCUMENT.source_id,
            source_version=DOCUMENT.source_version,
            artifact_id=DOCUMENT.artifact_id,
            s3_version_id=DOCUMENT.s3_version_id,
            content_sha256=DOCUMENT.content_sha256,
            filename=DOCUMENT.filename,
            declared_media_type=DOCUMENT.declared_media_type,
            byte_size=DOCUMENT.byte_size,
            status=IngestionStatus.REVIEW_REQUIRED,
            detected_media_type=DOCUMENT.detected_media_type,
            source_format=DOCUMENT.source_format,
            parser_id=DOCUMENT.parser_id,
            parser_version=DOCUMENT.parser_version,
            normalized_artifact_id=DOCUMENT.normalized_artifact_id,
            normalized_sha256=DOCUMENT.normalized_sha256,
            units=DOCUMENT.units,
        )
        with self.assertRaisesRegex(ValueError, "READY"):
            PolicyCandidateExtraction(
                document=review_required,
                candidates=(),
                extractor_id="policy-candidate-extractor",
                extractor_version="v1",
            )
        with self.assertRaisesRegex(ValueError, "undecided"):
            PolicyCandidateExtraction(
                document=DOCUMENT,
                candidates=(RuleCandidate(rule=RULE).approved(),),
                extractor_id="policy-candidate-extractor",
                extractor_version="v1",
            )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            PolicyCandidateExtraction(
                document=DOCUMENT,
                candidates=(RuleCandidate(rule=RULE), RuleCandidate(rule=RULE)),
                extractor_id="policy-candidate-extractor",
                extractor_version="v1",
            )

    def test_rejects_candidate_provenance_outside_the_exact_document(self) -> None:
        outside_document_rule = PolicyRule(
            rule_id="S3-OUTSIDE-100",
            version="2026-09-01",
            title="Outside source reference",
            severity=RuleSeverity.HIGH,
            applicable_phases=(AssessmentPhase.INITIAL,),
            resource_types=("AWS::S3::Bucket",),
            source_references=(
                SourceReference(
                    source_id="different-source",
                    source_version=REFERENCE.source_version,
                    locator=REFERENCE.locator,
                    content_sha256=REFERENCE.content_sha256,
                ),
            ),
        )
        wrong_digest_rule = PolicyRule(
            rule_id="S3-WRONG-DIGEST-100",
            version="2026-09-01",
            title="Wrong locator digest",
            severity=RuleSeverity.HIGH,
            applicable_phases=(AssessmentPhase.INITIAL,),
            resource_types=("AWS::S3::Bucket",),
            source_references=(
                SourceReference(
                    source_id=REFERENCE.source_id,
                    source_version=REFERENCE.source_version,
                    locator=REFERENCE.locator,
                    content_sha256="z" * 64,
                ),
            ),
        )
        for rule, message in (
            (outside_document_rule, "match"),
            (wrong_digest_rule, "digest"),
        ):
            with self.subTest(rule=rule.rule_id), self.assertRaisesRegex(ValueError, message):
                PolicyCandidateExtraction(
                    document=DOCUMENT,
                    candidates=(RuleCandidate(rule=rule),),
                    extractor_id="policy-candidate-extractor",
                    extractor_version="v1",
                )


class PolicySourceApprovalContractTest(unittest.TestCase):
    def test_exposes_the_original_binding_publication_checks(self) -> None:
        approval = _approval()

        self.assertEqual(
            approval.original_binding,
            (approval.artifact_id, approval.s3_version_id, approval.content_sha256),
        )

    def test_approves_only_the_exact_rule_version(self) -> None:
        approval = _approval()

        self.assertTrue(
            approval.approves(PolicyRuleReference(rule_id="S3-PUBLIC-100", version="2026-09-01"))
        )
        self.assertFalse(
            approval.approves(PolicyRuleReference(rule_id="S3-PUBLIC-100", version="2026-08-01"))
        )
        self.assertFalse(
            approval.approves(PolicyRuleReference(rule_id="S3-OTHER-100", version="2026-09-01"))
        )

    def test_requires_at_least_one_approved_rule(self) -> None:
        with self.assertRaises(ValueError):
            _approval(approved_rules=())

    def test_rejects_a_duplicate_approved_rule(self) -> None:
        reference = PolicyRuleReference(rule_id="S3-PUBLIC-100", version="2026-09-01")

        with self.assertRaises(ValueError):
            _approval(approved_rules=(reference, reference))

    def test_requires_the_full_finalization_tuple(self) -> None:
        for field in ("artifact_id", "s3_version_id", "content_sha256"):
            with self.subTest(field=field), self.assertRaises((TypeError, ValueError)):
                _approval(**{field: ""})

    def test_serializes_every_field_for_the_audit_record(self) -> None:
        payload = _approval().to_dict()

        self.assertEqual(set(payload), set(APPROVAL_FIELDS))
        json.dumps(payload)

    def test_the_serialized_approval_carries_no_policy_text(self) -> None:
        """승인 record는 식별자와 hash만 담는다. 원문 문장은 정규화 Artifact에만 있다."""
        payload = _approval().to_dict()

        self.assertNotIn("text", payload)
        for value in payload.values():
            self.assertNotIsInstance(value, bytes)


class RejectionCodeContractTest(unittest.TestCase):
    def test_the_three_documented_publication_refusals_have_codes(self) -> None:
        """`docs/POLICY_INGESTION.md`가 명시한 거부 조건 3건에 각각 코드가 있어야 한다."""
        for code in (
            ApprovalRejectionCode.SOURCE_NOT_APPROVED,
            ApprovalRejectionCode.RULE_NOT_APPROVED,
            ApprovalRejectionCode.SOURCE_VERSION_MISMATCH,
            ApprovalRejectionCode.ORIGINAL_BINDING_MISMATCH,
        ):
            self.assertIsInstance(code.value, str)

    def test_codes_are_stable_strings(self) -> None:
        self.assertEqual(ApprovalRejectionCode.RULE_NOT_APPROVED.value, "RULE_NOT_APPROVED")
        self.assertEqual(ApprovalRejectionCode.RULE_NOT_APPROVABLE.value, "RULE_NOT_APPROVABLE")
        self.assertEqual(RuleLifecycle.APPROVED.value, "APPROVED")

    def test_every_refusal_the_approval_path_can_raise_has_a_code(self) -> None:
        """승인 경로의 거부도 게시와 같은 열거값으로 표현된다.

        A가 `rejection_code`를 API 오류 코드로 옮기므로, 한 경로만 코드 없는 예외를 던지면
        그 자리가 오류 코드 없는 실패로 새어 나간다.
        """
        for code in (
            ApprovalRejectionCode.SOURCE_NOT_APPROVABLE,
            ApprovalRejectionCode.RULE_NOT_APPROVABLE,
            ApprovalRejectionCode.UNKNOWN_LOCATOR,
            ApprovalRejectionCode.CONTENT_DIGEST_MISMATCH,
            ApprovalRejectionCode.DUPLICATE_RULE_REFERENCE,
            ApprovalRejectionCode.EMPTY_PROFILE,
        ):
            self.assertIsInstance(code.value, str)


if __name__ == "__main__":
    unittest.main()
