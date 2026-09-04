"""A's approval/profile services call B's pure gate before persistence."""

import unittest

from apps.backend.api.policy_approval import PolicyApprovalApiService
from apps.backend.auth import AuthorizationDenied, Principal, Role
from apps.backend.policy.ingestion import ProfileBaseline
from packages.contracts import (
    AssessmentPhase,
    DocumentUnitKind,
    IngestionStatus,
    NormalizedDocumentUnit,
    NormalizedPolicyDocument,
    PolicyRule,
    PolicyRuleReference,
    PolicySource,
    PolicySourceFormat,
    PolicySourceKind,
    RuleCandidate,
    RuleSeverity,
    SourceReference,
)

DOCUMENT = NormalizedPolicyDocument(
    source_id="source-1",
    source_version="v1",
    artifact_id="original-1",
    s3_version_id="s3-v1",
    content_sha256="original-sha",
    filename="policy.md",
    declared_media_type="text/markdown",
    detected_media_type="text/markdown",
    source_format=PolicySourceFormat.MARKDOWN,
    byte_size=20,
    parser_id="markdown",
    parser_version="v1",
    normalized_artifact_id="normalized-1",
    normalized_sha256="normalized-sha",
    status=IngestionStatus.READY,
    units=(
        NormalizedDocumentUnit(
            locator="heading/access/item/1",
            kind=DocumentUnitKind.SECTION,
            text_sha256="unit-sha",
            text_length=10,
            origin="line/1",
        ),
    ),
)


def _rule(rule_id: str) -> PolicyRule:
    return PolicyRule(
        rule_id=rule_id,
        version="v1",
        title="Rule",
        severity=RuleSeverity.HIGH,
        applicable_phases=(AssessmentPhase.INITIAL,),
        resource_types=("AWS::S3::Bucket",),
        source_references=(
            SourceReference(
                source_id="source-1",
                source_version="v1",
                locator="heading/access/item/1",
                content_sha256="unit-sha",
            ),
        ),
    )


RULE = _rule("RULE-1")

#: 운영자가 게시한 기준선 Rule. 고객 승인 record가 없고, ISMS-P 원본만 인용한다.
BASELINE_RULE = PolicyRule(
    rule_id="ISMS-BASE-1",
    version="2026-08-31",
    title="Baseline rule",
    severity=RuleSeverity.HIGH,
    applicable_phases=(AssessmentPhase.INITIAL,),
    resource_types=("AWS::S3::Bucket",),
    source_references=(
        SourceReference(
            source_id="isms-p-2023",
            source_version="2023-10-31",
            locator="control/2.6.2",
            content_sha256="isms-unit-sha",
        ),
    ),
)
ISMS_SOURCE = PolicySource(
    source_id="isms-p-2023",
    kind=PolicySourceKind.ISMS_P,
    title="ISMS-P",
    version="2023-10-31",
    artifact_id="art-isms",
    content_sha256="isms-sha",
)


class Repository:
    def __init__(self) -> None:
        self.approval = None
        self.profile = None
        self.baseline_reads: list[tuple[str, str, str]] = []
        self.listed: str | None = None

    def load_review(self, **kwargs):
        # 두 후보를 돌려줘 부분 승인(하나만 고르기)을 검증할 수 있게 한다.
        return DOCUMENT, (RuleCandidate(rule=_rule("RULE-1")), RuleCandidate(rule=_rule("RULE-2")))

    def record_approval(self, **kwargs):
        self.approval = kwargs

    def load_publication(self, **kwargs):
        assert self.approval is not None
        return (
            self.approval["candidates"],
            (self.approval["approval"],),
            (
                PolicySource(
                    source_id="source-1",
                    kind=PolicySourceKind.INTERNAL_POLICY,
                    title="Policy",
                    version="v1",
                    artifact_id="original-1",
                    content_sha256="original-sha",
                ),
            ),
        )

    def record_profile(self, **kwargs):
        self.profile = kwargs

    def load_baseline(self, *, customer_id, policy_profile_id, version):
        self.baseline_reads.append((customer_id, policy_profile_id, version))
        return ProfileBaseline(
            policy_profile_id=policy_profile_id,
            version=version,
            rules=(BASELINE_RULE,),
            sources=(ISMS_SOURCE,),
        )

    def list_profiles(self, *, customer_id):
        self.listed = customer_id
        return (
            {
                "policy_profile_id": "profile-multiresource-baseline",
                "version": "v1",
                "rule_count": 1,
                "source_kinds": ["ISMS_P"],
                "published_at": "2026-09-01T00:00:00Z",
            },
        )


class PolicyApprovalApiServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = Repository()
        self.service = PolicyApprovalApiService(self.repository)
        self.admin = Principal(
            subject="admin", client_id="client", customer_id="cust-a", roles=frozenset({Role.ADMIN})
        )

    def test_approves_then_publishes_only_after_the_pure_gates(self) -> None:
        approval = self.service.approve(
            self.admin,
            source_id="source-1",
            source_version="v1",
            approved_rules=(PolicyRuleReference(rule_id="RULE-1", version="v1"),),
        )
        profile = self.service.publish(
            self.admin,
            sources=(("source-1", "v1"),),
            policy_profile_id="profile-1",
            version="v1",
        )

        self.assertEqual(approval.approved_rules[0].rule_id, "RULE-1")
        self.assertEqual(profile.rule_references[0].rule_id, "RULE-1")
        self.assertIsNotNone(self.repository.profile)

    def test_approves_only_the_selected_subset_of_candidates(self) -> None:
        # load_review는 RULE-1/RULE-2 두 후보를 주지만 리뷰어는 RULE-1만 승인한다.
        approval = self.service.approve(
            self.admin,
            source_id="source-1",
            source_version="v1",
            approved_rules=(PolicyRuleReference(rule_id="RULE-1", version="v1"),),
        )
        approved_ids = {reference.rule_id for reference in approval.approved_rules}
        self.assertEqual(approved_ids, {"RULE-1"})
        # 승인 record에 저장되는 후보도 고른 것만이다.
        recorded = {candidate.rule.rule_id for candidate in self.repository.approval["candidates"]}
        self.assertEqual(recorded, {"RULE-1"})

    def test_approving_a_rule_absent_from_candidates_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.service.approve(
                self.admin,
                source_id="source-1",
                source_version="v1",
                approved_rules=(PolicyRuleReference(rule_id="RULE-404", version="v1"),),
            )

    def test_empty_approval_selection_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.service.approve(
                self.admin, source_id="source-1", source_version="v1", approved_rules=()
            )

    def test_user_cannot_approve_or_publish(self) -> None:
        user = Principal(
            subject="user", client_id="client", customer_id="cust-a", roles=frozenset({Role.USER})
        )
        with self.assertRaises(AuthorizationDenied):
            self.service.approve(
                user,
                source_id="source-1",
                source_version="v1",
                approved_rules=(PolicyRuleReference(rule_id="RULE-1", version="v1"),),
            )
        with self.assertRaises(AuthorizationDenied):
            self.service.publish(
                user,
                sources=(("source-1", "v1"),),
                policy_profile_id="profile-1",
                version="v1",
            )


class CombinedPublicationTest(unittest.TestCase):
    """게시 경계가 사내 문서와 ISMS-P 기준선을 함께 받는다."""

    def setUp(self) -> None:
        self.repository = Repository()
        self.service = PolicyApprovalApiService(self.repository)
        self.admin = Principal(
            subject="admin", client_id="client", customer_id="cust-a", roles=frozenset({Role.ADMIN})
        )
        self.service.approve(
            self.admin,
            source_id="source-1",
            source_version="v1",
            approved_rules=(PolicyRuleReference(rule_id="RULE-1", version="v1"),),
        )

    def test_a_baseline_is_added_to_the_approved_rules(self) -> None:
        profile = self.service.publish(
            self.admin,
            sources=(("source-1", "v1"),),
            baseline=("profile-multiresource-baseline", "v1"),
            policy_profile_id="profile-combined",
            version="v1",
        )

        self.assertEqual(
            [reference.rule_id for reference in profile.rule_references],
            ["RULE-1", "ISMS-BASE-1"],
        )
        self.assertEqual(
            self.repository.baseline_reads,
            [("cust-a", "profile-multiresource-baseline", "v1")],
        )

    def test_the_published_profile_keeps_the_two_origins_apart(self) -> None:
        """합쳐도 원본 구분은 남는다. 남지 않으면 보고 단계가 준비도를 나눌 근거를 잃는다."""
        profile = self.service.publish(
            self.admin,
            sources=(("source-1", "v1"),),
            baseline=("profile-multiresource-baseline", "v1"),
            policy_profile_id="profile-combined",
            version="v1",
        )

        self.assertEqual(
            profile.rule_kinds(),
            {
                "RULE-1": (PolicySourceKind.INTERNAL_POLICY,),
                "ISMS-BASE-1": (PolicySourceKind.ISMS_P,),
            },
        )

    def test_a_baseline_alone_publishes_without_any_uploaded_document(self) -> None:
        """ISMS-P만으로 평가하려는 고객은 문서를 올리지 않았을 수 있다."""
        profile = self.service.publish(
            self.admin,
            sources=(),
            baseline=("profile-multiresource-baseline", "v1"),
            policy_profile_id="profile-isms-only",
            version="v1",
        )

        self.assertEqual([r.rule_id for r in profile.rule_references], ["ISMS-BASE-1"])

    def test_selecting_the_same_document_twice_is_refused_as_such(self) -> None:
        """중복 선택은 중복 선택으로 거부한다 — binding 불일치로 새어 나오면 원인을 못 읽는다."""
        with self.assertRaises(ValueError) as raised:
            self.service.publish(
                self.admin,
                sources=(("source-1", "v1"), ("source-1", "v1")),
                policy_profile_id="profile-combined",
                version="v1",
            )
        self.assertIn("more than once", str(raised.exception))

    def test_publishing_nothing_at_all_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.service.publish(
                self.admin, sources=(), policy_profile_id="profile-empty", version="v1"
            )

    def test_listing_profiles_needs_the_publish_action(self) -> None:
        user = Principal(
            subject="user", client_id="client", customer_id="cust-a", roles=frozenset({Role.USER})
        )
        with self.assertRaises(AuthorizationDenied):
            self.service.list_profiles(user)
        self.assertEqual(len(self.service.list_profiles(self.admin)), 1)
        self.assertEqual(self.repository.listed, "cust-a")
