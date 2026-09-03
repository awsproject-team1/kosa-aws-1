"""The live M1 plan follows the execution planner, Rule by Rule (ADR-0023 §7·§8).

이전에는 live 경로가 모든 Rule에 IAC·AWS_ACTUAL·DRIFT 세 좌표를 하드코딩했다. Worker는
`EvaluationExecutionPlanner`로 IaC 전용 Rule에 IAC만 실행하므로, 그 Rule의 나머지 두 좌표는
영원히 채워지지 않아 coverage가 100%가 되지 않고 readiness가 null로 남았다. 계획도 같은 planner를
통과해야 한다.

MANUAL Rule은 Repository 단위 governance 좌표에서 MANUAL 관점 하나를 만든다.
"""

import unittest

from apps.backend.assessment.runtime import (
    _with_complete_evaluation_plan,
    _with_governance_work,
)
from apps.backend.assessment.worker import AssessmentResourceWork
from apps.backend.policy import NoApplicablePolicyRulesError, PolicyContext
from apps.backend.policy.control_catalog import (
    CONTROL_CATALOG_VERSION,
    GOVERNANCE_ASSESSMENT_RESOURCE_TYPE,
    MANUAL_CONTROL_KEY,
)
from packages.contracts import (
    AssessmentPhase,
    EvaluationPerspective,
    PlannedEvaluation,
    PolicyRule,
    RuleEvaluationType,
    RuleSeverity,
    SourceReference,
)

REPOSITORY = "repo-001"
BUCKET = "bucket-001"
S3 = "AWS::S3::Bucket"
REFERENCE = SourceReference(
    source_id="policy", source_version="v1", locator="5.1-B", content_sha256="digest"
)


def _rule(rule_id: str, **overrides: object) -> PolicyRule:
    values: dict[str, object] = {
        "rule_id": rule_id,
        "version": "2026-09-03",
        "title": rule_id,
        "severity": RuleSeverity.HIGH,
        "applicable_phases": (AssessmentPhase.INITIAL, AssessmentPhase.POST_DEPLOY_VERIFICATION),
        "resource_types": (S3,),
        "source_references": (REFERENCE,),
    }
    values.update(overrides)
    return PolicyRule(**values)  # type: ignore[arg-type]


LEGACY = _rule("S3-PUBLIC-001")
IAC_ONLY = _rule(
    "S3-POLICY-AUTHORED",
    control_key="S3_BUCKET_POLICY_RESTRICTED",
    control_catalog_version=CONTROL_CATALOG_VERSION,
    evaluation_type=RuleEvaluationType.IAC,
    required_evidence=("S3.IAC_BUCKET_POLICY",),
    evaluation_rubric="FAIL when the bucket policy allows Principal *.",
)
MANUAL = _rule(
    "ORG-REVIEW-001",
    resource_types=(GOVERNANCE_ASSESSMENT_RESOURCE_TYPE,),
    control_key=MANUAL_CONTROL_KEY,
    control_catalog_version=CONTROL_CATALOG_VERSION,
    evaluation_type=RuleEvaluationType.MANUAL,
)


class Resolver:
    def __init__(self, rules_by_type: dict[str, tuple[PolicyRule, ...]]) -> None:
        self.rules_by_type = rules_by_type

    def resolve(self, *, policy_profile_id, phase, resource_type, expected_profile_version=None):
        rules = self.rules_by_type.get(resource_type, ())
        if not rules:
            raise NoApplicablePolicyRulesError("no applicable policy rules")
        return PolicyContext(
            policy_profile_id=policy_profile_id,
            policy_profile_version=expected_profile_version or "v1",
            phase=phase,
            resource_type=resource_type,
            rules=rules,
        )


def _work(**overrides: object) -> AssessmentResourceWork:
    values: dict[str, object] = {
        "customer_id": "cust-001",
        "assessment_id": "asm-001",
        "job_id": "job-001",
        "revision": 0,
        "policy_profile_id": "profile-customer",
        "phase": AssessmentPhase.INITIAL,
        "resource_id": BUCKET,
        "resource_type": S3,
        "perspective": EvaluationPerspective.AWS_ACTUAL,
        "model_profile_id": "assessment-nova-lite-m1-v2",
        "expected_profile_version": "v1",
        "assessed_commit_sha": "a" * 40,
    }
    values.update(overrides)
    return AssessmentResourceWork(**values)  # type: ignore[arg-type]


def _coordinates(works) -> set[PlannedEvaluation]:
    plans = {work.planned_coordinates for work in works}
    assert len(plans) == 1, "every work shares the one immutable plan"
    return set(plans.pop())


class LivePlanFollowsThePlannerTest(unittest.TestCase):
    def test_a_legacy_rule_still_plans_all_three_perspectives(self) -> None:
        works = _with_complete_evaluation_plan((_work(),), Resolver({S3: (LEGACY,)}))
        self.assertEqual(
            _coordinates(works),
            {
                PlannedEvaluation(resource_id=BUCKET, rule_id="S3-PUBLIC-001", perspective=p)
                for p in (
                    EvaluationPerspective.IAC,
                    EvaluationPerspective.AWS_ACTUAL,
                    EvaluationPerspective.DRIFT,
                )
            },
        )

    def test_an_iac_only_rule_plans_only_the_iac_coordinate(self) -> None:
        """AWS_ACTUAL/DRIFT 좌표를 계획하면 Worker가 채우지 않아 coverage가 영원히 미완이다."""
        works = _with_complete_evaluation_plan((_work(),), Resolver({S3: (LEGACY, IAC_ONLY)}))
        authored = {c for c in _coordinates(works) if c.rule_id == IAC_ONLY.rule_id}
        self.assertEqual(
            authored,
            {
                PlannedEvaluation(
                    resource_id=BUCKET,
                    rule_id=IAC_ONLY.rule_id,
                    perspective=EvaluationPerspective.IAC,
                )
            },
        )

    def test_a_manual_rule_plans_the_governance_coordinate_only(self) -> None:
        resolver = Resolver({S3: (LEGACY,), GOVERNANCE_ASSESSMENT_RESOURCE_TYPE: (MANUAL,)})
        works = _with_governance_work((_work(),), resolver, repository_id=REPOSITORY)
        works = _with_complete_evaluation_plan(works, resolver)
        manual = {c for c in _coordinates(works) if c.rule_id == MANUAL.rule_id}
        self.assertEqual(
            manual,
            {
                PlannedEvaluation(
                    resource_id=f"governance:{REPOSITORY}",
                    rule_id=MANUAL.rule_id,
                    perspective=EvaluationPerspective.MANUAL,
                )
            },
        )


class GovernanceWorkTest(unittest.TestCase):
    def test_no_manual_rule_means_no_governance_work(self) -> None:
        works = _with_governance_work(
            (_work(),), Resolver({S3: (LEGACY,)}), repository_id=REPOSITORY
        )
        self.assertEqual([work.resource_type for work in works], [S3])

    def test_a_manual_rule_adds_one_repository_level_work_item(self) -> None:
        works = _with_governance_work(
            (_work(),),
            Resolver({S3: (LEGACY,), GOVERNANCE_ASSESSMENT_RESOURCE_TYPE: (MANUAL,)}),
            repository_id=REPOSITORY,
        )
        self.assertEqual(len(works), 2)
        governance = works[-1]
        self.assertEqual(governance.resource_type, GOVERNANCE_ASSESSMENT_RESOURCE_TYPE)
        self.assertEqual(governance.resource_id, f"governance:{REPOSITORY}")
        self.assertIs(governance.perspective, EvaluationPerspective.MANUAL)
        # 같은 Assessment·Job·Profile 판본을 공유한다.
        self.assertEqual(governance.assessment_id, works[0].assessment_id)
        self.assertEqual(governance.expected_profile_version, works[0].expected_profile_version)

    def test_a_verification_adds_governance_work_only_when_the_source_plan_has_it(self) -> None:
        planned_with = (
            PlannedEvaluation(
                resource_id=BUCKET, rule_id="S3-PUBLIC-001", perspective=EvaluationPerspective.IAC
            ),
            PlannedEvaluation(
                resource_id=f"governance:{REPOSITORY}",
                rule_id=MANUAL.rule_id,
                perspective=EvaluationPerspective.MANUAL,
            ),
        )
        verification = _work(
            phase=AssessmentPhase.POST_DEPLOY_VERIFICATION, planned_coordinates=planned_with
        )
        # 검증은 원 계획을 재사용하므로 resolve하지 않는다 — resolver가 아무것도 모르더라도.
        works = _with_governance_work((verification,), Resolver({}), repository_id=REPOSITORY)
        self.assertEqual(len(works), 2)

        without = _work(
            phase=AssessmentPhase.POST_DEPLOY_VERIFICATION, planned_coordinates=planned_with[:1]
        )
        works = _with_governance_work(
            (without,),
            Resolver({GOVERNANCE_ASSESSMENT_RESOURCE_TYPE: (MANUAL,)}),
            repository_id=REPOSITORY,
        )
        self.assertEqual(len(works), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
