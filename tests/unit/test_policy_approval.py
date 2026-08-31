"""Tests for Rule candidate approval and Policy Profile publication."""

import hashlib
import unittest

from apps.backend.policy import InMemoryPolicyCatalog, PolicyContextResolver, PolicyNotFoundError
from apps.backend.policy.ingestion import (
    ApprovalRejectedError,
    UploadedPolicyOriginal,
    approve_source,
    normalize_upload,
    publish_profile,
    source_reference_for,
)
from packages.contracts import (
    ApprovalRejectionCode,
    AssessmentPhase,
    PolicyRule,
    PolicyRuleReference,
    PolicySource,
    PolicySourceKind,
    RuleCandidate,
    RuleLifecycle,
    RuleSeverity,
    SourceReference,
)

MARKDOWN = b"""# Access Control

Public buckets are prohibited.

Buckets encrypt objects at rest.
"""

SOURCE_ID = "internal-cloud-security-checklist"
SOURCE_VERSION = "2026-09-01"
PUBLIC_LOCATOR = "heading/access-control/item/1"
ENCRYPT_LOCATOR = "heading/access-control/item/2"
S3 = "AWS::S3::Bucket"


def _original(payload: bytes = MARKDOWN, **overrides: object) -> UploadedPolicyOriginal:
    fields: dict[str, object] = {
        "source_id": SOURCE_ID,
        "source_version": SOURCE_VERSION,
        "artifact_id": "artifact-001",
        "s3_version_id": "s3-version-001",
        "content_sha256": hashlib.sha256(payload).hexdigest(),
        "filename": "policy.md",
        "declared_media_type": "text/markdown",
        "byte_size": len(payload),
    }
    fields.update(overrides)
    return UploadedPolicyOriginal(**fields)  # type: ignore[arg-type]


def _document(payload: bytes = MARKDOWN, **overrides: object):
    return normalize_upload(_original(payload, **overrides), payload).document


def _rule(document, locator: str, *, rule_id: str, version: str = "2026-09-01") -> PolicyRule:
    return PolicyRule(
        rule_id=rule_id,
        version=version,
        title="Rule under review",
        severity=RuleSeverity.HIGH,
        applicable_phases=(AssessmentPhase.INITIAL,),
        resource_types=(S3,),
        source_references=(source_reference_for(document, locator),),
    )


def _candidate(document, locator: str, *, rule_id: str) -> RuleCandidate:
    return RuleCandidate(rule=_rule(document, locator, rule_id=rule_id))


class ApproveSourceTest(unittest.TestCase):
    def test_approval_cites_the_finalized_original(self) -> None:
        document = _document()

        approval, approved = approve_source(
            document,
            [_candidate(document, PUBLIC_LOCATOR, rule_id="S3-PUBLIC-100")],
            approved_by="policy-owner",
            approved_at="2026-09-01T00:00:00Z",
        )

        self.assertEqual(
            approval.original_binding,
            (document.artifact_id, document.s3_version_id, document.content_sha256),
        )
        self.assertEqual(approval.source_version, SOURCE_VERSION)
        self.assertEqual(approval.normalized_sha256, document.normalized_sha256)
        self.assertTrue(all(candidate.is_approved for candidate in approved))

    def test_the_candidates_passed_in_are_not_mutated(self) -> None:
        """후보는 불변이다. 승인은 새 값을 만들지 원본을 바꾸지 않는다."""
        document = _document()
        candidate = _candidate(document, PUBLIC_LOCATOR, rule_id="S3-PUBLIC-100")

        approve_source(
            document, [candidate], approved_by="policy-owner", approved_at="2026-09-01T00:00:00Z"
        )
        self.assertEqual(candidate.lifecycle, RuleLifecycle.CANDIDATE)

    def test_refuses_a_document_awaiting_review(self) -> None:
        payload = b"Public buckets are prohibited.\n"
        document = _document(payload, declared_media_type="text/plain")

        with self.assertRaises(ApprovalRejectedError) as raised:
            approve_source(
                document,
                [_candidate(_document(), PUBLIC_LOCATOR, rule_id="S3-PUBLIC-100")],
                approved_by="policy-owner",
                approved_at="2026-09-01T00:00:00Z",
            )
        self.assertEqual(
            raised.exception.rejection_code, ApprovalRejectionCode.SOURCE_NOT_APPROVABLE
        )

    def test_refuses_a_rule_citing_another_source_version(self) -> None:
        """승인을 다른 판본으로 옮겨 붙일 수 없다."""
        document = _document()
        other = _document(source_version="2026-08-01")
        candidate = _candidate(other, PUBLIC_LOCATOR, rule_id="S3-PUBLIC-100")

        with self.assertRaises(ApprovalRejectedError) as raised:
            approve_source(
                document,
                [candidate],
                approved_by="policy-owner",
                approved_at="2026-09-01T00:00:00Z",
            )
        self.assertEqual(
            raised.exception.rejection_code, ApprovalRejectionCode.SOURCE_VERSION_MISMATCH
        )

    def test_refuses_a_locator_the_document_does_not_contain(self) -> None:
        document = _document()
        rule = PolicyRule(
            rule_id="S3-PUBLIC-100",
            version="2026-09-01",
            title="Rule under review",
            severity=RuleSeverity.HIGH,
            applicable_phases=(AssessmentPhase.INITIAL,),
            resource_types=(S3,),
            source_references=(
                SourceReference(
                    source_id=SOURCE_ID,
                    source_version=SOURCE_VERSION,
                    locator="heading/does-not-exist",
                    content_sha256="a" * 64,
                ),
            ),
        )

        with self.assertRaises(ApprovalRejectedError) as raised:
            approve_source(
                document,
                [RuleCandidate(rule=rule)],
                approved_by="policy-owner",
                approved_at="2026-09-01T00:00:00Z",
            )
        self.assertEqual(raised.exception.rejection_code, ApprovalRejectionCode.UNKNOWN_LOCATOR)

    def test_refuses_a_rule_whose_digest_does_not_match_the_unit(self) -> None:
        """사람이 검토한 문장과 Rule이 고정한 hash가 다르면 승인이 다른 내용에 붙는다."""
        document = _document()
        reference = source_reference_for(document, PUBLIC_LOCATOR)
        rule = PolicyRule(
            rule_id="S3-PUBLIC-100",
            version="2026-09-01",
            title="Rule under review",
            severity=RuleSeverity.HIGH,
            applicable_phases=(AssessmentPhase.INITIAL,),
            resource_types=(S3,),
            source_references=(
                SourceReference(
                    source_id=reference.source_id,
                    source_version=reference.source_version,
                    locator=reference.locator,
                    content_sha256="f" * 64,
                ),
            ),
        )

        with self.assertRaises(ApprovalRejectedError) as raised:
            approve_source(
                document,
                [RuleCandidate(rule=rule)],
                approved_by="policy-owner",
                approved_at="2026-09-01T00:00:00Z",
            )
        self.assertEqual(
            raised.exception.rejection_code, ApprovalRejectionCode.CONTENT_DIGEST_MISMATCH
        )

    def test_refuses_an_empty_approval(self) -> None:
        with self.assertRaises(ApprovalRejectedError) as raised:
            approve_source(
                _document(), [], approved_by="policy-owner", approved_at="2026-09-01T00:00:00Z"
            )
        self.assertEqual(raised.exception.rejection_code, ApprovalRejectionCode.EMPTY_PROFILE)


class PublishProfileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.document = _document()
        self.approval, self.approved = approve_source(
            self.document,
            [
                _candidate(self.document, PUBLIC_LOCATOR, rule_id="S3-PUBLIC-100"),
                _candidate(self.document, ENCRYPT_LOCATOR, rule_id="S3-ENCRYPT-100"),
            ],
            approved_by="policy-owner",
            approved_at="2026-09-01T00:00:00Z",
        )

    def test_publishes_a_versioned_allow_list_of_approved_rules(self) -> None:
        profile = publish_profile(
            policy_profile_id="profile-customer-baseline",
            version="1",
            candidates=self.approved,
            approvals=[self.approval],
        )

        self.assertEqual(
            [(reference.rule_id, reference.version) for reference in profile.rule_references],
            [("S3-PUBLIC-100", "2026-09-01"), ("S3-ENCRYPT-100", "2026-09-01")],
        )

    def test_refuses_a_candidate_that_was_never_approved(self) -> None:
        """거부 조건 1 — 승인되지 않은 Rule을 참조하는 Profile."""
        with self.assertRaises(ApprovalRejectedError) as raised:
            publish_profile(
                policy_profile_id="profile-customer-baseline",
                version="1",
                candidates=[*self.approved, _candidate(self.document, PUBLIC_LOCATOR, rule_id="X")],
                approvals=[self.approval],
            )
        self.assertEqual(raised.exception.rejection_code, ApprovalRejectionCode.RULE_NOT_APPROVED)

    def test_refuses_a_rule_from_an_unapproved_source(self) -> None:
        """거부 조건 1 — 승인되지 않은 Source를 참조하는 Profile."""
        other = _document(source_id="unapproved-source")
        stranger = _candidate(other, PUBLIC_LOCATOR, rule_id="S3-PUBLIC-200").approved()

        with self.assertRaises(ApprovalRejectedError) as raised:
            publish_profile(
                policy_profile_id="profile-customer-baseline",
                version="1",
                candidates=[stranger],
                approvals=[self.approval],
            )
        self.assertEqual(raised.exception.rejection_code, ApprovalRejectionCode.SOURCE_NOT_APPROVED)

    def test_refuses_a_reference_to_a_different_version_of_an_approved_source(self) -> None:
        """거부 조건 2 — 승인된 것과 다른 Source version을 가리키는 SourceReference."""
        revised = _document(source_version="2026-10-01")
        stranger = _candidate(revised, PUBLIC_LOCATOR, rule_id="S3-PUBLIC-300").approved()

        with self.assertRaises(ApprovalRejectedError) as raised:
            publish_profile(
                policy_profile_id="profile-customer-baseline",
                version="1",
                candidates=[stranger],
                approvals=[self.approval],
            )
        self.assertEqual(
            raised.exception.rejection_code, ApprovalRejectionCode.SOURCE_VERSION_MISMATCH
        )

    def test_refuses_a_source_whose_artifact_binding_differs_from_the_approval(self) -> None:
        """거부 조건 3 — 승인 record의 (artifact_id, s3_version_id, content_sha256)과 어긋난다."""
        relabelled = PolicySource(
            source_id=SOURCE_ID,
            kind=PolicySourceKind.INTERNAL_POLICY,
            title="Cloud security checklist",
            version=SOURCE_VERSION,
            artifact_id="artifact-999",
            content_sha256=self.approval.content_sha256,
        )

        with self.assertRaises(ApprovalRejectedError) as raised:
            publish_profile(
                policy_profile_id="profile-customer-baseline",
                version="1",
                candidates=self.approved,
                approvals=[self.approval],
                sources=[relabelled],
            )
        self.assertEqual(
            raised.exception.rejection_code, ApprovalRejectionCode.ORIGINAL_BINDING_MISMATCH
        )

    def test_accepts_a_source_whose_binding_matches_the_approval(self) -> None:
        matching = PolicySource(
            source_id=SOURCE_ID,
            kind=PolicySourceKind.INTERNAL_POLICY,
            title="Cloud security checklist",
            version=SOURCE_VERSION,
            artifact_id=self.approval.artifact_id,
            content_sha256=self.approval.content_sha256,
        )

        profile = publish_profile(
            policy_profile_id="profile-customer-baseline",
            version="1",
            candidates=self.approved,
            approvals=[self.approval],
            sources=[matching],
        )
        self.assertEqual(len(profile.rule_references), 2)

    def test_refuses_a_duplicate_rule_reference(self) -> None:
        with self.assertRaises(ApprovalRejectedError) as raised:
            publish_profile(
                policy_profile_id="profile-customer-baseline",
                version="1",
                candidates=[*self.approved, self.approved[0]],
                approvals=[self.approval],
            )
        self.assertEqual(
            raised.exception.rejection_code, ApprovalRejectionCode.DUPLICATE_RULE_REFERENCE
        )

    def test_refuses_an_empty_profile(self) -> None:
        with self.assertRaises(ApprovalRejectedError) as raised:
            publish_profile(
                policy_profile_id="profile-customer-baseline",
                version="1",
                candidates=[],
                approvals=[self.approval],
            )
        self.assertEqual(raised.exception.rejection_code, ApprovalRejectionCode.EMPTY_PROFILE)

    def test_refuses_two_approval_records_for_one_source_version(self) -> None:
        with self.assertRaises(ApprovalRejectedError) as raised:
            publish_profile(
                policy_profile_id="profile-customer-baseline",
                version="1",
                candidates=self.approved,
                approvals=[self.approval, self.approval],
            )
        self.assertEqual(
            raised.exception.rejection_code, ApprovalRejectionCode.ORIGINAL_BINDING_MISMATCH
        )


class PublishedProfileReachesAssessmentTest(unittest.TestCase):
    """게시된 Profile은 기존 Policy Context 경로에서 그대로 동작해야 한다."""

    def setUp(self) -> None:
        self.document = _document()
        self.approval, self.approved = approve_source(
            self.document,
            [_candidate(self.document, PUBLIC_LOCATOR, rule_id="S3-PUBLIC-100")],
            approved_by="policy-owner",
            approved_at="2026-09-01T00:00:00Z",
        )
        self.profile = publish_profile(
            policy_profile_id="profile-customer-baseline",
            version="1",
            candidates=self.approved,
            approvals=[self.approval],
        )

    def test_the_published_profile_resolves_a_policy_context(self) -> None:
        catalog = InMemoryPolicyCatalog(
            profiles=(self.profile,), rules=tuple(c.rule for c in self.approved)
        )

        context = PolicyContextResolver(catalog).resolve(
            policy_profile_id="profile-customer-baseline",
            phase=AssessmentPhase.INITIAL,
            resource_type=S3,
        )
        self.assertEqual([rule.rule_id for rule in context.rules], ["S3-PUBLIC-100"])
        self.assertTrue(context.allows_evidence(f"{SOURCE_ID}@{SOURCE_VERSION}#{PUBLIC_LOCATOR}"))

    def test_an_unapproved_rule_never_reaches_a_policy_context(self) -> None:
        """`docs/POLICY_INGESTION.md`: 사람 승인 전 Rule은 Profile 및 Assessment Context에
        들어가지 않는다. Profile allow-list가 유일한 진입 경로이므로, Catalog에 Rule이 있어도
        Profile이 참조하지 않으면 Context에 들어올 수 없다."""
        candidate = _candidate(self.document, ENCRYPT_LOCATOR, rule_id="S3-ENCRYPT-999")
        catalog = InMemoryPolicyCatalog(
            profiles=(self.profile,),
            rules=(*(c.rule for c in self.approved), candidate.rule),
        )

        context = PolicyContextResolver(catalog).resolve(
            policy_profile_id="profile-customer-baseline",
            phase=AssessmentPhase.INITIAL,
            resource_type=S3,
        )
        self.assertNotIn("S3-ENCRYPT-999", [rule.rule_id for rule in context.rules])
        self.assertFalse(context.allows_evidence(f"{SOURCE_ID}@{SOURCE_VERSION}#{ENCRYPT_LOCATOR}"))

    def test_a_profile_pinning_a_missing_rule_is_refused_by_the_catalog(self) -> None:
        """게시 뒤 Rule이 사라지면 다른 allow-list로 평가하지 않고 실패한다."""
        with self.assertRaises(PolicyNotFoundError):
            InMemoryPolicyCatalog(profiles=(self.profile,), rules=())

    def test_the_profile_only_lists_rules_the_approval_covers(self) -> None:
        covered = {
            (reference.rule_id, reference.version) for reference in self.approval.approved_rules
        }
        for reference in self.profile.rule_references:
            self.assertIn((reference.rule_id, reference.version), covered)
        self.assertTrue(
            self.approval.approves(
                PolicyRuleReference(rule_id="S3-PUBLIC-100", version="2026-09-01")
            )
        )
        self.assertFalse(
            self.approval.approves(PolicyRuleReference(rule_id="S3-PUBLIC-100", version="9999"))
        )


if __name__ == "__main__":
    unittest.main()
