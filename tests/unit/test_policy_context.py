"""Policy Context must apply the Profile allow-list before evaluation."""

import unittest

from apps.backend.policy import PolicyContextResolver, PolicyNotFoundError
from packages.contracts import (
    AssessmentPhase,
    PolicyProfile,
    PolicyRule,
    PolicyRuleReference,
    RuleSeverity,
    SourceReference,
)


class Catalog:
    def __init__(self) -> None:
        self.profile = PolicyProfile(
            policy_profile_id="profile-001",
            version="v1",
            rule_references=(
                PolicyRuleReference(rule_id="S3-001", version="v1"),
                PolicyRuleReference(rule_id="EC2-001", version="v1"),
            ),
        )
        reference = SourceReference(
            source_id="isms-p",
            source_version="2023-10-31",
            locator="control/5.2.1",
            content_sha256="digest-001",
        )
        self.rules = {
            "S3-001": PolicyRule(
                rule_id="S3-001",
                version="v1",
                title="S3 block public access",
                severity=RuleSeverity.HIGH,
                applicable_phases=(AssessmentPhase.INITIAL,),
                resource_types=("AWS::S3::Bucket",),
                source_references=(reference,),
            ),
            "EC2-001": PolicyRule(
                rule_id="EC2-001",
                version="v1",
                title="EC2 instance profile",
                severity=RuleSeverity.MEDIUM,
                applicable_phases=(AssessmentPhase.INITIAL,),
                resource_types=("AWS::EC2::Instance",),
                source_references=(reference,),
            ),
        }

    def get_profile(self, policy_profile_id: str):
        return self.profile if policy_profile_id == self.profile.policy_profile_id else None

    def get_rule(self, rule_id: str, version: str):
        rule = self.rules.get(rule_id)
        return rule if rule is not None and rule.version == version else None


class PolicyContextResolverTest(unittest.TestCase):
    def test_returns_only_profile_allowed_rules_applicable_to_resource_and_phase(self) -> None:
        context = PolicyContextResolver(Catalog()).resolve(
            policy_profile_id="profile-001",
            phase=AssessmentPhase.INITIAL,
            resource_type="AWS::S3::Bucket",
        )

        self.assertEqual([rule.rule_id for rule in context.rules], ["S3-001"])
        self.assertEqual(context.source_references[0].locator, "control/5.2.1")

    def test_unknown_profile_or_empty_applicability_is_not_evaluable(self) -> None:
        resolver = PolicyContextResolver(Catalog())
        with self.assertRaises(PolicyNotFoundError):
            resolver.resolve(
                policy_profile_id="unknown",
                phase=AssessmentPhase.INITIAL,
                resource_type="AWS::S3::Bucket",
            )
        with self.assertRaises(PolicyNotFoundError):
            resolver.resolve(
                policy_profile_id="profile-001",
                phase=AssessmentPhase.POST_DEPLOY_VERIFICATION,
                resource_type="AWS::S3::Bucket",
            )
