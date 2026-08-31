"""Tests for the deterministic M0 Policy Catalog adapter."""

import unittest
from pathlib import Path

from apps.backend.policy import (
    InMemoryPolicyCatalog,
    PolicyContextResolver,
    PolicyNotFoundError,
    load_m0_fixture_catalog,
)
from packages.contracts import AssessmentPhase, PolicyProfile, PolicyRuleReference

FIXTURE_PATH = Path(__file__).parents[2] / "fixtures" / "m0" / "policy_profile.json"


class PolicyCatalogTest(unittest.TestCase):
    def test_fixture_catalog_resolves_the_approved_rule_only(self) -> None:
        source, catalog = load_m0_fixture_catalog(FIXTURE_PATH)
        context = PolicyContextResolver(catalog).resolve(
            policy_profile_id="profile-mvp-baseline",
            phase=AssessmentPhase.INITIAL,
            resource_type="AWS::S3::Bucket",
        )

        self.assertEqual(source.source_id, "isms-p-2023")
        self.assertEqual([rule.rule_id for rule in context.rules], ["S3-PUBLIC-001"])

    def test_rejects_a_profile_that_references_a_missing_rule(self) -> None:
        profile = PolicyProfile(
            policy_profile_id="profile-001",
            version="v1",
            rule_references=(PolicyRuleReference(rule_id="missing", version="v1"),),
        )

        with self.assertRaisesRegex(PolicyNotFoundError, "unavailable rule"):
            InMemoryPolicyCatalog(profiles=(profile,), rules=())
