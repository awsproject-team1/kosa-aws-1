"""The whole path, end to end: an uploaded policy decides what gets evaluated.

    Upload → Normalize → Extraction request → Authoring Worker → Review
    → Partial approval → Approved Rule Registry → Profile publish
    → Assessment creation / Profile version pin → DynamoDB Catalog
    → execution plan → Evaluation → Finding

이 파일이 증명하려는 것은 하나다: **평가에 쓰이는 Rule은 고객이 업로드하고 사람이 승인한
정책에서 나온다.** 저장소에 커밋된 fixture Rule이 아니다. 그 사실이 깨지면 앞의 모든 단계가
결과에 아무 영향을 주지 않는 장식이 된다.

환경변수를 고치거나 fixture Rule을 손대지 않고 통과해야 한다.
"""

import unittest
from io import BytesIO
from pathlib import Path

from apps.backend.api.jobs import AssessmentRequest, JobApiService
from apps.backend.api.policy_approval import PolicyApprovalApiService
from apps.backend.api.policy_candidates import PolicyCandidateApiService
from apps.backend.assessment.execution_plan import EvaluationExecutionPlanner
from apps.backend.assessment.manual_review import (
    ManualReviewEvaluator,
    governance_resource_id,
)
from apps.backend.assessment.runner import AssessmentRunner
from apps.backend.auth import Principal, Role
from apps.backend.jobs import OutboxDispatcher, OutboxStatus, WorkflowOutboxEntry
from apps.backend.policy import DynamoDbPolicyCatalog, PolicyContextResolver
from apps.backend.policy.authoring import (
    FakePolicyCandidateExtractor,
    NormalizedArtifactReader,
    extract_policy_candidates,
)
from apps.backend.policy.control_catalog import (
    GOVERNANCE_ASSESSMENT_RESOURCE_TYPE,
    MVP_CONTROL_CATALOG,
)
from apps.backend.policy.registry import load_rule_registry
from apps.backend.repositories.policy_approval import DynamoDbPolicyApprovalRepository
from packages.contracts import (
    AssessmentPhase,
    AuthoringRunStatus,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    ModelProfile,
    ModelProfileRole,
    PolicyProfile,
    PolicyRuleReference,
    RuleEvaluationType,
    RuleSeverity,
    ScoringMode,
)
from tests.authoring_fixtures import UNIT_TEXTS, normalized_artifact_bytes, ready_document
from tests.unit.test_authoring_result_persistence import FakeTable, store_ingestion_item
from tests.unit.test_policy_authoring_pipeline import automatable, manual, unsupported

CUSTOMER = "cust-001"
REPOSITORY = "repo-001"
PROFILE_ID = "profile-customer-baseline"
BUCKET = "policy-artifacts"
RULES_PATH = Path(__file__).parents[2] / "fixtures" / "rules"
DOCUMENT = ready_document()

MODEL_PROFILE = ModelProfile(
    model_profile_id="assessment-v1",
    role=ModelProfileRole.ASSESSMENT,
    region="us-east-1",
    model_id="amazon.nova-lite-v1:0",
    prompt_version="assessment/1",
    rubric_version="rubric/1",
    golden_dataset_version="golden/1",
)


class _ArtifactSource:
    def get_object(self, **_kwargs: object) -> dict[str, object]:
        return {"Body": BytesIO(normalized_artifact_bytes())}


class _Queue:
    """The authoring queue. 요청을 모아 두었다가 worker 자리에서 처리한다."""

    def __init__(self) -> None:
        self.requests: list[object] = []

    def enqueue(self, request: object) -> None:
        self.requests.append(request)


class _WorkflowRepository:
    def __init__(self) -> None:
        self.assessments: list[object] = []
        self.outbox: list[WorkflowOutboxEntry] = []

    def create_assessment_workflow(self, assessment, job, outbox) -> None:
        self.assessments.append(assessment)
        self.outbox.append(outbox)

    def list_pending_outbox(self, *, limit: int) -> tuple[WorkflowOutboxEntry, ...]:
        return tuple(entry for entry in self.outbox if entry.status is OutboxStatus.PENDING)[:limit]

    def mark_outbox_dispatched(self, entry: WorkflowOutboxEntry) -> None:
        return None

    def record_outbox_dispatch_failure(self, entry: WorkflowOutboxEntry) -> None:
        raise AssertionError("integration queue dispatch must not fail")


class _Dispatcher:
    def dispatch(self, task) -> None:
        return None


class _ApprovedRepositories:
    def authorize(self, principal, *, repository_id: str) -> None:
        return None


class _RecordingEvaluator:
    """A stand-in Actual evaluator that fails one rule so a Finding is produced."""

    def __init__(self, *, failing_rule_ids: frozenset[str] = frozenset()) -> None:
        self.failing_rule_ids = failing_rule_ids
        self.seen_rule_ids: list[str] = []

    def evaluate(self, *, resource_id, rule, context, model_profile) -> EvaluationResult:
        self.seen_rule_ids.append(rule.rule_id)
        failed = rule.rule_id in self.failing_rule_ids
        return EvaluationResult(
            resource_id=resource_id,
            rule_id=rule.rule_id,
            perspective=EvaluationPerspective.AWS_ACTUAL,
            status=EvaluationStatus.FAIL if failed else EvaluationStatus.PASS,
            severity=rule.severity.value,
            score=10.0 if failed else 95.0,
            rationale="integration evaluation",
            evidence_references=(f"aws:s3:bucket/{resource_id}#read-resource",),
            rule_version=rule.version,
            rubric_version=model_profile.rubric_version,
            model_profile_id=model_profile.model_profile_id,
            scoring_mode=ScoringMode.CONTINUOUS,
        )


def _principal(customer_id: str = CUSTOMER) -> Principal:
    return Principal(
        subject="reviewer@example.com",
        client_id="frontend",
        customer_id=customer_id,
        roles=frozenset({Role.ADMIN}),
    )


def _repository(table: FakeTable) -> DynamoDbPolicyApprovalRepository:
    return DynamoDbPolicyApprovalRepository(
        table_name="governance",
        transaction_client=table,  # type: ignore[arg-type]
        table=table,  # type: ignore[arg-type]
    )


def _run_authoring_worker(table: FakeTable, request, *, requirements) -> AuthoringRunStatus:
    """Stand in for the Authoring Worker Lambda: read, extract, persist.

    가짜 Extractor는 주입된 결과만 돌려준다 — 정책 문장을 읽고 분기하면, 이 테스트가 통과하는
    이유가 "파이프라인이 옳다"가 아니라 "가짜가 그 문장을 알아봤다"가 된다.
    """
    result = extract_policy_candidates(
        customer_id=request.customer_id,
        document=DOCUMENT,
        artifact_reader=NormalizedArtifactReader(reader=_ArtifactSource(), bucket=BUCKET),  # type: ignore[arg-type]
        extractor=FakePolicyCandidateExtractor(requirements),
        catalog=MVP_CONTROL_CATALOG,
        authoring_run_id=request.authoring_run_id,
        requested_at=request.requested_at,
    )
    return (
        _repository(table)
        .record_authoring_result(customer_id=request.customer_id, result=result)
        .status
    )


class AuthoringToAssessmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.table = FakeTable()
        store_ingestion_item(self.table)
        self.queue = _Queue()
        self.candidates = PolicyCandidateApiService(
            repository=_repository(self.table),
            queue=self.queue,  # type: ignore[arg-type]
            run_id_factory=lambda: "authoring-run-1",
        )
        self.approvals = PolicyApprovalApiService(_repository(self.table))

    # ---- the path ----

    def _extract(self, requirements=None) -> None:
        if requirements is None:
            requirements = (automatable(), manual(), unsupported())
        accepted = self.candidates.request_extraction(
            _principal(), source_id=DOCUMENT.source_id, source_version=DOCUMENT.source_version
        )
        self.assertIs(accepted.status, AuthoringRunStatus.QUEUED)
        status = _run_authoring_worker(
            self.table, self.queue.requests[0], requirements=requirements
        )
        self.assertIs(status, AuthoringRunStatus.READY)

    def _review(self):
        return self.candidates.list_candidates(
            _principal(), source_id=DOCUMENT.source_id, source_version=DOCUMENT.source_version
        )

    def _approve(self, references: tuple[PolicyRuleReference, ...]):
        return self.approvals.approve(
            _principal(),
            source_id=DOCUMENT.source_id,
            source_version=DOCUMENT.source_version,
            approved_rules=references,
        )

    def _publish(self, references, *, version: str = "v1", expected: str | None = None):
        profile = PolicyProfile(
            policy_profile_id=PROFILE_ID, version=version, rule_references=references
        )
        _repository(self.table).record_profile(
            customer_id=CUSTOMER,
            profile=profile,
            published_by="admin@example.com",
            published_at="2026-09-03T00:00:00Z",
            expected_current_version=expected,
        )
        return profile

    def _create_assessment(self):
        workflow = _WorkflowRepository()
        service = JobApiService(
            repository=workflow,
            assessment_scope=_ApprovedRepositories(),
            policy_catalog_factory=lambda *, customer_id: DynamoDbPolicyCatalog(
                self.table, customer_id=customer_id
            ),
            outbox_dispatcher=OutboxDispatcher(repository=workflow, dispatcher=_Dispatcher()),
            job_id_factory=lambda: "job-001",
            assessment_id_factory=lambda: "asm-001",
        )
        service.create_assessment(
            _principal(),
            AssessmentRequest(repository_id=REPOSITORY, policy_profile_id=PROFILE_ID),
        )
        return workflow.assessments[0]

    # ---- tests ----

    def test_an_uploaded_policy_decides_what_the_assessment_evaluates(self) -> None:
        self._extract()
        page = self._review()

        # 리뷰어는 부분 승인한다: 자동 평가 가능한 것만 고르고 MANUAL은 이번엔 남겨 둔다.
        automatable_entries = [
            entry
            for entry in page.candidates
            if entry.evaluation_type is not RuleEvaluationType.MANUAL
        ]
        self.assertEqual(len(automatable_entries), 1)
        references = tuple(
            PolicyRuleReference(rule_id=entry.rule_id, version=entry.rule_version)
            for entry in automatable_entries
        )
        approval = self._approve(references)
        self.assertEqual(approval.approved_rules, references)

        profile = self._publish(references)
        assessment = self._create_assessment()
        self.assertEqual(assessment.policy_profile_version, profile.version)

        catalog = DynamoDbPolicyCatalog(self.table, customer_id=CUSTOMER)
        context = PolicyContextResolver(catalog).resolve(
            policy_profile_id=assessment.policy_profile_id,
            phase=AssessmentPhase.INITIAL,
            resource_type="AWS::S3::Bucket",
            expected_profile_version=assessment.policy_profile_version,
        )

        # **평가되는 Rule은 업로드한 정책에서 나온 것이다.**
        self.assertEqual(len(context.rules), 1)
        rule = context.rules[0]
        self.assertEqual(rule.control_key, "S3_BLOCK_PUBLIC_ACCESS")
        self.assertTrue(rule.rule_id.startswith("CUST-"))
        fixture_rule_ids = {entry.rule_id for entry in load_rule_registry(RULES_PATH).rules}
        self.assertNotIn(rule.rule_id, fixture_rule_ids)

        # 그 Rule이 AWS 전용이므로 AWS_ACTUAL만 계획된다.
        planner = EvaluationExecutionPlanner(
            available_perspectives=(EvaluationPerspective.IAC, EvaluationPerspective.AWS_ACTUAL),
            derive_drift=True,
        )
        self.assertEqual(planner.perspectives_for(rule), (EvaluationPerspective.AWS_ACTUAL,))
        self.assertEqual(planner.rules_for(EvaluationPerspective.IAC, context.rules), ())
        self.assertEqual(planner.drift_rules(context.rules), ())

        # 그리고 평가가 실제로 Finding을 낼 수 있다.
        evaluator = _RecordingEvaluator(failing_rule_ids=frozenset({rule.rule_id}))
        results = AssessmentRunner(evaluator).evaluate_resource(
            resource_id="customer-bucket", context=context, model_profile=MODEL_PROFILE
        )
        self.assertEqual(evaluator.seen_rule_ids, [rule.rule_id])
        self.assertEqual(len(results), 1)
        self.assertIs(results[0].status, EvaluationStatus.FAIL)
        self.assertEqual(results[0].severity, RuleSeverity.CRITICAL.value)

    def test_an_unapproved_candidate_never_reaches_the_runtime(self) -> None:
        """부분 승인의 요점이다. 고르지 않은 후보는 평가 경계 밖에 남는다."""
        self._extract()
        page = self._review()
        manual_entry = next(
            entry for entry in page.candidates if entry.evaluation_type is RuleEvaluationType.MANUAL
        )
        automatable_entry = next(
            entry
            for entry in page.candidates
            if entry.evaluation_type is not RuleEvaluationType.MANUAL
        )
        approved = (
            PolicyRuleReference(
                rule_id=automatable_entry.rule_id, version=automatable_entry.rule_version
            ),
        )
        self._approve(approved)

        catalog = DynamoDbPolicyCatalog(self.table, customer_id=CUSTOMER)

        self.assertIsNotNone(
            catalog.get_rule(automatable_entry.rule_id, automatable_entry.rule_version)
        )
        # 승인되지 않은 MANUAL 후보는 Rule Registry에 아예 없다.
        self.assertIsNone(catalog.get_rule(manual_entry.rule_id, manual_entry.rule_version))

    def test_an_unsupported_requirement_never_becomes_an_approvable_rule(self) -> None:
        self._extract()
        page = self._review()

        self.assertEqual(len(page.unsupported), 1)
        summaries = {entry.requirement_summary for entry in page.candidates}
        self.assertNotIn(page.unsupported[0].requirement_summary, summaries)


class ManualRuleEndToEndTest(unittest.TestCase):
    """조직 정책 문장 → MANUAL 후보 → 승인 → Profile → MANUAL_REVIEW 결과."""

    def setUp(self) -> None:
        self.table = FakeTable()
        store_ingestion_item(self.table)
        self.queue = _Queue()
        self.candidates = PolicyCandidateApiService(
            repository=_repository(self.table),
            queue=self.queue,  # type: ignore[arg-type]
            run_id_factory=lambda: "authoring-run-1",
        )
        self.approvals = PolicyApprovalApiService(_repository(self.table))

    def test_a_manual_requirement_produces_a_stable_review_coordinate(self) -> None:
        self.candidates.request_extraction(
            _principal(), source_id=DOCUMENT.source_id, source_version=DOCUMENT.source_version
        )
        _run_authoring_worker(self.table, self.queue.requests[0], requirements=(manual(),))
        page = self.candidates.list_candidates(
            _principal(), source_id=DOCUMENT.source_id, source_version=DOCUMENT.source_version
        )
        entry = page.candidates[0]
        self.assertIs(entry.evaluation_type, RuleEvaluationType.MANUAL)
        self.assertEqual(entry.resource_types, (GOVERNANCE_ASSESSMENT_RESOURCE_TYPE,))

        reference = PolicyRuleReference(rule_id=entry.rule_id, version=entry.rule_version)
        self.approvals.approve(
            _principal(),
            source_id=DOCUMENT.source_id,
            source_version=DOCUMENT.source_version,
            approved_rules=(reference,),
        )
        _repository(self.table).record_profile(
            customer_id=CUSTOMER,
            profile=PolicyProfile(
                policy_profile_id=PROFILE_ID, version="v1", rule_references=(reference,)
            ),
            published_by="admin@example.com",
            published_at="2026-09-03T00:00:00Z",
        )

        catalog = DynamoDbPolicyCatalog(self.table, customer_id=CUSTOMER)
        context = PolicyContextResolver(catalog).resolve(
            policy_profile_id=PROFILE_ID,
            phase=AssessmentPhase.INITIAL,
            resource_type=GOVERNANCE_ASSESSMENT_RESOURCE_TYPE,
            expected_profile_version="v1",
        )
        results = AssessmentRunner(ManualReviewEvaluator()).evaluate_resource(
            resource_id=governance_resource_id(REPOSITORY),
            context=context,
            model_profile=MODEL_PROFILE,
        )

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertIs(result.perspective, EvaluationPerspective.MANUAL)
        self.assertIs(result.status, EvaluationStatus.MANUAL_REVIEW)
        # Initial과 Verification이 같은 좌표를 가져야 비교가 성립한다.
        self.assertEqual(result.resource_id, f"governance:{REPOSITORY}")

    def test_the_manual_rule_applies_to_both_compared_phases(self) -> None:
        self.candidates.request_extraction(
            _principal(), source_id=DOCUMENT.source_id, source_version=DOCUMENT.source_version
        )
        _run_authoring_worker(self.table, self.queue.requests[0], requirements=(manual(),))
        page = self.candidates.list_candidates(
            _principal(), source_id=DOCUMENT.source_id, source_version=DOCUMENT.source_version
        )
        reference = PolicyRuleReference(
            rule_id=page.candidates[0].rule_id, version=page.candidates[0].rule_version
        )
        self.approvals.approve(
            _principal(),
            source_id=DOCUMENT.source_id,
            source_version=DOCUMENT.source_version,
            approved_rules=(reference,),
        )

        rule = DynamoDbPolicyCatalog(self.table, customer_id=CUSTOMER).get_rule(
            reference.rule_id, reference.version
        )

        assert rule is not None
        self.assertIn(AssessmentPhase.INITIAL, rule.applicable_phases)
        self.assertIn(AssessmentPhase.POST_DEPLOY_VERIFICATION, rule.applicable_phases)


class PolicyVersioningTest(unittest.TestCase):
    """v1 승인·게시·평가 → v2 업로드 → v1 승인이 v2로 상속되지 않는다."""

    def setUp(self) -> None:
        self.table = FakeTable()
        store_ingestion_item(self.table)
        self.queue = _Queue()
        self.candidates = PolicyCandidateApiService(
            repository=_repository(self.table),
            queue=self.queue,  # type: ignore[arg-type]
            run_id_factory=lambda: "authoring-run-1",
        )
        self.approvals = PolicyApprovalApiService(_repository(self.table))

    def test_a_running_assessment_keeps_its_pinned_profile_after_a_new_publication(self) -> None:
        self.candidates.request_extraction(
            _principal(), source_id=DOCUMENT.source_id, source_version=DOCUMENT.source_version
        )
        _run_authoring_worker(
            self.table, self.queue.requests[0], requirements=(automatable(), manual())
        )
        page = self.candidates.list_candidates(
            _principal(), source_id=DOCUMENT.source_id, source_version=DOCUMENT.source_version
        )
        references = tuple(
            PolicyRuleReference(rule_id=entry.rule_id, version=entry.rule_version)
            for entry in page.candidates
        )
        self.approvals.approve(
            _principal(),
            source_id=DOCUMENT.source_id,
            source_version=DOCUMENT.source_version,
            approved_rules=references,
        )
        repository = _repository(self.table)
        automatable_reference = next(
            reference
            for reference, entry in zip(references, page.candidates, strict=True)
            if entry.evaluation_type is not RuleEvaluationType.MANUAL
        )
        repository.record_profile(
            customer_id=CUSTOMER,
            profile=PolicyProfile(
                policy_profile_id=PROFILE_ID, version="v1", rule_references=references
            ),
            published_by="admin@example.com",
            published_at="2026-09-03T00:00:00Z",
        )

        # Assessment가 v1을 고정한 뒤, 더 좁은 v2가 게시된다.
        repository.record_profile(
            customer_id=CUSTOMER,
            profile=PolicyProfile(
                policy_profile_id=PROFILE_ID,
                version="v2",
                rule_references=(automatable_reference,),
            ),
            published_by="admin@example.com",
            published_at="2026-09-03T01:00:00Z",
            expected_current_version="v1",
        )

        resolver = PolicyContextResolver(DynamoDbPolicyCatalog(self.table, customer_id=CUSTOMER))
        pinned = resolver.resolve(
            policy_profile_id=PROFILE_ID,
            phase=AssessmentPhase.INITIAL,
            resource_type=GOVERNANCE_ASSESSMENT_RESOURCE_TYPE,
            expected_profile_version="v1",
        )

        # v1을 고정한 Assessment는 MANUAL Rule을 계속 평가한다. v2에는 그 Rule이 없다.
        self.assertEqual(pinned.policy_profile_version, "v1")
        self.assertEqual(len(pinned.rules), 1)

    def test_a_re_extraction_with_a_different_prompt_fails_closed(self) -> None:
        """같은 source version을 다른 추출로 덮어쓰면 리뷰 중인 후보 집합이 설명 없이 바뀐다."""
        from apps.backend.policy.authoring import ExtractorIdentity
        from apps.backend.repositories.errors import RepositoryError

        self.candidates.request_extraction(
            _principal(), source_id=DOCUMENT.source_id, source_version=DOCUMENT.source_version
        )
        request = self.queue.requests[0]
        _run_authoring_worker(self.table, request, requirements=(automatable(),))

        other = extract_policy_candidates(
            customer_id=CUSTOMER,
            document=DOCUMENT,
            artifact_reader=NormalizedArtifactReader(reader=_ArtifactSource(), bucket=BUCKET),  # type: ignore[arg-type]
            extractor=FakePolicyCandidateExtractor(
                (automatable(),),
                identity=ExtractorIdentity(
                    extractor_id="fake-policy-candidate-extractor",
                    extractor_version="1.0.0",
                    model_id="fake",
                    model_version="1",
                    prompt_version="policy-authoring/v2",
                ),
            ),
            catalog=MVP_CONTROL_CATALOG,
            authoring_run_id=request.authoring_run_id,
            requested_at=request.requested_at,
        )

        with self.assertRaises(RepositoryError):
            _repository(self.table).record_authoring_result(customer_id=CUSTOMER, result=other)


class TenantAndTextBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.table = FakeTable()
        store_ingestion_item(self.table)
        self.queue = _Queue()
        self.candidates = PolicyCandidateApiService(
            repository=_repository(self.table),
            queue=self.queue,  # type: ignore[arg-type]
            run_id_factory=lambda: "authoring-run-1",
        )
        self.candidates.request_extraction(
            _principal(), source_id=DOCUMENT.source_id, source_version=DOCUMENT.source_version
        )
        _run_authoring_worker(
            self.table,
            self.queue.requests[0],
            requirements=(automatable(), manual(), unsupported()),
        )

    def test_no_stored_item_or_api_response_carries_a_verbatim_source_sentence(self) -> None:
        """원문은 `ExtractionUnit` 안에만 존재한다. 그 타입에는 직렬화가 없다."""
        page = self.candidates.list_candidates(
            _principal(), source_id=DOCUMENT.source_id, source_version=DOCUMENT.source_version
        )
        stored = repr(self.table.items)
        rendered = repr(page.to_dict())

        for _locator, _kind, text in UNIT_TEXTS:
            with self.subTest(text=text[:32]):
                self.assertNotIn(text, stored)
                self.assertNotIn(text, rendered)

    def test_another_customer_reads_none_of_this(self) -> None:
        """다른 고객의 파티션에서는 이 실행이 존재하지 않는다 — 404이지 503이 아니다."""
        from packages.common.errors import PolicySourceNotFound

        with self.assertRaises(PolicySourceNotFound):
            self.candidates.list_candidates(
                _principal("cust-002"),
                source_id=DOCUMENT.source_id,
                source_version=DOCUMENT.source_version,
            )

    def test_no_candidate_carries_a_model_written_severity_or_score(self) -> None:
        """LLM은 severity·score·judgment를 만들지 않는다. severity는 Catalog가 정한다."""
        page = self.candidates.list_candidates(
            _principal(), source_id=DOCUMENT.source_id, source_version=DOCUMENT.source_version
        )

        for entry in page.candidates:
            with self.subTest(rule=entry.rule_id):
                payload = entry.to_dict()
                for forbidden in ("judgment", "score", "source_score", "anchor", "severity"):
                    self.assertNotIn(forbidden, payload)
                self.assertIn("proposed_severity", payload)


if __name__ == "__main__":
    unittest.main()
