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
RDS = "AWS::RDS::DBInstance"
ALB = "AWS::ElasticLoadBalancingV2::LoadBalancer"
MULTIRESOURCE_PROFILE_ID = "profile-multiresource-baseline"


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

    def test_pins_the_profile_version_when_one_is_expected(self) -> None:
        """비동기 Job은 승인 시점 Profile version으로 고정된다."""
        context = self.resolver.resolve(
            policy_profile_id=PROFILE_ID,
            phase=AssessmentPhase.INITIAL,
            resource_type=S3,
            expected_profile_version="v2",
        )

        self.assertEqual(context.policy_profile_version, "v2")

    def test_rejects_a_profile_replaced_after_approval(self) -> None:
        """Job 생성 뒤 Profile이 교체되면 다른 allow-list로 평가하지 않고 실패한다."""
        with self.assertRaisesRegex(PolicyNotFoundError, "policy profile version changed"):
            self.resolver.resolve(
                policy_profile_id=PROFILE_ID,
                phase=AssessmentPhase.INITIAL,
                resource_type=S3,
                expected_profile_version="v1",
            )

    def test_context_allows_only_canonical_policy_evidence(self) -> None:
        context = self.resolver.resolve(
            policy_profile_id=PROFILE_ID, phase=AssessmentPhase.INITIAL, resource_type=S3
        )

        allowed = next(iter(context.policy_evidence_references))
        self.assertIn("@", allowed)
        self.assertTrue(context.allows_evidence(allowed))
        # Resource 상태 근거는 별도 namespace로 허용한다.
        self.assertTrue(context.allows_evidence("aws:s3:bucket/b#read-resource"))
        self.assertTrue(context.allows_evidence("terraform:aws_s3_bucket_public_access_block"))
        # version 없는 구 형식과 Context 밖 정책 근거는 거부한다.
        self.assertFalse(context.allows_evidence("isms-p-2023#control/2.6.2"))
        self.assertFalse(context.allows_evidence("isms-p-2023@2023-10-31#control/9.9.9"))
        self.assertFalse(context.allows_evidence(""))
        self.assertFalse(context.allows_evidence(None))

    def test_registered_ec2_rules_stay_out_of_the_approved_profile(self) -> None:
        """승인된 `profile-mvp-baseline`은 S3 allow-list다. 신규 type Rule을 끼워 넣지 않는다."""
        profile = self.registry.catalog.get_profile(PROFILE_ID)
        assert profile is not None
        profile_rule_ids = {reference.rule_id for reference in profile.rule_references}

        self.assertTrue(any(rule.rule_id.startswith("EC2-") for rule in self.registry.rules))
        self.assertFalse(
            {
                rule_id
                for rule_id in profile_rule_ids
                if rule_id.startswith(("EC2-", "RDS-", "ALB-"))
            }
        )

        for resource_type in (EC2, RDS, ALB):
            with self.assertRaisesRegex(PolicyNotFoundError, "no applicable policy rules"):
                self.resolver.resolve(
                    policy_profile_id=PROFILE_ID,
                    phase=AssessmentPhase.INITIAL,
                    resource_type=resource_type,
                )

    def test_resolver_filters_by_resource_type_for_a_profile_that_allows_ec2(self) -> None:
        """Mapping/Context 계층은 multi-type에서 type별로만 Rule을 넘긴다."""
        catalog = self.registry.catalog
        ec2_rule = catalog.get_rule("EC2-EBS-ENCRYPT-001", "2026-08-31")
        assert ec2_rule is not None
        self.assertIn(EC2, ec2_rule.resource_types)
        self.assertNotIn(S3, ec2_rule.resource_types)

        mixed_profile = self._profile_with_all_rules()
        context = PolicyContextResolver(mixed_profile).resolve(
            policy_profile_id="profile-mixed", phase=AssessmentPhase.INITIAL, resource_type=EC2
        )

        self.assertEqual(
            [rule.rule_id for rule in context.rules],
            ["EC2-EBS-ENCRYPT-001", "EC2-PUBLIC-IP-001", "EC2-SG-INGRESS-001"],
        )
        # Snapshot 전용 Rule은 Instance Context에 들어오지 않는다.
        self.assertNotIn("EC2-SNAPSHOT-PUBLIC-001", [rule.rule_id for rule in context.rules])

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


class MultiResourceProfileTest(unittest.TestCase):
    """`profile-multiresource-baseline`이 S3/EC2/RDS/ALB 4종을 실제로 해석한다."""

    def setUp(self) -> None:
        self.registry = load_rule_registry(REGISTRY_PATH)
        self.resolver = PolicyContextResolver(self.registry.catalog)

    def _rule_ids(self, resource_type: str, phase: AssessmentPhase) -> list[str]:
        context = self.resolver.resolve(
            policy_profile_id=MULTIRESOURCE_PROFILE_ID,
            phase=phase,
            resource_type=resource_type,
        )
        return [rule.rule_id for rule in context.rules]

    def test_resolves_each_resource_type_to_only_its_own_rules(self) -> None:
        expected = {
            S3: [
                "S3-PUBLIC-001",
                "S3-ACL-001",
                "S3-POLICY-001",
                "S3-ENCRYPT-001",
                "S3-TLS-001",
                "S3-LOGGING-001",
            ],
            EC2: ["EC2-EBS-ENCRYPT-001", "EC2-PUBLIC-IP-001", "EC2-SG-INGRESS-001"],
            # 한 줄에 모으면 secret scanner가 Rule ID 목록을 generic API key로 오탐한다
            # ("ACCESS" 키워드 + 인접 문자열). Rule ID는 공개 식별자다 (AGENTS.md).
            RDS: [
                "RDS-PUBLIC-001",
                "RDS-ACCESS-001",
                "RDS-ENCRYPT-001",
                "RDS-LOGGING-001",
            ],
            ALB: ["ALB-HTTPS-001", "ALB-LOGGING-001"],
        }

        for resource_type, rule_ids in expected.items():
            with self.subTest(resource_type=resource_type):
                self.assertEqual(self._rule_ids(resource_type, AssessmentPhase.INITIAL), rule_ids)

    def test_every_profile_rule_is_reachable_from_some_resource_type(self) -> None:
        """Profile에 넣었지만 어떤 target type으로도 해석되지 않는 Rule은 평가되지 않는다."""
        profile = self.registry.catalog.get_profile(MULTIRESOURCE_PROFILE_ID)
        assert profile is not None
        declared = {reference.rule_id for reference in profile.rule_references}

        reachable = {
            rule_id
            for resource_type in (S3, EC2, RDS, ALB)
            for rule_id in self._rule_ids(resource_type, AssessmentPhase.INITIAL)
        }

        self.assertEqual(declared, reachable)

    def test_new_rules_apply_to_the_deployment_and_verification_phases(self) -> None:
        """조치 후 재평가가 원 Assessment와 같은 Rule 집합을 얻어야 비교가 성립한다."""
        for phase in (
            AssessmentPhase.DEPLOYMENT_READINESS,
            AssessmentPhase.POST_DEPLOY_VERIFICATION,
        ):
            for resource_type in (EC2, RDS, ALB):
                with self.subTest(phase=phase, resource_type=resource_type):
                    self.assertEqual(
                        self._rule_ids(resource_type, phase),
                        self._rule_ids(resource_type, AssessmentPhase.INITIAL),
                    )

    def test_every_registered_rule_carries_a_remediation_eligibility(self) -> None:
        """허용 범위 미등록 Rule은 조용히 MANUAL_REVIEW로 떨어진다. 등록을 강제한다."""
        for rule in self.registry.rules:
            with self.subTest(rule_id=rule.rule_id):
                self.assertIsNotNone(
                    self.registry.remediation.eligibility(
                        rule_id=rule.rule_id, version=rule.version
                    )
                )

    def test_every_registered_rule_implements_at_least_one_control(self) -> None:
        """Control에 매핑되지 않은 Rule은 Coverage 설명에 나타나지 않는다."""
        for rule in self.registry.rules:
            with self.subTest(rule_id=rule.rule_id):
                self.assertTrue(
                    self.registry.controls.controls_for_rule(
                        rule_id=rule.rule_id, version=rule.version
                    )
                )

    def test_context_allows_the_actual_evidence_namespace_of_every_type(self) -> None:
        for resource_type, locator in (
            (S3, "aws:s3:bucket/demo-bucket#read-resource"),
            (EC2, "aws:ec2:instance/i-0123456789abcdef0#read-resource"),
            (RDS, "aws:rds:db-instance/demo-db#read-resource"),
            (ALB, "aws:elasticloadbalancing:loadbalancer/app/demo/abc#read-resource"),
        ):
            with self.subTest(resource_type=resource_type):
                context = self.resolver.resolve(
                    policy_profile_id=MULTIRESOURCE_PROFILE_ID,
                    phase=AssessmentPhase.INITIAL,
                    resource_type=resource_type,
                )
                self.assertTrue(context.allows_evidence(locator))


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
        """암호화 통제는 네 Resource 범위 전체에 걸친다. 확장은 통제 단위로 관측된다."""
        resource_types = self.registry.controls.resource_types_for_control(
            "ISMS-P-2.7.1", catalog=self.registry.catalog
        )

        self.assertEqual(resource_types, (S3, EC2, "AWS::EC2::Volume", RDS, ALB))

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
        """S3 Context는 ISMS-P-2.6.2의 EC2/RDS Rule을 평가하지 않는다. 완전 평가로 보이면 안 된다."""
        context = PolicyContextResolver(self.registry.catalog).resolve(
            policy_profile_id=PROFILE_ID, phase=AssessmentPhase.INITIAL, resource_type=S3
        )

        coverage = {
            entry.control_id: entry
            for entry in self.registry.controls.control_rule_coverage(context)
        }

        partial = coverage["ISMS-P-2.6.2"]
        self.assertEqual((partial.evaluated_rules, partial.total_rules), (2, 5))
        self.assertFalse(partial.is_complete)
        # 2.9.4도 RDS/ALB 로깅 Rule을 얻었으므로 S3 Context만으로는 더 이상 완전하지 않다.
        self.assertFalse(coverage["ISMS-P-2.9.4"].is_complete)
        self.assertEqual(
            (coverage["ISMS-P-2.9.4"].evaluated_rules, coverage["ISMS-P-2.9.4"].total_rules),
            (1, 3),
        )

    def test_rejects_an_unknown_control(self) -> None:
        with self.assertRaisesRegex(PolicyNotFoundError, "not found"):
            self.registry.controls.resource_types_for_control(
                "ISMS-P-9.9.9", catalog=self.registry.catalog
            )


if __name__ == "__main__":
    unittest.main()
