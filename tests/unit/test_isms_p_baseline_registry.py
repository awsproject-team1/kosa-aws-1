"""The ISMS-P baseline registry is complete, honest about automation, and publishes safely.

ISMS-P는 고객이 올리는 문서가 아니라 운영자 기준선이다(ADR-0026). 이 파일은 그 기준선이
(1) 인증기준 101개 항목을 하나도 빠뜨리지 않고 항목마다 MANUAL Rule을 갖고,
(2) Catalog의 자동 판정 통제마다 Rule 하나가 그 통제가 근거가 되는 항목들을 인용하며,
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
    ControlAutomationSupport,
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
#: 자동 판정 근거를 하나라도 갖는 항목. 전부 영역 2(보호대책)의 기술 통제다.
EXPECTED_AUTOMATED_ITEMS = {
    "2.6.1",
    "2.6.2",
    "2.6.4",
    "2.6.6",
    "2.6.7",
    "2.7.1",
    "2.9.4",
    "2.10.2",
    "2.10.3",
    "2.10.4",
    "2.10.5",
}
PROFILE_ID = "profile-isms-p-baseline"

REGISTRY = load_rule_registry(BASELINE_DIR)
LEGACY = load_rule_registry(LEGACY_DIR)
MANUAL_RULES = tuple(r for r in REGISTRY.rules if r.evaluation_type is RuleEvaluationType.MANUAL)
AUTOMATED_RULES = tuple(
    r for r in REGISTRY.rules if r.evaluation_type is not RuleEvaluationType.MANUAL
)


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
        expression = str(kwargs.get("ConditionExpression", ""))
        if expression.startswith("current_version = "):
            values = kwargs["ExpressionAttributeValues"]
            assert isinstance(values, dict)
            existing = self.items.get(key)
            if existing is None or existing.get("current_version") != values[":current"]:
                raise ConditionalFailure()
            self.items[key] = dict(item)
            return
        if key in self.items:
            raise ConditionalFailure()
        self.items[key] = dict(item)

    def get_item(self, **kwargs: object) -> Mapping[str, object]:
        key = kwargs["Key"]
        assert isinstance(key, dict)
        item = self.items.get((key["PK"], key["SK"]))
        return {} if item is None else {"Item": dict(item)}


class CompletenessTest(unittest.TestCase):
    def test_every_certification_control_has_exactly_one_manual_rule(self) -> None:
        """101개 항목. 하나라도 빠지면 그 항목은 어떤 고객에게도 검토 좌표가 되지 않는다."""
        by_part: dict[str, int] = {}
        for rule in MANUAL_RULES:
            part = rule.rule_id.removeprefix("ISMSP-").split(".")[0]
            by_part[part] = by_part.get(part, 0) + 1
        self.assertEqual(by_part, EXPECTED_CONTROLS_BY_PART)
        self.assertEqual(len(MANUAL_RULES), sum(EXPECTED_CONTROLS_BY_PART.values()))
        self.assertEqual(
            sorted(REGISTRY.controls.control_ids),
            sorted(f"ISMS-P-{r.rule_id.removeprefix('ISMSP-')}" for r in MANUAL_RULES),
        )

    def test_the_profile_carries_every_rule_in_one_isms_p_segment(self) -> None:
        (profile,) = REGISTRY.profiles
        self.assertEqual(profile.policy_profile_id, PROFILE_ID)
        self.assertEqual(profile.version, "v2")
        self.assertEqual(
            {reference.rule_id for reference in profile.rule_references},
            {rule.rule_id for rule in REGISTRY.rules},
        )
        self.assertEqual(profile.source_kinds, (PolicySourceKind.ISMS_P,))
        (segment,) = profile.segments
        self.assertEqual(len(segment.rule_references), len(REGISTRY.rules))

    def test_every_manual_rule_cites_its_own_control_locator_only(self) -> None:
        """locator는 `control/x.y.z` 하나다. 원문 문장은 저장소에 없고 digest만 남는다."""
        for rule in MANUAL_RULES:
            (reference,) = rule.source_references
            self.assertEqual(reference.source_id, "isms-p-2023")
            self.assertEqual(reference.source_version, "2023-10-31")
            self.assertEqual(reference.locator, f"control/{rule.rule_id.removeprefix('ISMSP-')}")
            self.assertEqual(len(reference.content_sha256), 64)


class ManualSemanticsTest(unittest.TestCase):
    def test_every_manual_rule_is_a_governance_coordinate(self) -> None:
        """인증기준 항목은 심사원이 증적을 보고 판정한다. 어떤 도구도 부르지 않아야 한다."""
        control = MVP_CONTROL_CATALOG.control(MANUAL_CONTROL_KEY)
        assert control is not None
        for rule in MANUAL_RULES:
            self.assertEqual(rule.control_key, MANUAL_CONTROL_KEY)
            self.assertEqual(rule.resource_types, (GOVERNANCE_ASSESSMENT_RESOURCE_TYPE,))
            self.assertEqual(rule.required_evidence, ())
            self.assertIs(rule.severity, control.default_severity)
            self.assertIn(AssessmentPhase.INITIAL, rule.applicable_phases)
            self.assertIn(AssessmentPhase.POST_DEPLOY_VERIFICATION, rule.applicable_phases)

    def test_the_manual_evaluator_accepts_a_baseline_rule_without_calling_anything(self) -> None:
        rule = MANUAL_RULES[0]
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
        for rule in MANUAL_RULES:
            self.assertLess(len(rule.title), 80, rule.rule_id)
            self.assertIsNone(rule.evaluation_rubric)
            self.assertLess(len(rule.applicability_semantics or ""), 120, rule.rule_id)


class AutomatedEvidenceTest(unittest.TestCase):
    """One rule per automatable catalog control, citing the items it is evidence for."""

    def test_every_automatable_catalog_control_has_exactly_one_rule(self) -> None:
        """통제 하나에 Rule 하나. 항목마다 복제하면 같은 사실이 항목 수만큼 점수에 들어간다."""
        automatable = {
            c.control_key
            for c in MVP_CONTROL_CATALOG.controls
            if c.automation_support is ControlAutomationSupport.AVAILABLE
        }
        self.assertEqual({r.control_key for r in AUTOMATED_RULES}, automatable)
        self.assertEqual(len(AUTOMATED_RULES), len(automatable))

    def test_automated_rules_carry_the_catalog_execution_semantics(self) -> None:
        """authoring이 만드는 AUTOMATABLE Rule과 같은 모양이어야 같은 runtime을 탄다."""
        for rule in AUTOMATED_RULES:
            control = MVP_CONTROL_CATALOG.control(rule.control_key or "")
            assert control is not None, rule.rule_id
            self.assertIs(rule.evaluation_type, RuleEvaluationType.HYBRID)
            self.assertEqual(rule.control_catalog_version, MVP_CONTROL_CATALOG.version)
            self.assertEqual(rule.resource_types, control.supported_resource_types)
            self.assertEqual(rule.required_evidence, control.baseline_required_evidence)
            self.assertEqual(rule.optional_evidence, control.baseline_optional_evidence)
            self.assertIs(rule.severity, control.default_severity)
            self.assertEqual(rule.evaluation_rubric, control.description)
            self.assertIn(AssessmentPhase.DEPLOYMENT_READINESS, rule.applicable_phases)
            self.assertTrue(rule.source_references, rule.rule_id)
            for reference in rule.source_references:
                self.assertEqual(reference.source_id, "isms-p-2023")

    def test_exactly_the_technical_items_have_automated_evidence(self) -> None:
        """11개 항목. 관리체계(1.x)와 개인정보(3.x)는 자동 근거가 없다 — 있다고 말하면 거짓이다."""
        cited = {
            ref.locator.removeprefix("control/")
            for rule in AUTOMATED_RULES
            for ref in rule.source_references
        }
        self.assertEqual(cited, EXPECTED_AUTOMATED_ITEMS)

    def test_an_item_with_automated_evidence_keeps_its_manual_rule(self) -> None:
        """자동 근거는 항목의 확인사항 일부에만 답한다. 나머지는 여전히 사람이 검토한다."""
        for item in EXPECTED_AUTOMATED_ITEMS:
            control = REGISTRY.controls.get_control(f"ISMS-P-{item}")
            assert control is not None, item
            rule_ids = [reference.rule_id for reference in control.rule_references]
            self.assertIn(f"ISMSP-{item}", rule_ids)
            self.assertGreater(len(rule_ids), 1, item)

    def test_an_item_without_automated_evidence_has_only_its_manual_rule(self) -> None:
        for control in (
            REGISTRY.controls.get_control(cid) for cid in REGISTRY.controls.control_ids
        ):
            assert control is not None
            item = control.control_id.removeprefix("ISMS-P-")
            if item in EXPECTED_AUTOMATED_ITEMS:
                continue
            self.assertEqual(
                [reference.rule_id for reference in control.rule_references], [f"ISMSP-{item}"]
            )


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

        # Source 1개는 이미 있으므로 새로 써지는 것은 Rule 전부 + Profile item 2다.
        self.assertEqual(bootstrap.publish(REGISTRY), len(REGISTRY.rules) + 2)
        self.assertEqual(bootstrap.publish(REGISTRY), 0)
        rule_item = table.items[("CUSTOMER#cust-001", "RULE#ISMSP-1.1.1#VERSION#2023-10-31")]
        self.assertEqual(rule_item["lifecycle"], RuleLifecycle.APPROVED.value)
        self.assertEqual(rule_item["evaluation_type"], "MANUAL")

        catalog = DynamoDbPolicyCatalog(table, customer_id="cust-001")
        resolver = PolicyContextResolver(catalog)
        governance = resolver.resolve(
            policy_profile_id=PROFILE_ID,
            phase=AssessmentPhase.INITIAL,
            resource_type=GOVERNANCE_ASSESSMENT_RESOURCE_TYPE,
        )
        self.assertEqual(len(governance.rules), len(MANUAL_RULES))
        buckets = resolver.resolve(
            policy_profile_id=PROFILE_ID,
            phase=AssessmentPhase.INITIAL,
            resource_type="AWS::S3::Bucket",
        )
        self.assertEqual(
            sorted(rule.control_key or "" for rule in buckets.rules),
            [
                "S3_BLOCK_PUBLIC_ACCESS",
                "S3_BUCKET_ACL_DISABLED",
                "S3_BUCKET_POLICY_RESTRICTED",
                "S3_ENCRYPTION_AT_REST",
                "S3_SERVER_ACCESS_LOGGING",
                "S3_TLS_ONLY",
            ],
        )

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

    def test_the_mapping_only_names_automatable_controls_and_real_items(self) -> None:
        """매핑은 사람이 적은 표다. KNOWN_UNSUPPORTED 통제나 없는 항목을 적으면 여기서 걸린다."""
        module = _build_module()
        controls = [
            module.Control(item, "x", "p", "s")
            for item in sorted({i for items in module.AUTOMATABLE_MAPPING.values() for i in items})
        ]
        module._require_mapping_targets(controls)  # 커밋된 표는 통과한다.

        module.AUTOMATABLE_MAPPING["EC2_SNAPSHOT_NOT_PUBLIC"] = ("2.6.2",)
        with self.assertRaisesRegex(ValueError, "not an automatable"):
            module._require_mapping_targets(controls)

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
