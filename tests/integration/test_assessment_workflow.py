"""M0 fixture integration: Assessment API creation through C result persistence."""

import json
import unittest
from pathlib import Path

from apps.backend.api.jobs import AssessmentRequest, JobApiService
from apps.backend.assessment import (
    AssessmentResourceWork,
    AssessmentRunner,
    AssessmentWorker,
    BedrockStructuredEvaluator,
    DynamoDbAssessmentReportStore,
    DynamoDbEvaluationResultStore,
    InMemoryModelProfileRegistry,
)
from apps.backend.auth import Principal, Role
from apps.backend.jobs import OutboxDispatcher, OutboxStatus, WorkflowOutboxEntry
from apps.backend.policy import (
    PolicyContext,
    PolicyContextResolver,
    load_m0_fixture_catalog,
    load_rule_registry,
)
from packages.contracts import (
    AssessmentPhase,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    ModelProfile,
    ModelProfileRole,
    PolicyRule,
)

FIXTURE_PATH = Path(__file__).parents[2] / "fixtures" / "m0" / "policy_profile.json"
SNAPSHOT_PATH = Path(__file__).parents[2] / "fixtures" / "m0" / "s3_resource_snapshot.json"
RULE_REGISTRY_PATH = Path(__file__).parents[2] / "fixtures" / "rules"

MODEL_PROFILE = ModelProfile(
    model_profile_id="assessment-nova-lite-m0-v1",
    role=ModelProfileRole.ASSESSMENT,
    region="us-east-1",
    model_id="amazon.nova-lite-v1:0",
    prompt_version="assessment-s3-v1",
    rubric_version="mvp-v1",
    golden_dataset_version="m0-s3-v1",
)


class ApprovedScope:
    def authorize(
        self, principal: Principal, *, repository_id: str, policy_profile_id: str
    ) -> None:
        return None


class WorkflowRepository:
    def __init__(self) -> None:
        self.jobs = {}
        self.outbox: list[WorkflowOutboxEntry] = []

    def create_assessment_workflow(self, assessment, job, outbox) -> None:
        self.jobs[(job.customer_id, job.job_id)] = job
        self.outbox.append(outbox)

    def get_job(self, customer_id: str, job_id: str):
        return self.jobs.get((customer_id, job_id))

    def list_pending_outbox(self, *, limit: int) -> tuple[WorkflowOutboxEntry, ...]:
        return tuple(entry for entry in self.outbox if entry.status is OutboxStatus.PENDING)[:limit]

    def mark_outbox_dispatched(self, entry: WorkflowOutboxEntry) -> None:
        self.outbox.remove(entry)

    def record_outbox_dispatch_failure(self, entry: WorkflowOutboxEntry) -> None:
        raise AssertionError("fixture queue dispatch must not fail")


class Queue:
    def __init__(self) -> None:
        self.tasks = []

    def dispatch(self, task) -> None:
        self.tasks.append(task)


class WorkRepository:
    def __init__(self, snapshot: dict[str, object]) -> None:
        self.snapshot = snapshot

    def get_resource_work(self, *, job_id: str, expected_revision: int):
        return AssessmentResourceWork(
            customer_id="cust-001",
            assessment_id="asm-001",
            job_id=job_id,
            revision=expected_revision,
            policy_profile_id="profile-mvp-baseline",
            phase=AssessmentPhase.INITIAL,
            resource_id=self.snapshot["resource_id"],
            resource_type=self.snapshot["resource_type"],
            perspective=EvaluationPerspective(self.snapshot["perspective"]),
            model_profile_id=MODEL_PROFILE.model_profile_id,
        )


class Evaluator:
    def __init__(self, snapshot: dict[str, object]) -> None:
        self.snapshot = snapshot

    def evaluate(
        self,
        *,
        resource_id: str,
        rule: PolicyRule,
        context: PolicyContext,
        model_profile: ModelProfile,
    ) -> EvaluationResult:
        public_access_block = self.snapshot["public_access_block"]
        assert isinstance(public_access_block, dict)
        is_compliant = all(public_access_block.values())
        return EvaluationResult(
            resource_id=resource_id,
            rule_id=rule.rule_id,
            perspective=EvaluationPerspective.IAC,
            status=EvaluationStatus.PASS if is_compliant else EvaluationStatus.FAIL,
            severity=rule.severity.value,
            score=100 if is_compliant else 20,
            rationale="Fixture S3 public-access-block state was evaluated deterministically",
            evidence_references=tuple(self.snapshot["evidence_references"]),
            rule_version=rule.version,
            rubric_version="mvp-v1",
            model_profile_id=model_profile.model_profile_id,
        )


class Table:
    def __init__(self) -> None:
        self.items = {}

    def put_item(self, **kwargs: object) -> None:
        item = kwargs["Item"]
        assert isinstance(item, dict)
        self.items[(item["PK"], item["SK"])] = item

    def get_item(self, **kwargs: object) -> dict[str, object]:
        key = kwargs["Key"]
        assert isinstance(key, dict)
        item = self.items.get((key["PK"], key["SK"]))
        return {} if item is None else {"Item": item}

    def query(self, **kwargs: object) -> dict[str, object]:
        values = kwargs["ExpressionAttributeValues"]
        assert isinstance(values, dict)
        customer = values[":customer"]
        prefix = values[":assessment"]
        return {
            "Items": [
                item
                for (pk, sk), item in self.items.items()
                if pk == customer and sk.startswith(prefix)
            ]
        }


class BedrockClient:
    """Deterministic M1 stand-in for the injected regional Bedrock client."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def converse(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": json.dumps(
                                {
                                    "status": "FAIL",
                                    "score": 20,
                                    "rationale": "Fixture confirms public access block is disabled.",
                                    "evidence_references": [
                                        "terraform:aws_s3_bucket_public_access_block"
                                    ],
                                }
                            )
                        }
                    ]
                }
            }
        }


class AssessmentWorkflowIntegrationTest(unittest.TestCase):
    def test_api_outbox_and_worker_persist_a_fixture_result(self) -> None:
        repository = WorkflowRepository()
        queue = Queue()
        service = JobApiService(
            repository=repository,
            assessment_scope=ApprovedScope(),
            outbox_dispatcher=OutboxDispatcher(repository=repository, dispatcher=queue),
            job_id_factory=lambda: "job-001",
            assessment_id_factory=lambda: "asm-001",
        )
        principal = Principal(
            subject="user-001",
            client_id="client-001",
            customer_id="cust-001",
            roles=frozenset({Role.USER}),
        )
        response = service.create_assessment(
            principal,
            AssessmentRequest(repository_id="repo-001", policy_profile_id="profile-mvp-baseline"),
        )
        # API dispatches immediately; the sweeper remains a recovery path only.

        _, catalog = load_m0_fixture_catalog(FIXTURE_PATH)
        snapshot = json.loads(SNAPSHOT_PATH.read_text())
        table = Table()
        outcomes = AssessmentWorker(
            work_repository=WorkRepository(snapshot),
            context_resolver=PolicyContextResolver(catalog),
            runner=AssessmentRunner(Evaluator(snapshot)),
            model_profiles=InMemoryModelProfileRegistry((MODEL_PROFILE,)),
            result_store=DynamoDbEvaluationResultStore(table),
        ).handle(queue.tasks[0])

        self.assertEqual(response.assessment_id, "asm-001")
        self.assertEqual(outcomes[0].status, EvaluationStatus.FAIL)
        self.assertIn(
            (
                "CUSTOMER#cust-001",
                "ASSESSMENT#asm-001#RESULT#bucket-public-001#RULE#S3-PUBLIC-001#PERSPECTIVE#IAC",
            ),
            table.items,
        )

    def test_m1_bedrock_adapter_runs_through_the_existing_worker_with_fixture_evidence(
        self,
    ) -> None:
        repository = WorkflowRepository()
        queue = Queue()
        service = JobApiService(
            repository=repository,
            assessment_scope=ApprovedScope(),
            outbox_dispatcher=OutboxDispatcher(repository=repository, dispatcher=queue),
            job_id_factory=lambda: "job-001",
            assessment_id_factory=lambda: "asm-001",
        )
        service.create_assessment(
            Principal(
                subject="user-001",
                client_id="client-001",
                customer_id="cust-001",
                roles=frozenset({Role.USER}),
            ),
            AssessmentRequest(repository_id="repo-001", policy_profile_id="profile-mvp-baseline"),
        )
        registry = load_rule_registry(RULE_REGISTRY_PATH)
        snapshot = json.loads(SNAPSHOT_PATH.read_text())
        evidence = snapshot["evidence_references"]
        assert isinstance(evidence, list) and all(isinstance(item, str) for item in evidence)
        client = BedrockClient()
        table = Table()
        report_store = DynamoDbAssessmentReportStore(table)

        outcomes = AssessmentWorker(
            work_repository=WorkRepository(snapshot),
            context_resolver=PolicyContextResolver(registry.catalog),
            runner=AssessmentRunner(
                BedrockStructuredEvaluator(
                    client=client,
                    perspective=EvaluationPerspective.IAC,
                    resource_document=snapshot,
                    evidence_references=tuple(evidence),
                )
            ),
            model_profiles=InMemoryModelProfileRegistry((MODEL_PROFILE,)),
            result_store=DynamoDbEvaluationResultStore(table),
            plan_store=report_store,
        ).handle(queue.tasks[0])
        report = report_store.get_report(customer_id="cust-001", assessment_id="asm-001")

        self.assertEqual(len(outcomes), 6)
        self.assertTrue(all(outcome.status is EvaluationStatus.FAIL for outcome in outcomes))
        self.assertEqual(client.calls[0]["modelId"], MODEL_PROFILE.model_id)
        self.assertEqual(report.coverage.percentage, 100)
        self.assertEqual(len(report.findings), 6)
        self.assertIsNotNone(report.readiness_score)
        assert report.readiness_score is not None
        self.assertEqual(report.readiness_score.score, 20)


class PerspectiveBedrockClient:
    """Return one fixed decision per perspective so DRIFT is deterministic."""

    def __init__(self, *, iac_status: str, actual_status: str) -> None:
        self.decisions = {
            EvaluationPerspective.IAC.value: (iac_status, "terraform:public-access-block"),
            EvaluationPerspective.AWS_ACTUAL.value: (actual_status, "aws:s3:public-access-block"),
        }
        self.calls: list[dict[str, object]] = []

    def converse(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        messages = kwargs["messages"]
        assert isinstance(messages, list)
        request = json.loads(messages[0]["content"][0]["text"])
        status, evidence = self.decisions[request["perspective"]]
        return {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": json.dumps(
                                {
                                    "status": status,
                                    "score": 100 if status == "PASS" else 20,
                                    "rationale": f"Fixture decision for {request['perspective']}.",
                                    "evidence_references": [evidence],
                                }
                            )
                        }
                    ]
                }
            }
        }


class InitialAssessmentPerspectiveIntegrationTest(unittest.TestCase):
    """M1 exit criteria: one Assessment yields IAC, AWS_ACTUAL, DRIFT, Findings, Readiness."""

    def run_assessment(
        self, *, iac_status: str, actual_status: str
    ) -> tuple[object, tuple[EvaluationResult, ...]]:
        repository, queue = WorkflowRepository(), Queue()
        service = JobApiService(
            repository=repository,
            assessment_scope=ApprovedScope(),
            outbox_dispatcher=OutboxDispatcher(repository=repository, dispatcher=queue),
            job_id_factory=lambda: "job-001",
            assessment_id_factory=lambda: "asm-001",
        )
        service.create_assessment(
            Principal(
                subject="user-001",
                client_id="client-001",
                customer_id="cust-001",
                roles=frozenset({Role.USER}),
            ),
            AssessmentRequest(repository_id="repo-001", policy_profile_id="profile-mvp-baseline"),
        )
        registry = load_rule_registry(RULE_REGISTRY_PATH)
        snapshot = json.loads(SNAPSHOT_PATH.read_text())
        client = PerspectiveBedrockClient(iac_status=iac_status, actual_status=actual_status)
        table = Table()
        report_store = DynamoDbAssessmentReportStore(table)
        outcomes = AssessmentWorker(
            work_repository=WorkRepository(snapshot),
            context_resolver=PolicyContextResolver(registry.catalog),
            perspective_runners={
                perspective: AssessmentRunner(
                    BedrockStructuredEvaluator(
                        client=client,
                        perspective=perspective,
                        resource_document=snapshot,
                        evidence_references=(evidence,),
                    )
                )
                for perspective, evidence in (
                    (EvaluationPerspective.IAC, "terraform:public-access-block"),
                    (EvaluationPerspective.AWS_ACTUAL, "aws:s3:public-access-block"),
                )
            },
            derive_drift=True,
            model_profiles=InMemoryModelProfileRegistry((MODEL_PROFILE,)),
            result_store=DynamoDbEvaluationResultStore(table),
            plan_store=report_store,
        ).handle(queue.tasks[0])
        return (
            report_store.get_report(customer_id="cust-001", assessment_id="asm-001"),
            outcomes,
        )

    def test_drifted_resource_reports_all_three_perspectives_with_full_coverage(self) -> None:
        report, outcomes = self.run_assessment(iac_status="PASS", actual_status="FAIL")

        # 6 approved S3 rules × (IAC, AWS_ACTUAL, DRIFT)
        self.assertEqual(len(outcomes), 18)
        self.assertEqual(report.coverage.planned_evaluations, 18)
        self.assertEqual(report.coverage.percentage, 100)
        self.assertEqual(
            {result.perspective for result in report.results},
            {
                EvaluationPerspective.IAC,
                EvaluationPerspective.AWS_ACTUAL,
                EvaluationPerspective.DRIFT,
            },
        )

    def test_findings_cover_the_failing_actual_and_the_derived_drift(self) -> None:
        report, _ = self.run_assessment(iac_status="PASS", actual_status="FAIL")

        perspectives = sorted(finding.perspective.value for finding in report.findings)
        self.assertEqual(perspectives, ["AWS_ACTUAL"] * 6 + ["DRIFT"] * 6)
        self.assertTrue(
            all(
                finding.evidence_references
                == ("terraform:public-access-block", "aws:s3:public-access-block")
                for finding in report.findings
                if finding.perspective is EvaluationPerspective.DRIFT
            )
        )

    def test_readiness_excludes_drift_alignment_from_the_representative_score(self) -> None:
        report, _ = self.run_assessment(iac_status="PASS", actual_status="FAIL")

        assert report.readiness_score is not None
        # Only the 12 IAC/AWS_ACTUAL results score: severity-weighted mean of 100 and 20.
        self.assertEqual(report.readiness_score.evaluated_evaluations, 12)
        self.assertEqual(report.readiness_score.score, 60)

    def test_aligned_resource_reports_no_drift_finding(self) -> None:
        report, _ = self.run_assessment(iac_status="FAIL", actual_status="FAIL")

        self.assertEqual(
            {finding.perspective for finding in report.findings},
            {EvaluationPerspective.IAC, EvaluationPerspective.AWS_ACTUAL},
        )
        self.assertEqual(report.coverage.percentage, 100)
        assert report.readiness_score is not None
        self.assertEqual(report.readiness_score.score, 20)
