"""apply 확정 뒤 검증 Assessment를 시작하는 A 경계 테스트 (ADR-0020 §1·§2·§3·§7).

고정하는 불변식:
- 검증 Assessment는 **새 id**이고 원 Assessment의 Profile 판본·Model Profile·rubric을 pin한다.
- Deployment Job은 같은 revision에서만 다음 revision으로 올라가고, write-once `assessment_id`가
  검증 Assessment를 가리키며, `ASSESS_RESOURCE` task가 그 revision으로 발행된다.
- 같은 apply 완료의 재전달은 새 Assessment를 만들지 않는다.
- Profile 판본이 바뀌었으면 검증하지 않고 사람에게 남긴다(다른 allow-list로 재평가하지 않는다).
"""

import unittest
from dataclasses import replace

from apps.backend.assessment.verification import VerificationSource
from apps.backend.deployment.record import DeploymentRecord
from apps.backend.deployment.verification import (
    PostDeployVerificationError,
    PostDeployVerificationService,
)
from apps.backend.jobs.models import Job
from apps.backend.jobs.outbox import OutboxDispatcher
from apps.backend.policy import NoApplicablePolicyRulesError, PolicyContext
from apps.backend.policy.control_catalog import (
    CONTROL_CATALOG_VERSION,
    GOVERNANCE_ASSESSMENT_RESOURCE_TYPE,
    MANUAL_CONTROL_KEY,
)
from packages.common.errors import DuplicateJobError
from packages.contracts import (
    AssessmentPhase,
    EvaluationPerspective,
    JobCurrentStep,
    JobStatus,
    PlannedEvaluation,
    PolicyRule,
    RuleEvaluationType,
    RuleSeverity,
    SourceReference,
    WorkflowCommand,
)
from tests.unit.test_deployment_worker import (
    CUSTOMER_ID,
    DEPLOYMENT_ID,
    JOB_ID,
    REPOSITORY_ID,
    approved_work,
    run_reference,
)

SOURCE_ASSESSMENT = "asm-source"
PROFILE_ID = "profile-mvp-baseline"
PROFILE_VERSION = "v2"
REFERENCE = SourceReference(
    source_id="isms-p", source_version="2023-10-31", locator="5.2.1", content_sha256="digest"
)


def _rule(rule_id: str, **overrides: object) -> PolicyRule:
    values: dict[str, object] = {
        "rule_id": rule_id,
        "version": "2026-08-31",
        "title": f"{rule_id} title",
        "severity": RuleSeverity.HIGH,
        "applicable_phases": (
            AssessmentPhase.INITIAL,
            AssessmentPhase.POST_DEPLOY_VERIFICATION,
        ),
        "resource_types": ("AWS::S3::Bucket",),
        "source_references": (REFERENCE,),
    }
    values.update(overrides)
    return PolicyRule(**values)  # type: ignore[arg-type]


MANUAL_RULE = _rule(
    "ORG-MANUAL-001",
    resource_types=(GOVERNANCE_ASSESSMENT_RESOURCE_TYPE,),
    control_key=MANUAL_CONTROL_KEY,
    control_catalog_version=CONTROL_CATALOG_VERSION,
    evaluation_type=RuleEvaluationType.MANUAL,
)

PLANNED = (
    PlannedEvaluation(
        resource_id="bucket-001", rule_id="S3-PUBLIC-001", perspective=EvaluationPerspective.IAC
    ),
    PlannedEvaluation(
        resource_id="bucket-001",
        rule_id="S3-PUBLIC-001",
        perspective=EvaluationPerspective.AWS_ACTUAL,
    ),
    PlannedEvaluation(
        resource_id="bucket-001", rule_id="S3-PUBLIC-001", perspective=EvaluationPerspective.DRIFT
    ),
)


def _record(verification_assessment_id: str | None = None) -> DeploymentRecord:
    return DeploymentRecord(
        deployment_id=DEPLOYMENT_ID,
        customer_id=CUSTOMER_ID,
        repository_id=REPOSITORY_ID,
        job_id=JOB_ID,
        remediation_id="rem-001",
        commit_sha="a" * 40,
        source_assessment_id=SOURCE_ASSESSMENT,
        verification_assessment_id=verification_assessment_id,
    )


def _job(revision: int = 1, assessment_id: str | None = None) -> Job:
    return Job(
        job_id=JOB_ID,
        customer_id=CUSTOMER_ID,
        job_type="DEPLOYMENT",
        status=JobStatus.RUNNING,
        current_step=JobCurrentStep.POST_DEPLOY_VERIFICATION,
        requested_by="subject-001",
        revision=revision,
        deployment_id=DEPLOYMENT_ID,
        assessment_id=assessment_id,
    )


def _source(**overrides: object) -> VerificationSource:
    values: dict[str, object] = {
        "assessment_id": SOURCE_ASSESSMENT,
        "customer_id": CUSTOMER_ID,
        "repository_id": REPOSITORY_ID,
        "policy_profile_id": PROFILE_ID,
        "policy_profile_version": PROFILE_VERSION,
        "model_profile_id": "assessment-nova-lite-m1-v2",
        "rubric_version": "m1-three-perspective-v1",
        "phase": AssessmentPhase.INITIAL,
        "planned_coordinates": PLANNED,
    }
    values.update(overrides)
    return VerificationSource(**values)  # type: ignore[arg-type]


class FakeDeployments:
    def __init__(self, record: DeploymentRecord | None) -> None:
        self.record = record

    def get_deployment(self, *, customer_id: str, deployment_id: str):
        return self.record


class FakeJobs:
    def __init__(self, job: Job | None) -> None:
        self.job = job

    def get_job(self, customer_id: str, job_id: str):
        return self.job


class FakeSources:
    def __init__(self, source: VerificationSource) -> None:
        self.source = source

    def get_verification_source(self, *, customer_id: str, assessment_id: str):
        assert assessment_id == SOURCE_ASSESSMENT
        return self.source


class FakeResolver:
    """Resolves the pinned Profile per resource type; governance only when asked to."""

    def __init__(
        self,
        *,
        version: str = PROFILE_VERSION,
        rules: tuple[PolicyRule, ...] = (_rule("S3-PUBLIC-001"),),
        governance_rules: tuple[PolicyRule, ...] = (),
    ) -> None:
        self.version = version
        self.rules = rules
        self.governance_rules = governance_rules
        self.calls: list[tuple[str, str | None]] = []

    def resolve(self, *, policy_profile_id, phase, resource_type, expected_profile_version=None):
        self.calls.append((resource_type, expected_profile_version))
        rules = (
            self.governance_rules
            if resource_type == GOVERNANCE_ASSESSMENT_RESOURCE_TYPE
            else self.rules
        )
        if not rules:
            raise NoApplicablePolicyRulesError("no applicable policy rules")
        return PolicyContext(
            policy_profile_id=policy_profile_id,
            policy_profile_version=self.version,
            phase=phase,
            resource_type=resource_type,
            rules=rules,
        )


class FakeStore:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    def create_verification_assessment(self, **kwargs: object) -> None:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error


class FakeOutboxRepository:
    def mark_outbox_dispatched(self, entry):
        return None

    def record_outbox_dispatch_failure(self, entry):
        return None

    def list_pending_outbox(self, *, limit):
        return ()


class FakeDispatcher:
    def __init__(self) -> None:
        self.tasks: list[object] = []

    def dispatch(self, task) -> None:
        self.tasks.append(task)


def _service(
    *,
    record: DeploymentRecord | None = None,
    job: Job | None = None,
    source: VerificationSource | None = None,
    resolver: FakeResolver | None = None,
    store: FakeStore | None = None,
    dispatcher: FakeDispatcher | None = None,
    deployments: FakeDeployments | None = None,
) -> tuple[PostDeployVerificationService, FakeStore, FakeDispatcher]:
    store = store or FakeStore()
    dispatcher = dispatcher or FakeDispatcher()
    service = PostDeployVerificationService(
        deployments=deployments or FakeDeployments(record if record is not None else _record()),
        jobs=FakeJobs(job if job is not None else _job()),
        sources=FakeSources(source or _source()),
        context_resolvers=lambda *, customer_id: resolver or FakeResolver(),
        resource_types_for=lambda customer_id, repository_id: ("AWS::S3::Bucket",),
        store=store,
        outbox_dispatcher=OutboxDispatcher(
            repository=FakeOutboxRepository(), dispatcher=dispatcher
        ),
        assessment_id_factory=lambda: "asm-verify-001",
    )
    return service, store, dispatcher


class StartVerificationTest(unittest.TestCase):
    def test_creates_a_pinned_verification_assessment_and_queues_its_task(self) -> None:
        service, store, dispatcher = _service()
        assessment_id = service.start_verification(
            work=approved_work(run_reference=run_reference())
        )

        self.assertEqual(assessment_id, "asm-verify-001")
        call = store.calls[0]
        assessment = call["assessment"]
        self.assertIs(assessment.phase, AssessmentPhase.POST_DEPLOY_VERIFICATION)
        self.assertEqual(assessment.source_assessment_id, SOURCE_ASSESSMENT)
        self.assertEqual(assessment.deployment_id, DEPLOYMENT_ID)
        self.assertEqual(assessment.job_id, JOB_ID)
        # 원 Assessment의 scope pin 3종을 그대로 갖는다 (§2·§3).
        self.assertEqual(assessment.policy_profile_version, PROFILE_VERSION)
        self.assertEqual(assessment.model_profile_id, "assessment-nova-lite-m1-v2")
        self.assertEqual(assessment.rubric_version, "m1-three-perspective-v1")
        # Job은 같은 revision에서 다음 revision으로 올라가고 검증 Assessment를 가리킨다 (§7).
        self.assertEqual(call["expected_revision"], 1)
        resumed = call["job"]
        self.assertEqual(resumed.revision, 2)
        self.assertEqual(resumed.assessment_id, "asm-verify-001")
        self.assertIs(resumed.current_step, JobCurrentStep.POST_DEPLOY_VERIFICATION)
        outbox = call["outbox"]
        self.assertIs(outbox.task.command, WorkflowCommand.ASSESS_RESOURCE)
        self.assertEqual(outbox.task.expected_revision, 2)
        self.assertEqual([task.job_id for task in dispatcher.tasks], [JOB_ID])

    def test_the_pinned_version_is_what_the_resolver_is_asked_for(self) -> None:
        resolver = FakeResolver()
        service, _, _ = _service(resolver=resolver)
        service.start_verification(work=approved_work(run_reference=run_reference()))
        self.assertIn(("AWS::S3::Bucket", PROFILE_VERSION), resolver.calls)
        # governance 좌표도 물어본다 — MANUAL Rule이 있으면 계획에 들어 있기 때문이다.
        self.assertIn((GOVERNANCE_ASSESSMENT_RESOURCE_TYPE, PROFILE_VERSION), resolver.calls)

    def test_a_manual_rule_in_the_source_plan_is_accepted_through_the_governance_context(
        self,
    ) -> None:
        planned = (
            *PLANNED,
            PlannedEvaluation(
                resource_id=f"governance:{REPOSITORY_ID}",
                rule_id=MANUAL_RULE.rule_id,
                perspective=EvaluationPerspective.MANUAL,
            ),
        )
        service, store, _ = _service(
            source=_source(planned_coordinates=planned),
            resolver=FakeResolver(governance_rules=(MANUAL_RULE,)),
        )
        service.start_verification(work=approved_work(run_reference=run_reference()))
        self.assertEqual(len(store.calls), 1)

    def test_a_planned_rule_missing_from_the_verification_phase_is_refused(self) -> None:
        service, store, _ = _service(resolver=FakeResolver(rules=(_rule("S3-OTHER-001"),)))
        with self.assertRaisesRegex(PostDeployVerificationError, "PLANNED_RULE_NOT_APPLICABLE"):
            service.start_verification(work=approved_work(run_reference=run_reference()))
        self.assertEqual(store.calls, [])


class IdempotencyTest(unittest.TestCase):
    def test_a_redelivered_completion_returns_the_existing_assessment(self) -> None:
        service, store, dispatcher = _service(record=_record("asm-verify-existing"))
        assessment_id = service.start_verification(
            work=approved_work(run_reference=run_reference())
        )
        self.assertEqual(assessment_id, "asm-verify-existing")
        self.assertEqual(store.calls, [])
        self.assertEqual(dispatcher.tasks, [])

    def test_a_concurrent_winner_is_read_back_after_a_write_conflict(self) -> None:
        class Deployments:
            def __init__(self) -> None:
                self.reads = 0

            def get_deployment(self, *, customer_id: str, deployment_id: str):
                self.reads += 1
                return _record(None if self.reads == 1 else "asm-verify-winner")

        service, _, dispatcher = _service(
            deployments=Deployments(), store=FakeStore(error=DuplicateJobError("started"))
        )
        assessment_id = service.start_verification(
            work=approved_work(run_reference=run_reference())
        )
        self.assertEqual(assessment_id, "asm-verify-winner")
        self.assertEqual(dispatcher.tasks, [])


class FailClosedTest(unittest.TestCase):
    def test_a_moved_job_revision_is_not_a_retry(self) -> None:
        service, store, _ = _service(job=_job(revision=5))
        with self.assertRaisesRegex(PostDeployVerificationError, "revision moved"):
            service.start_verification(work=approved_work(run_reference=run_reference()))
        self.assertEqual(store.calls, [])

    def test_a_replaced_profile_version_is_not_verified_silently(self) -> None:
        service, store, _ = _service(resolver=FakeResolver(version="v3"))
        with self.assertRaisesRegex(PostDeployVerificationError, "POLICY_PROFILE_VERSION_REPLACED"):
            service.start_verification(work=approved_work(run_reference=run_reference()))
        self.assertEqual(store.calls, [])

    def test_a_missing_deployment_fails(self) -> None:
        service, _, _ = _service(deployments=FakeDeployments(None))
        with self.assertRaisesRegex(PostDeployVerificationError, "deployment not found"):
            service.start_verification(work=approved_work(run_reference=run_reference()))

    def test_a_source_outside_the_deployment_scope_fails(self) -> None:
        service, _, _ = _service(source=_source(repository_id="repo-other"))
        with self.assertRaisesRegex(PostDeployVerificationError, "outside the deployment scope"):
            service.start_verification(work=approved_work(run_reference=run_reference()))

    def test_a_terminal_job_is_not_resumed(self) -> None:
        service, _, _ = _service(job=replace(_job(), status=JobStatus.CANCELLED))
        with self.assertRaisesRegex(PostDeployVerificationError, "already terminal"):
            service.start_verification(work=approved_work(run_reference=run_reference()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
