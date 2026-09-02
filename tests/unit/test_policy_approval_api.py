"""A's approval/profile services call B's pure gate before persistence."""

import unittest

from apps.backend.api.policy_approval import PolicyApprovalApiService
from apps.backend.auth import AuthorizationDenied, Principal, Role
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


class Repository:
    def __init__(self) -> None:
        self.approval = None
        self.profile = None

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
            source_id="source-1",
            source_version="v1",
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
                source_id="source-1",
                source_version="v1",
                policy_profile_id="profile-1",
                version="v1",
            )
