"""The report splits readiness by policy origin when the Profile spans several.

한 Profile이 사내 정책 문서와 ISMS-P 기준선을 함께 담을 수 있게 되면서 필요해진 경로다. 두
준비도를 합친 하나의 숫자는 어느 기준에 대한 답도 아니므로, 보고서는 그 Assessment가 고정한
Profile 판본을 읽어 Rule의 원본을 알아내고 점수를 원본별로 낸다.

Store는 평가 계획이나 결과 item을 바꾸지 않는다. Assessment item이 이미 `policy_profile_id`와
`policy_profile_version`을 고정해 두었으므로 그 판본을 그대로 읽는다 — current pointer를 따라가면
그 사이에 교체된 Profile로 점수를 나누게 된다.
"""

import unittest

from apps.backend.assessment import (
    AssessmentEvaluationPlan,
    DynamoDbAssessmentReportStore,
    DynamoDbEvaluationResultStore,
)
from packages.contracts import (
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    PlannedEvaluation,
    PolicyProfile,
    PolicyProfileSegment,
    PolicyRuleReference,
    PolicySourceKind,
)
from tests.unit.test_dynamodb_assessment_report_store import Table

CUSTOMER = "cust-001"
ASSESSMENT = "asm-001"
INTERNAL_RULE = "CUST-PUBLIC-1"
ISMS_RULE = "ISMS-BASE-1"


def _reference(rule_id: str) -> PolicyRuleReference:
    return PolicyRuleReference(rule_id=rule_id, version="v1")


COMBINED_PROFILE = PolicyProfile(
    policy_profile_id="profile-combined",
    version="v1",
    rule_references=(_reference(INTERNAL_RULE), _reference(ISMS_RULE)),
    segments=(
        PolicyProfileSegment(
            kind=PolicySourceKind.INTERNAL_POLICY,
            source_id="src-internal",
            source_version="ver-1",
            rule_references=(_reference(INTERNAL_RULE),),
        ),
        PolicyProfileSegment(
            kind=PolicySourceKind.ISMS_P,
            source_id="isms-p-2023",
            source_version="2023-10-31",
            rule_references=(_reference(ISMS_RULE),),
        ),
    ),
)

SINGLE_ORIGIN_PROFILE = PolicyProfile(
    policy_profile_id="profile-internal",
    version="v1",
    rule_references=(_reference(INTERNAL_RULE),),
)


class Catalog:
    """The one Catalog read the report needs, scoped to one customer."""

    def __init__(self, customer_id: str, profiles: dict[tuple[str, str], PolicyProfile]) -> None:
        self.customer_id = customer_id
        self._profiles = profiles
        self.reads: list[tuple[str, str | None]] = []

    def get_profile(
        self, policy_profile_id: str, version: str | None = None
    ) -> PolicyProfile | None:
        self.reads.append((policy_profile_id, version))
        return self._profiles.get((policy_profile_id, version or ""))


def _result(rule_id: str, *, score: float, resource_id: str = "bucket-001") -> EvaluationResult:
    return EvaluationResult(
        resource_id=resource_id,
        rule_id=rule_id,
        perspective=EvaluationPerspective.IAC,
        status=EvaluationStatus.PASS if score == 100 else EvaluationStatus.FAIL,
        severity="HIGH",
        score=score,
        rationale="Fixture result.",
        evidence_references=("fixture:evidence",),
        rule_version="v1",
        rubric_version="v1",
        model_profile_id="assessment-profile-v1",
    )


def _plan(*rule_ids: str) -> AssessmentEvaluationPlan:
    return AssessmentEvaluationPlan(
        customer_id=CUSTOMER,
        assessment_id=ASSESSMENT,
        planned_coordinates=tuple(
            PlannedEvaluation(
                resource_id="bucket-001", rule_id=rule_id, perspective=EvaluationPerspective.IAC
            )
            for rule_id in rule_ids
        ),
    )


class SegmentReadinessReportTest(unittest.TestCase):
    def _store(
        self,
        table: Table,
        *,
        profile: PolicyProfile | None = COMBINED_PROFILE,
        pin_version: bool = True,
        catalogs: list[Catalog] | None = None,
    ) -> DynamoDbAssessmentReportStore:
        profiles = (
            {} if profile is None else {(profile.policy_profile_id, profile.version): profile}
        )
        assessment: dict[str, object] = {
            "PK": f"CUSTOMER#{CUSTOMER}",
            "SK": f"ASSESSMENT#{ASSESSMENT}",
            "entity_type": "ASSESSMENT",
            "customer_id": CUSTOMER,
            "job_id": "job-1",
            "policy_profile_id": "profile-combined"
            if profile is None
            else profile.policy_profile_id,
        }
        if pin_version:
            assessment["policy_profile_version"] = "v1" if profile is None else profile.version
        table.put_item(Item=assessment)

        def factory(customer_id: str) -> Catalog:
            catalog = Catalog(customer_id, profiles)
            if catalogs is not None:
                catalogs.append(catalog)
            return catalog

        return DynamoDbAssessmentReportStore(table, policy_catalog_factory=factory)

    def _seed_results(self, table: Table, *results: EvaluationResult) -> None:
        DynamoDbEvaluationResultStore(table).put_if_absent(
            customer_id=CUSTOMER, assessment_id=ASSESSMENT, results=results
        )

    def test_a_combined_profile_reports_one_score_per_policy_origin(self) -> None:
        table = Table()
        store = self._store(table)
        store.put_plan_if_absent(_plan(INTERNAL_RULE, ISMS_RULE))
        self._seed_results(table, _result(INTERNAL_RULE, score=100), _result(ISMS_RULE, score=0))

        report = store.get_report(customer_id=CUSTOMER, assessment_id=ASSESSMENT)

        scores = {entry.kind: entry.score for entry in report.segment_readiness}
        self.assertEqual(sorted(scores), ["INTERNAL_POLICY", "ISMS_P"])
        assert scores["INTERNAL_POLICY"] is not None and scores["ISMS_P"] is not None
        self.assertEqual(scores["INTERNAL_POLICY"].score, 100)
        self.assertEqual(scores["ISMS_P"].score, 0)

    def test_the_paged_report_carries_the_same_scores_as_the_whole_report(self) -> None:
        """페이지 조각으로 계산하면 그 페이지에 무엇이 실렸는지에 따라 점수가 달라진다."""
        table = Table()
        store = self._store(table)
        store.put_plan_if_absent(_plan(INTERNAL_RULE, ISMS_RULE))
        self._seed_results(table, _result(INTERNAL_RULE, score=100), _result(ISMS_RULE, score=0))

        # 페이지 경로는 계획 item의 완료 counter를 읽는다. Worker의 transactional update가
        # 없는 fake에서는 그 값을 직접 세워 "계획이 다 끝난" 상태를 만든다.
        table.items[(f"CUSTOMER#{CUSTOMER}", f"ASSESSMENT#{ASSESSMENT}#PLAN")][
            "completed_evaluations"
        ] = 2

        page = store.get_report_page(customer_id=CUSTOMER, assessment_id=ASSESSMENT, limit=1)

        self.assertEqual(len(page.results), 1)
        self.assertEqual(
            {entry.kind: entry.score.score for entry in page.segment_readiness if entry.score},
            {"INTERNAL_POLICY": 100, "ISMS_P": 0},
        )

    def test_a_profile_with_one_origin_is_not_split(self) -> None:
        table = Table()
        store = self._store(table, profile=SINGLE_ORIGIN_PROFILE)
        store.put_plan_if_absent(_plan(INTERNAL_RULE))
        self._seed_results(table, _result(INTERNAL_RULE, score=0))

        report = store.get_report(customer_id=CUSTOMER, assessment_id=ASSESSMENT)

        self.assertEqual(report.segment_readiness, ())
        assert report.readiness_score is not None
        self.assertEqual(report.readiness_score.score, 0)

    def test_a_store_without_a_catalog_reports_no_segments(self) -> None:
        """Catalog를 배선하지 않은 배포는 지금까지처럼 전체 점수 하나만 낸다."""
        table = Table()
        store = DynamoDbAssessmentReportStore(table)
        store.put_plan_if_absent(_plan(INTERNAL_RULE))
        self._seed_results(table, _result(INTERNAL_RULE, score=60))

        report = store.get_report(customer_id=CUSTOMER, assessment_id=ASSESSMENT)

        self.assertEqual(report.segment_readiness, ())

    def test_an_assessment_without_a_pinned_version_is_not_split(self) -> None:
        """current pointer를 따라가면 그 사이에 교체된 Profile로 점수를 나누게 된다."""
        table = Table()
        store = self._store(table, pin_version=False)
        store.put_plan_if_absent(_plan(INTERNAL_RULE, ISMS_RULE))
        self._seed_results(table, _result(INTERNAL_RULE, score=100), _result(ISMS_RULE, score=0))

        report = store.get_report(customer_id=CUSTOMER, assessment_id=ASSESSMENT)

        self.assertEqual(report.segment_readiness, ())

    def test_the_catalog_is_built_for_the_reading_customer(self) -> None:
        """Store 하나가 여러 고객의 보고서를 읽는다. 한 고객에 묶인 Catalog를 들고 있으면 안 된다."""
        table = Table()
        catalogs: list[Catalog] = []
        store = self._store(table, catalogs=catalogs)
        store.put_plan_if_absent(_plan(INTERNAL_RULE, ISMS_RULE))
        self._seed_results(table, _result(INTERNAL_RULE, score=100), _result(ISMS_RULE, score=0))

        store.get_report(customer_id=CUSTOMER, assessment_id=ASSESSMENT)

        self.assertEqual([catalog.customer_id for catalog in catalogs], [CUSTOMER])
        self.assertEqual(catalogs[0].reads, [("profile-combined", "v1")])


if __name__ == "__main__":
    unittest.main()
