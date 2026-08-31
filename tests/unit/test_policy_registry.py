"""Tests for the committed MVP Rule Registry and Control/Resource mapping."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from apps.backend.policy import (
    PolicyContextResolver,
    PolicyNotFoundError,
    PolicyRegistryError,
    load_rule_registry,
)
from packages.contracts import AssessmentPhase

REGISTRY_PATH = Path(__file__).parents[2] / "fixtures" / "rules"
PROFILE_ID = "profile-mvp-baseline"
S3 = "AWS::S3::Bucket"
EC2 = "AWS::EC2::Instance"


def _write_registry(directory: Path, **overrides: object) -> Path:
    """Copy the committed registry into a temp directory, replacing named files."""
    for path in REGISTRY_PATH.iterdir():
        if path.suffix == ".json":
            (directory / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    for name, content in overrides.items():
        (directory / name.replace("__", ".")).write_text(
            json.dumps(content, ensure_ascii=False), encoding="utf-8"
        )
    return directory


class RuleRegistryLoadTest(unittest.TestCase):
    def test_loads_every_committed_rule_file(self) -> None:
        registry = load_rule_registry(REGISTRY_PATH)

        rule_ids = {rule.rule_id for rule in registry.rules}
        self.assertIn("S3-PUBLIC-001", rule_ids)
        self.assertIn("EC2-EBS-ENCRYPT-001", rule_ids)
        self.assertEqual(
            {source.source_id for source in registry.sources},
            {"internal-cloud-security-checklist", "isms-p-2023"},
        )
        self.assertIsNotNone(registry.get_source("isms-p-2023", "2023-10-31"))
        self.assertIsNone(registry.get_source("isms-p-2023", "1999-01-01"))
        self.assertIsNone(registry.get_source("unknown-source", "2023-10-31"))

    def test_every_rule_carries_a_traceable_source_reference(self) -> None:
        registry = load_rule_registry(REGISTRY_PATH)

        declared = {source.source_id for source in registry.sources}
        for rule in registry.rules:
            self.assertTrue(rule.source_references, rule.rule_id)
            for reference in rule.source_references:
                self.assertIn(reference.source_id, declared)
                self.assertEqual(len(reference.content_sha256), 64, reference.locator)

    def test_every_reference_pins_the_declared_source_version(self) -> None:
        registry = load_rule_registry(REGISTRY_PATH)

        declared = {source.source_id: source.version for source in registry.sources}
        for rule in registry.rules:
            for reference in rule.source_references:
                self.assertEqual(reference.source_version, declared[reference.source_id])
                self.assertEqual(
                    reference.evidence_reference,
                    f"{reference.source_id}@{reference.source_version}#{reference.locator}",
                )

    def test_rejects_a_reference_to_an_undeclared_source(self) -> None:
        with TemporaryDirectory() as name:
            directory = _write_registry(
                Path(name),
                rules__s3__json=[
                    {
                        "rule_id": "S3-PUBLIC-001",
                        "version": "2026-08-31",
                        "title": "rule",
                        "severity": "HIGH",
                        "applicable_phases": ["INITIAL"],
                        "resource_types": [S3],
                        "source_references": [
                            {
                                "source_id": "unknown-source",
                                "source_version": "2026-08-24",
                                "locator": "part2/5.1-B",
                                "content_sha256": "0" * 64,
                            }
                        ],
                    }
                ],
            )

            with self.assertRaisesRegex(PolicyRegistryError, "undeclared source versions"):
                load_rule_registry(directory)

    def test_rejects_a_reference_pinned_to_an_undeclared_source_version(self) -> None:
        """원문이 개정되면 Rule이 가리키던 판본이 남아야 한다."""
        with TemporaryDirectory() as name:
            directory = _write_registry(
                Path(name),
                rules__s3__json=[
                    {
                        "rule_id": "S3-PUBLIC-001",
                        "version": "2026-08-31",
                        "title": "rule",
                        "severity": "HIGH",
                        "applicable_phases": ["INITIAL"],
                        "resource_types": [S3],
                        "source_references": [
                            {
                                "source_id": "isms-p-2023",
                                "source_version": "1999-01-01",
                                "locator": "control/2.6.2",
                                "content_sha256": "0" * 64,
                            }
                        ],
                    }
                ],
            )

            with self.assertRaisesRegex(PolicyRegistryError, "isms-p-2023@1999-01-01"):
                load_rule_registry(directory)

    def test_rejects_a_control_that_references_a_missing_rule(self) -> None:
        with TemporaryDirectory() as name:
            directory = _write_registry(
                Path(name),
                controls__json=[
                    {
                        "control_id": "ISMS-P-2.6.2",
                        "title": "정보시스템 접근",
                        "source_reference": {
                            "source_id": "isms-p-2023",
                            "source_version": "2023-10-31",
                            "locator": "control/2.6.2",
                            "content_sha256": "0" * 64,
                        },
                        "rule_references": [{"rule_id": "S3-MISSING-001", "version": "v1"}],
                    }
                ],
            )

            with self.assertRaisesRegex(PolicyRegistryError, "unavailable rule"):
                load_rule_registry(directory)

    def test_rejects_a_definition_that_is_missing_a_required_field(self) -> None:
        with TemporaryDirectory() as name:
            directory = _write_registry(
                Path(name),
                rules__s3__json=[
                    {
                        "rule_id": "S3-PUBLIC-001",
                        "version": "2026-08-31",
                        "title": "rule",
                        "severity": "HIGH",
                        "applicable_phases": ["INITIAL"],
                        "resource_types": [S3],
                        "source_references": [
                            {
                                "source_id": "isms-p-2023",
                                "locator": "control/2.6.2",
                                "content_sha256": "0" * 64,
                            }
                        ],
                    }
                ],
            )

            with self.assertRaisesRegex(PolicyRegistryError, "definition is invalid"):
                load_rule_registry(directory)

    def test_rejects_duplicate_source_versions(self) -> None:
        with TemporaryDirectory() as name:
            sources = json.loads((REGISTRY_PATH / "sources.json").read_text(encoding="utf-8"))
            directory = _write_registry(Path(name), sources__json=[*sources, sources[0]])

            with self.assertRaisesRegex(PolicyRegistryError, "duplicate policy source"):
                load_rule_registry(directory)

    def test_rejects_a_registry_file_that_is_not_a_list(self) -> None:
        with TemporaryDirectory() as name:
            directory = _write_registry(Path(name), profiles__json={"policy_profile_id": "x"})

            with self.assertRaisesRegex(PolicyRegistryError, "must contain a list"):
                load_rule_registry(directory)

    def test_rejects_a_directory_without_rule_files(self) -> None:
        with TemporaryDirectory() as name:
            directory = Path(name)
            for filename in ("sources.json", "profiles.json", "controls.json"):
                (directory / filename).write_text("[]", encoding="utf-8")

            with self.assertRaisesRegex(PolicyRegistryError, "no rules"):
                load_rule_registry(directory)


class ProfileAllowListTest(unittest.TestCase):
    """Profile allow-list 밖의 Rule은 어떤 Resource 유형으로도 Context에 들어가지 않는다."""

    def setUp(self) -> None:
        self.registry = load_rule_registry(REGISTRY_PATH)
        self.resolver = PolicyContextResolver(self.registry.catalog)

    def test_s3_context_contains_only_profile_rules_in_profile_order(self) -> None:
        context = self.resolver.resolve(
            policy_profile_id=PROFILE_ID, phase=AssessmentPhase.INITIAL, resource_type=S3
        )

        self.assertEqual(
            [rule.rule_id for rule in context.rules],
            [
                "S3-PUBLIC-001",
                "S3-ACL-001",
                "S3-POLICY-001",
                "S3-ENCRYPT-001",
                "S3-TLS-001",
                "S3-LOGGING-001",
            ],
        )
        self.assertEqual(context.policy_profile_version, "v2")

    def test_context_evidence_locators_are_deduplicated(self) -> None:
        """여러 Rule이 같은 통제를 인용해도 Evidence locator는 한 번만 노출된다."""
        context = self.resolver.resolve(
            policy_profile_id=PROFILE_ID, phase=AssessmentPhase.INITIAL, resource_type=S3
        )

        references = context.source_references
        total = sum(len(rule.source_references) for rule in context.rules)

        self.assertLess(len(references), total)
        self.assertEqual(len(references), len(set(references)))

    def test_registered_ec2_rules_stay_out_of_the_approved_profile(self) -> None:
        """M1 평가 대상은 S3 단독이다. EC2 Rule은 Registry에만 있고 Profile에는 없다."""
        profile = self.registry.catalog.get_profile(PROFILE_ID)
        assert profile is not None
        profile_rule_ids = {reference.rule_id for reference in profile.rule_references}

        self.assertTrue(any(rule.rule_id.startswith("EC2-") for rule in self.registry.rules))
        self.assertFalse({rule_id for rule_id in profile_rule_ids if rule_id.startswith("EC2-")})

        with self.assertRaisesRegex(PolicyNotFoundError, "no applicable policy rules"):
            self.resolver.resolve(
                policy_profile_id=PROFILE_ID, phase=AssessmentPhase.INITIAL, resource_type=EC2
            )

    def test_resolver_filters_by_resource_type_for_a_profile_that_allows_ec2(self) -> None:
        """Mapping/Context 계층 자체는 multi-type에서 동작한다 (M2 EC2 확장 대비)."""
        catalog = self.registry.catalog
        ec2_rule = catalog.get_rule("EC2-EBS-ENCRYPT-001", "2026-08-31")
        assert ec2_rule is not None
        self.assertIn(EC2, ec2_rule.resource_types)
        self.assertNotIn(S3, ec2_rule.resource_types)

        mixed_profile = self._profile_with_all_rules()
        context = PolicyContextResolver(mixed_profile).resolve(
            policy_profile_id="profile-mixed", phase=AssessmentPhase.INITIAL, resource_type=EC2
        )

        self.assertEqual([rule.rule_id for rule in context.rules], ["EC2-EBS-ENCRYPT-001"])

    def _profile_with_all_rules(self):
        from apps.backend.policy import InMemoryPolicyCatalog
        from packages.contracts import PolicyProfile, PolicyRuleReference

        profile = PolicyProfile(
            policy_profile_id="profile-mixed",
            version="v1",
            rule_references=tuple(
                PolicyRuleReference(rule_id=rule.rule_id, version=rule.version)
                for rule in self.registry.rules
            ),
        )
        return InMemoryPolicyCatalog(profiles=(profile,), rules=self.registry.rules)


class ControlMappingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = load_rule_registry(REGISTRY_PATH)

    def test_maps_a_rule_to_the_controls_it_implements(self) -> None:
        controls = self.registry.controls.controls_for_rule(
            rule_id="S3-PUBLIC-001", version="2026-08-31"
        )

        self.assertEqual(
            [control.control_id for control in controls], ["ISMS-P-2.10.2", "ISMS-P-2.10.3"]
        )

    def test_expands_a_control_to_its_resource_types(self) -> None:
        resource_types = self.registry.controls.resource_types_for_control(
            "ISMS-P-2.7.1", catalog=self.registry.catalog
        )

        self.assertEqual(resource_types, (S3, EC2, "AWS::EC2::Volume"))

    def test_reports_the_controls_a_resolved_context_covers(self) -> None:
        context = PolicyContextResolver(self.registry.catalog).resolve(
            policy_profile_id=PROFILE_ID, phase=AssessmentPhase.INITIAL, resource_type=S3
        )

        covered = self.registry.controls.covered_controls(context)

        self.assertEqual(
            [control.control_id for control in covered],
            ["ISMS-P-2.10.2", "ISMS-P-2.10.3", "ISMS-P-2.6.2", "ISMS-P-2.7.1", "ISMS-P-2.9.4"],
        )

    def test_reports_partial_control_coverage(self) -> None:
        """S3 Context는 ISMS-P-2.6.2의 EC2 Rule을 평가하지 않는다. 완전 평가로 보이면 안 된다."""
        context = PolicyContextResolver(self.registry.catalog).resolve(
            policy_profile_id=PROFILE_ID, phase=AssessmentPhase.INITIAL, resource_type=S3
        )

        coverage = {
            entry.control_id: entry
            for entry in self.registry.controls.control_rule_coverage(context)
        }

        partial = coverage["ISMS-P-2.6.2"]
        self.assertEqual((partial.evaluated_rules, partial.total_rules), (2, 3))
        self.assertFalse(partial.is_complete)
        self.assertTrue(coverage["ISMS-P-2.9.4"].is_complete)

    def test_rejects_an_unknown_control(self) -> None:
        with self.assertRaisesRegex(PolicyNotFoundError, "not found"):
            self.registry.controls.resource_types_for_control(
                "ISMS-P-9.9.9", catalog=self.registry.catalog
            )


if __name__ == "__main__":
    unittest.main()
