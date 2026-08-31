"""M0 fixture integration: Assessment API creation through C result persistence."""

import json
import unittest
from pathlib import Path

from apps.backend.api.jobs import AssessmentRequest, JobApiService
from apps.backend.assessment import (
    AssessmentResourceWork,
    AssessmentRunner,
    AssessmentWorker,
    DynamoDbEvaluationResultStore,
    InMemoryModelProfileRegistry,
)
from apps.backend.auth import Principal, Role
from apps.backend.jobs import OutboxDispatcher, OutboxStatus, WorkflowOutboxEntry
from apps.backend.policy import PolicyContext, PolicyContextResolver, load_m0_fixture_catalog
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


class AssessmentWorkflowIntegrationTest(unittest.TestCase):
    def test_api_outbox_and_worker_persist_a_fixture_result(self) -> None:
        repository = WorkflowRepository()
        service = JobApiService(
            repository=repository,
            assessment_scope=ApprovedScope(),
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
        queue = Queue()
        self.assertEqual(
            OutboxDispatcher(repository=repository, dispatcher=queue).dispatch_pending(), 1
        )

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
