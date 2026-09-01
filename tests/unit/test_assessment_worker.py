"""Unit tests for the C Assessment worker application boundary."""

import unittest

from apps.backend.assessment import (
    AssessmentResourceWork,
    AssessmentRunner,
    AssessmentWorker,
    AssessmentWorkNotFoundError,
    InMemoryModelProfileRegistry,
)
from apps.backend.policy import PolicyContext, PolicyContextResolver
from packages.contracts import (
    AssessmentPhase,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    ModelProfile,
    ModelProfileRole,
    PolicyProfile,
    PolicyRule,
    PolicyRuleReference,
    RuleSeverity,
    SourceReference,
    WorkflowCommand,
    WorkflowTask,
)

MODEL_PROFILE = ModelProfile(
    model_profile_id="assessment-nova-lite-m0-v1",
    role=ModelProfileRole.ASSESSMENT,
    region="us-east-1",
    model_id="amazon.nova-lite-v1:0",
    prompt_version="assessment-s3-v1",
    rubric_version="mvp-v1",
    golden_dataset_version="m0-s3-v1",
)

RULE = PolicyRule(
    rule_id="S3-001",
    version="v1",
    title="S3 public access block",
    severity=RuleSeverity.HIGH,
    applicable_phases=(AssessmentPhase.INITIAL,),
    resource_types=("AWS::S3::Bucket",),
    source_references=(
        SourceReference(
            source_id="isms-p",
            source_version="2023-10-31",
            locator="5.2.1",
            content_sha256="digest",
        ),
    ),
)


class Catalog:
    def get_profile(self, policy_profile_id: str) -> PolicyProfile | None:
        return PolicyProfile(
            policy_profile_id="profile-001",
            version="v1",
            rule_references=(PolicyRuleReference(rule_id="S3-001", version="v1"),),
        )

    def get_rule(self, rule_id: str, version: str) -> PolicyRule | None:
        return RULE if (rule_id, version) == (RULE.rule_id, RULE.version) else None


class WorkRepository:
    def __init__(self, work: AssessmentResourceWork | None) -> None:
        self.work = work
        self.calls: list[tuple[str, int]] = []

    def get_resource_work(
        self, *, job_id: str, expected_revision: int
    ) -> AssessmentResourceWork | None:
        self.calls.append((job_id, expected_revision))
        return self.work


class ResultStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[EvaluationResult, ...]]] = []

    def put_if_absent(
        self,
        *,
        customer_id: str,
        assessment_id: str,
        results: tuple[EvaluationResult, ...],
    ) -> None:
        self.calls.append((customer_id, assessment_id, results))


class Evaluator:
    def __init__(self, *, perspective: EvaluationPerspective = EvaluationPerspective.IAC) -> None:
        self.perspective = perspective

    def evaluate(
        self,
        *,
        resource_id: str,
        rule: PolicyRule,
        context: PolicyContext,
        model_profile: ModelProfile,
    ) -> EvaluationResult:
        return EvaluationResult(
            resource_id=resource_id,
            rule_id=rule.rule_id,
            perspective=self.perspective,
            status=EvaluationStatus.PASS,
            severity=rule.severity.value,
            score=100,
            rationale="Public access is blocked",
            evidence_references=("terraform:public-access-block",),
            rule_version=rule.version,
            rubric_version="mvp-v1",
            model_profile_id=model_profile.model_profile_id,
        )


def work(*, revision: int = 2) -> AssessmentResourceWork:
    return AssessmentResourceWork(
        customer_id="cust-001",
        assessment_id="asm-001",
        job_id="job-001",
        revision=revision,
        policy_profile_id="profile-001",
        phase=AssessmentPhase.INITIAL,
        resource_id="bucket-001",
        resource_type="AWS::S3::Bucket",
        perspective=EvaluationPerspective.IAC,
        model_profile_id=MODEL_PROFILE.model_profile_id,
    )


class AssessmentWorkerTest(unittest.TestCase):
    def build_worker(
        self, *, work_value: AssessmentResourceWork | None, perspective: EvaluationPerspective
    ) -> tuple[AssessmentWorker, ResultStore]:
        store = ResultStore()
        return (
            AssessmentWorker(
                work_repository=WorkRepository(work_value),
                context_resolver=PolicyContextResolver(Catalog()),
                runner=AssessmentRunner(Evaluator(perspective=perspective)),
                model_profiles=InMemoryModelProfileRegistry((MODEL_PROFILE,)),
                result_store=store,
            ),
            store,
        )

    def test_reloads_authoritative_work_and_persists_validated_results(self) -> None:
        worker, store = self.build_worker(work_value=work(), perspective=EvaluationPerspective.IAC)

        outcomes = worker.handle(
            WorkflowTask(
                job_id="job-001", expected_revision=2, command=WorkflowCommand.ASSESS_RESOURCE
            )
        )

        self.assertEqual(outcomes[0].resource_id, "bucket-001")
        self.assertEqual(store.calls[0][0:2], ("cust-001", "asm-001"))

    def test_rejects_non_assessment_command_before_loading_work(self) -> None:
        worker, store = self.build_worker(work_value=work(), perspective=EvaluationPerspective.IAC)

        with self.assertRaisesRegex(ValueError, "only accepts"):
            worker.handle(
                WorkflowTask(
                    job_id="job-001",
                    expected_revision=2,
                    command=WorkflowCommand.GENERATE_REMEDIATION,
                )
            )
        self.assertEqual(store.calls, [])

    def test_rejects_missing_or_stale_authoritative_work(self) -> None:
        worker, _ = self.build_worker(work_value=None, perspective=EvaluationPerspective.IAC)

        with self.assertRaises(AssessmentWorkNotFoundError):
            worker.handle(
                WorkflowTask(
                    job_id="job-001", expected_revision=2, command=WorkflowCommand.ASSESS_RESOURCE
                )
            )

    def test_rejects_result_perspective_outside_work(self) -> None:
        worker, store = self.build_worker(
            work_value=work(), perspective=EvaluationPerspective.AWS_ACTUAL
        )

        with self.assertRaisesRegex(ValueError, "perspective"):
            worker.handle(
                WorkflowTask(
                    job_id="job-001", expected_revision=2, command=WorkflowCommand.ASSESS_RESOURCE
                )
            )
        self.assertEqual(store.calls, [])


class PlanStore:
    def __init__(self) -> None:
        self.plans: list[object] = []

    def put_plan_if_absent(self, plan: object) -> None:
        self.plans.append(plan)


class DecisionEvaluator:
    """Return a fixed status per perspective so DRIFT derivation is deterministic."""

    # Resource-state evidence namespaces the Policy Context allows per perspective.
    EVIDENCE_PREFIXES = {
        EvaluationPerspective.IAC: "terraform:",
        EvaluationPerspective.AWS_ACTUAL: "aws:",
    }

    def __init__(
        self, *, perspective: EvaluationPerspective, status: EvaluationStatus, score: float
    ) -> None:
        self.perspective = perspective
        self.status = status
        self.score = score

    def evaluate(
        self,
        *,
        resource_id: str,
        rule: PolicyRule,
        context: PolicyContext,
        model_profile: ModelProfile,
    ) -> EvaluationResult:
        return EvaluationResult(
            resource_id=resource_id,
            rule_id=rule.rule_id,
            perspective=self.perspective,
            status=self.status,
            severity=rule.severity.value,
            score=self.score,
            rationale="fixture",
            evidence_references=(f"{self.EVIDENCE_PREFIXES[self.perspective]}public-access-block",),
            rule_version=rule.version,
            rubric_version="mvp-v1",
            model_profile_id=model_profile.model_profile_id,
        )


class InitialAssessmentPerspectiveTest(unittest.TestCase):
    def build_worker(
        self,
        *,
        iac_status: EvaluationStatus,
        actual_status: EvaluationStatus,
        derive_drift: bool = True,
        work_value: AssessmentResourceWork | None = None,
    ) -> tuple[AssessmentWorker, ResultStore, PlanStore]:
        store, plans = ResultStore(), PlanStore()
        return (
            AssessmentWorker(
                work_repository=WorkRepository(work_value or work()),
                context_resolver=PolicyContextResolver(Catalog()),
                perspective_runners={
                    EvaluationPerspective.AWS_ACTUAL: AssessmentRunner(
                        DecisionEvaluator(
                            perspective=EvaluationPerspective.AWS_ACTUAL,
                            status=actual_status,
                            score=100 if actual_status is EvaluationStatus.PASS else 20,
                        )
                    ),
                    EvaluationPerspective.IAC: AssessmentRunner(
                        DecisionEvaluator(
                            perspective=EvaluationPerspective.IAC,
                            status=iac_status,
                            score=100 if iac_status is EvaluationStatus.PASS else 20,
                        )
                    ),
                },
                derive_drift=derive_drift,
                model_profiles=InMemoryModelProfileRegistry((MODEL_PROFILE,)),
                result_store=store,
                plan_store=plans,
            ),
            store,
            plans,
        )

    @staticmethod
    def task() -> WorkflowTask:
        return WorkflowTask(
            job_id="job-001", expected_revision=2, command=WorkflowCommand.ASSESS_RESOURCE
        )

    def test_produces_iac_actual_and_derived_drift_for_one_resource(self) -> None:
        worker, store, _ = self.build_worker(
            iac_status=EvaluationStatus.PASS, actual_status=EvaluationStatus.FAIL
        )

        outcomes = worker.handle(self.task())

        self.assertEqual(
            tuple(outcome.perspective for outcome in outcomes),
            (
                EvaluationPerspective.IAC,
                EvaluationPerspective.AWS_ACTUAL,
                EvaluationPerspective.DRIFT,
            ),
        )
        self.assertIs(outcomes[2].status, EvaluationStatus.FAIL)
        self.assertEqual(store.calls[0][2], outcomes)

    def test_plan_counts_every_perspective_so_coverage_is_not_task_ordered(self) -> None:
        worker, _, plans = self.build_worker(
            iac_status=EvaluationStatus.PASS, actual_status=EvaluationStatus.PASS
        )

        worker.handle(self.task())

        self.assertEqual(plans.plans[0].planned_evaluations, 3)

    def test_server_supplied_plan_overrides_the_derived_count(self) -> None:
        worker, _, plans = self.build_worker(
            iac_status=EvaluationStatus.PASS,
            actual_status=EvaluationStatus.PASS,
            work_value=AssessmentResourceWork(
                customer_id="cust-001",
                assessment_id="asm-001",
                job_id="job-001",
                revision=2,
                policy_profile_id="profile-001",
                phase=AssessmentPhase.INITIAL,
                resource_id="bucket-001",
                resource_type="AWS::S3::Bucket",
                perspective=EvaluationPerspective.IAC,
                model_profile_id=MODEL_PROFILE.model_profile_id,
                planned_evaluations=9,
            ),
        )

        worker.handle(self.task())

        self.assertEqual(plans.plans[0].planned_evaluations, 9)

    def test_drift_can_be_disabled_and_then_plans_only_evaluated_perspectives(self) -> None:
        worker, _, plans = self.build_worker(
            iac_status=EvaluationStatus.PASS,
            actual_status=EvaluationStatus.PASS,
            derive_drift=False,
        )

        outcomes = worker.handle(self.task())

        self.assertEqual(len(outcomes), 2)
        self.assertEqual(plans.plans[0].planned_evaluations, 2)

    def test_requires_exactly_one_of_runner_or_perspective_runners(self) -> None:
        for kwargs in (
            {},
            {
                "runner": AssessmentRunner(Evaluator()),
                "perspective_runners": {EvaluationPerspective.IAC: AssessmentRunner(Evaluator())},
            },
        ):
            with (
                self.subTest(kwargs=sorted(kwargs)),
                self.assertRaisesRegex(TypeError, "exactly one"),
            ):
                AssessmentWorker(
                    work_repository=WorkRepository(work()),
                    context_resolver=PolicyContextResolver(Catalog()),
                    model_profiles=InMemoryModelProfileRegistry((MODEL_PROFILE,)),
                    result_store=ResultStore(),
                    **kwargs,
                )

    def test_rejects_drift_as_an_evaluated_perspective(self) -> None:
        with self.assertRaisesRegex(ValueError, "DRIFT is derived"):
            AssessmentWorker(
                work_repository=WorkRepository(work()),
                context_resolver=PolicyContextResolver(Catalog()),
                model_profiles=InMemoryModelProfileRegistry((MODEL_PROFILE,)),
                result_store=ResultStore(),
                perspective_runners={EvaluationPerspective.DRIFT: AssessmentRunner(Evaluator())},
            )

    def test_rejects_drift_without_perspective_runners(self) -> None:
        with self.assertRaisesRegex(ValueError, "derive_drift requires"):
            AssessmentWorker(
                work_repository=WorkRepository(work()),
                context_resolver=PolicyContextResolver(Catalog()),
                model_profiles=InMemoryModelProfileRegistry((MODEL_PROFILE,)),
                result_store=ResultStore(),
                runner=AssessmentRunner(Evaluator()),
                derive_drift=True,
            )
