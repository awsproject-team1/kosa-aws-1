"""The ISMS-P baseline registry is complete, manual, and publishes like any other registry.

ISMS-P는 고객이 올리는 문서가 아니라 운영자 기준선이다(ADR-0026). 이 파일은 그 기준선이
(1) 인증기준 101개 항목을 하나도 빠뜨리지 않고, (2) 전부 사람이 판정하는 MANUAL Rule이며,
(3) legacy Registry와 같은 Source 바이트를 선언해 bootstrap이 fail-closed하지 않고,
(4) 게시 요청의 `baseline`으로 들어가 `ISMS_P` Segment를 만든다는 것을 고정한다.

원문(`policies-local/`)이 없는 환경에서는 원문을 읽는 검사만 건너뛴다(ADR-0004).
"""

import importlib.util
import unittest
from collections.abc import Mapping
from pathlib import Path

from apps.backend.assessment.manual_review import ManualReviewEvaluator
from apps.backend.policy import (
    DynamoDbPolicyCatalog,
    DynamoDbPolicyCatalogBootstrap,
    PolicyContextResolver,
    load_rule_registry,
)
from apps.backend.policy.control_catalog import (
    GOVERNANCE_ASSESSMENT_RESOURCE_TYPE,
    MANUAL_CONTROL_KEY,
    MVP_CONTROL_CATALOG,
)
from apps.backend.policy.ingestion.approval import ProfileBaseline, publish_profile
from packages.contracts import (
    AssessmentPhase,
    EvaluationStatus,
    ModelProfile,
    ModelProfileRole,
    PolicySourceKind,
    RuleEvaluationType,
    RuleLifecycle,
)

REPO_ROOT = Path(__file__).parents[2]
BASELINE_DIR = REPO_ROOT / "fixtures" / "baselines" / "isms-p-2023"
LEGACY_DIR = REPO_ROOT / "fixtures" / "rules"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_isms_p_baseline.py"

#: ISMS-P 2023-10-31 인증기준의 영역별 항목 수. 관리체계 16, 보호대책 64, 개인정보 21.
EXPECTED_CONTROLS_BY_PART = {"1": 16, "2": 64, "3": 21}
PROFILE_ID = "profile-isms-p-baseline"

REGISTRY = load_rule_registry(BASELINE_DIR)
LEGACY = load_rule_registry(LEGACY_DIR)


def _build_module():
    spec = importlib.util.spec_from_file_location("build_isms_p_baseline", BUILD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConditionalFailure(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class Table:
    """The same minimal table the bootstrap test uses: conditional puts, consistent gets."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, object]] = {}

    def put_item(self, **kwargs: object) -> None:
        item = kwargs["Item"]
        assert isinstance(item, dict)
        key = (item["PK"], item["SK"])
        if key in self.items:
            raise ConditionalFailure()
        self.items[key] = dict(item)

    def get_item(self, **kwargs: object) -> Mapping[str, object]:
        key = kwargs["Key"]
        assert isinstance(key, dict)
        item = self.items.get((key["PK"], key["SK"]))
        return {} if item is None else {"Item": item}


class CompletenessTest(unittest.TestCase):
    def test_every_certification_control_has_exactly_one_rule_and_one_control(self) -> None:
        """101개 항목. 하나라도 빠지면 그 항목은 어떤 고객에게도 검토 좌표가 되지 않는다."""
        by_part: dict[str, int] = {}
        for rule in REGISTRY.rules:
            control_id = rule.rule_id.removeprefix("ISMSP-")
            by_part[control_id.split(".")[0]] = by_part.get(control_id.split(".")[0], 0) + 1
        self.assertEqual(by_part, EXPECTED_CONTROLS_BY_PART)
        self.assertEqual(len(REGISTRY.rules), sum(EXPECTED_CONTROLS_BY_PART.values()))
        self.assertEqual(
            sorted(REGISTRY.controls.control_ids),
            sorted(f"ISMS-P-{rule.rule_id.removeprefix('ISMSP-')}" for rule in REGISTRY.rules),
        )
        for rule in REGISTRY.rules:
            implemented = REGISTRY.controls.controls_for_rule(
                rule_id=rule.rule_id, version=rule.version
            )
            self.assertEqual(len(implemented), 1, rule.rule_id)

    def test_the_profile_carries_every_rule_in_one_isms_p_segment(self) -> None:
        (profile,) = REGISTRY.profiles
        self.assertEqual(profile.policy_profile_id, PROFILE_ID)
        self.assertEqual(
            {reference.rule_id for reference in profile.rule_references},
            {rule.rule_id for rule in REGISTRY.rules},
        )
        self.assertEqual(profile.source_kinds, (PolicySourceKind.ISMS_P,))
        (segment,) = profile.segments
        self.assertEqual(len(segment.rule_references), len(REGISTRY.rules))

    def test_every_rule_cites_one_control_locator_of_the_certification_source(self) -> None:
        """locator는 `control/x.y.z` 하나다. 원문 문장은 저장소에 없고 digest만 남는다."""
        for rule in REGISTRY.rules:
            (reference,) = rule.source_references
            self.assertEqual(reference.source_id, "isms-p-2023")
            self.assertEqual(reference.source_version, "2023-10-31")
            self.assertEqual(reference.locator, f"control/{rule.rule_id.removeprefix('ISMSP-')}")
            self.assertEqual(len(reference.content_sha256), 64)


class ManualSemanticsTest(unittest.TestCase):
    def test_every_rule_is_a_manual_governance_coordinate(self) -> None:
        """인증기준 항목은 심사원이 증적을 보고 판정한다. 어떤 도구도 부르지 않아야 한다."""
        control = MVP_CONTROL_CATALOG.control(MANUAL_CONTROL_KEY)
        for rule in REGISTRY.rules:
            self.assertIs(rule.evaluation_type, RuleEvaluationType.MANUAL, rule.rule_id)
            self.assertEqual(rule.control_key, MANUAL_CONTROL_KEY)
            self.assertEqual(rule.resource_types, (GOVERNANCE_ASSESSMENT_RESOURCE_TYPE,))
            self.assertEqual(rule.required_evidence, ())
            self.assertIs(rule.severity, control.default_severity)
            self.assertIn(AssessmentPhase.INITIAL, rule.applicable_phases)
            self.assertIn(AssessmentPhase.POST_DEPLOY_VERIFICATION, rule.applicable_phases)

    def test_the_manual_evaluator_accepts_a_baseline_rule_without_calling_anything(self) -> None:
        rule = REGISTRY.rules[0]
        catalog = _catalog_for(REGISTRY)
        context = PolicyContextResolver(catalog).resolve(
            policy_profile_id=PROFILE_ID,
            phase=AssessmentPhase.INITIAL,
            resource_type=GOVERNANCE_ASSESSMENT_RESOURCE_TYPE,
        )
        result = ManualReviewEvaluator().evaluate(
            resource_id="governance:repo-001",
            rule=rule,
            context=context,
            model_profile=ModelProfile(
                model_profile_id="assessment-v1",
                role=ModelProfileRole.ASSESSMENT,
                region="us-east-1",
                model_id="unused",
                prompt_version="assessment/1",
                rubric_version="assessment-rubric/1",
                golden_dataset_version="assessment-golden/1",
            ),
        )
        self.assertIs(result.status, EvaluationStatus.MANUAL_REVIEW)
        self.assertEqual(result.evidence_references, ("isms-p-2023@2023-10-31#control/1.1.1",))

    def test_no_stored_field_carries_certification_prose(self) -> None:
        """항목 번호·항목명·분야명만 남긴다. 상세내용·확인사항 문장은 원문이다(ADR-0004)."""
        for rule in REGISTRY.rules:
            self.assertLess(len(rule.title), 80, rule.rule_id)
            self.assertIsNone(rule.evaluation_rubric)
            self.assertIsNone(rule.severity_guidance)
            semantics = rule.applicability_semantics or ""
            self.assertLess(len(semantics), 120, rule.rule_id)


class PublicationTest(unittest.TestCase):
    def test_the_source_is_byte_identical_to_the_legacy_registry(self) -> None:
        """다르면 두 번째 bootstrap이 `different immutable content`로 fail-closed한다."""
        self.assertEqual(
            REGISTRY.get_source("isms-p-2023", "2023-10-31"),
            LEGACY.get_source("isms-p-2023", "2023-10-31"),
        )

    def test_bootstrap_publishes_after_the_legacy_registry_and_is_idempotent(self) -> None:
        """같은 파티션에 legacy를 먼저 게시한 뒤 기준선을 게시한다 — Source item이 겹친다."""
        table = Table()
        bootstrap = DynamoDbPolicyCatalogBootstrap(table, customer_id="cust-001")
        bootstrap.publish(LEGACY)

        # Source 1개는 이미 있으므로 새로 써지는 것은 Rule 101 + Profile item 2다.
        self.assertEqual(bootstrap.publish(REGISTRY), len(REGISTRY.rules) + 2)
        self.assertEqual(bootstrap.publish(REGISTRY), 0)
        rule_item = table.items[("CUSTOMER#cust-001", "RULE#ISMSP-1.1.1#VERSION#2023-10-31")]
        self.assertEqual(rule_item["lifecycle"], RuleLifecycle.APPROVED.value)
        self.assertEqual(rule_item["evaluation_type"], "MANUAL")

        catalog = DynamoDbPolicyCatalog(table, customer_id="cust-001")
        context = PolicyContextResolver(catalog).resolve(
            policy_profile_id=PROFILE_ID,
            phase=AssessmentPhase.INITIAL,
            resource_type=GOVERNANCE_ASSESSMENT_RESOURCE_TYPE,
        )
        self.assertEqual(len(context.rules), len(REGISTRY.rules))

    def test_the_baseline_joins_a_customer_profile_as_an_isms_p_segment(self) -> None:
        """게시 요청의 `baseline`으로 들어가면 Segment가 ISMS_P로 갈린다 — 점수가 나뉘는 근거."""
        (profile,) = REGISTRY.profiles
        baseline = ProfileBaseline(
            policy_profile_id=profile.policy_profile_id,
            version=profile.version,
            rules=REGISTRY.rules,
            sources=REGISTRY.sources,
        )

        published = publish_profile(
            policy_profile_id="profile-customer",
            version="v1",
            candidates=(),
            approvals=(),
            baseline=baseline,
        )

        self.assertEqual(len(published.rule_references), len(REGISTRY.rules))
        self.assertEqual(published.source_kinds, (PolicySourceKind.ISMS_P,))


class GeneratorTest(unittest.TestCase):
    def test_read_controls_walks_headings_sections_and_items(self) -> None:
        """원문 없이도 검사할 수 있는 파싱 경계. 분야가 시작되는 행은 앞에 두 셀이 더 붙는다."""
        module = _build_module()
        rows = [
            ["1. 관리체계 수립 및 운영(16개)/세부항목(42개)"],
            ["분야", "항목", "상세내용", "주요 확인사항"],
            ["1.1.", " 관리체계 기반 마련", "1.1.1", "경영진의 참여", "상세", "확인"],
            ["", "", "", "", "", "확인 2"],
            ["1.1.2", "최고책임자의 지정", "상세", "확인"],
            ["2. 보호대책 요구사항(64개)/세부항목(210개)"],
            ["2.1.", " 정책, 조직, 자산 관리", "2.1.1", "정책의 유지관리", "상세", "확인"],
        ]

        controls = module.read_controls(rows)

        self.assertEqual(
            [(c.control_id, c.name, c.part_name, c.section_name) for c in controls],
            [
                ("1.1.1", "경영진의 참여", "관리체계 수립 및 운영", "관리체계 기반 마련"),
                ("1.1.2", "최고책임자의 지정", "관리체계 수립 및 운영", "관리체계 기반 마련"),
                ("2.1.1", "정책의 유지관리", "보호대책 요구사항", "정책, 조직, 자산 관리"),
            ],
        )

    def test_a_repeated_control_in_the_original_is_refused(self) -> None:
        module = _build_module()
        rows = [
            ["1. 관리체계(16개)/세부항목(42개)"],
            ["1.1.", "기반", "1.1.1", "경영진의 참여", "상세", "확인"],
            ["1.1.1", "경영진의 참여", "상세", "확인"],
        ]
        with self.assertRaisesRegex(ValueError, "appears twice"):
            module.read_controls(rows)

    def test_the_committed_registry_matches_a_fresh_build_of_the_original(self) -> None:
        """원문 보유자만 실행된다. 커밋본이 생성본과 다르면 누군가 손으로 고친 것이다."""
        module = _build_module()
        digest = module._digest_module()
        try:
            documents = module.build(digest)
        except digest.PolicySourceUnavailableError:
            self.skipTest("policy original not available locally (ADR-0004)")
        self.assertEqual(module.check(documents), [])


def _catalog_for(registry) -> DynamoDbPolicyCatalog:
    table = Table()
    DynamoDbPolicyCatalogBootstrap(table, customer_id="cust-001").publish(registry)
    return DynamoDbPolicyCatalog(table, customer_id="cust-001")


if __name__ == "__main__":
    unittest.main()
