"""Shared E2E: deployment 생성→승인→apply→검증=VERIFIED + audit trail 조회.

실제 DynamoDB/GitHub/Terraform 없이, HTTP handler 라우팅과 RBAC를 통과하는 in-memory
fake로 M3 A의 승인 배포 happy-path를 끝에서 끝까지 고정한다. 생성이 남긴 DEPLOYMENT_REQUESTED와
승인이 남긴 DEPLOYMENT_APPROVED audit event가 같은 in-memory store에 쌓이고, Admin이
`GET /audit-events`로 그 이력을 최신순으로 조회하는 것까지 확인한다.
"""

import json
import unittest
from dataclasses import replace

from apps.backend.api.audit import AuditEventApiService
from apps.backend.api.deployments import (
    DeploymentApiService,
    DeploymentSource,
)
from apps.backend.api.handler import JobHttpHandler
from apps.backend.api.jobs import JobApiService
from apps.backend.assessment import ComparisonAssessment
from apps.backend.assessment.reporting import AssessmentReport
from apps.backend.deployment import DeploymentApprovalService, DeploymentRecord
from apps.backend.jobs import OutboxDispatcher
from packages.contracts import (
    ApplyOutcome,
    ArtifactReference,
    ArtifactType,
    AssessmentCoverage,
    AuditEventPage,
    AuditEventType,
    AuditEventView,
    DeploymentFacts,
    DeploymentStatus,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    JobCurrentStep,
    JobStatus,
    PlannedEvaluation,
    ReadinessScore,
    RemediationAction,
    TerraformPlan,
    VerificationOutcome,
)
from packages.contracts.remediation import DeploymentReadiness, DeploymentReadinessStatus

CUSTOMER = "cust-001"
REPO = "repo-001"
REMEDIATION = "remediation-001"
DEPLOYMENT = "deployment-001"
COMMIT = "commit-001"
PLAN_HASH = "plan-hash-001"
SOURCE_ASM = "asm-source"
VERIFY_ASM = "asm-verify"


class AuditStore:
    """create/approve가 쓰고 GET /audit-events가 읽는 공용 in-memory audit 이력."""

    def __init__(self) -> None:
        self.events: list[AuditEventView] = []

    def append(self, event_type: AuditEventType, occurred_at: str, **attributes: object) -> None:
        self.events.append(
            AuditEventView(
                event_id=f"audit-{len(self.events) + 1:03d}",
                customer_id=CUSTOMER,
                event_type=event_type,
                occurred_at=occurred_at,
                attributes=attributes,
            )
        )

    def list_events(self, *, customer_id, limit, cursor=None, event_type=None) -> AuditEventPage:
        assert customer_id == CUSTOMER
        events = [e for e in self.events if e.customer_id == customer_id]
        if event_type is not None:
            events = [e for e in events if e.event_type is event_type]
        # 최신순.
        events = list(reversed(events))[:limit]
        return AuditEventPage(events=tuple(events))


class DeploymentStore:
    """생성·조회 in-memory. 생성 시 audit store에 DEPLOYMENT_REQUESTED를 남긴다."""

    def __init__(self, audit: AuditStore, source: DeploymentSource) -> None:
        self._audit = audit
        self._source = source
        self.records: dict[str, DeploymentRecord] = {}

    # --- DeploymentSourceReader ---
    def get_deployment_source(self, *, customer_id, remediation_id) -> DeploymentSource:
        assert (customer_id, remediation_id) == (CUSTOMER, REMEDIATION)
        return self._source

    # --- DeploymentRecordRepository ---
    def create_deployment(self, record, *, job, outbox) -> None:
        self.records[record.deployment_id] = record
        self._audit.append(
            AuditEventType.DEPLOYMENT_REQUESTED,
            "2026-09-03T00:00:00Z",
            deployment_id=record.deployment_id,
            remediation_id=record.remediation_id,
            commit_sha=record.commit_sha,
            plan_hash=record.plan_hash,
        )

    def mark_verified(self, deployment_id: str, verification_assessment_id: str) -> None:
        """검증 Worker가 새 검증 Assessment를 record에 결합한 상태를 시뮬레이션한다."""
        record = self.records[deployment_id]
        self.records[deployment_id] = replace(
            record, verification_assessment_id=verification_assessment_id
        )

    def get_deployment(self, *, customer_id, deployment_id):
        assert customer_id == CUSTOMER
        return self.records.get(deployment_id)


class ApprovalStore:
    """승인 in-memory. 승인 시 audit store에 DEPLOYMENT_APPROVED를 남긴다."""

    def __init__(self, audit: AuditStore) -> None:
        self._audit = audit
        self.approvals: list = []

    def record_approval(self, *, customer_id, approval, readiness) -> None:
        assert customer_id == CUSTOMER
        self.approvals.append(approval)
        self._audit.append(
            AuditEventType.DEPLOYMENT_APPROVED,
            "2026-09-03T01:00:00Z",
            deployment_id=approval.deployment_id,
            commit_sha=approval.commit_sha,
            plan_hash=approval.plan_hash,
        )


class PlanReader:
    """승인 화면이 읽는 저장 plan + C readiness (READY_FOR_APPROVAL)."""

    def get_approval_input(self, *, customer_id, deployment_id):
        assert customer_id == CUSTOMER
        plan = TerraformPlan(
            deployment_id=deployment_id,
            commit_sha=COMMIT,
            plan_hash=PLAN_HASH,
            artifact=ArtifactReference(
                artifact_id="art-plan-001",
                artifact_type=ArtifactType.TERRAFORM_PLAN,
                content_sha256=PLAN_HASH,
                customer_id=CUSTOMER,
                repository_id=REPO,
            ),
        )
        readiness = DeploymentReadiness(
            deployment_id=deployment_id,
            finding_id="finding-001",
            commit_sha=COMMIT,
            plan_hash=PLAN_HASH,
            status=DeploymentReadinessStatus.READY_FOR_APPROVAL,
            reason_codes=("READY",),
        )
        return plan, readiness


class FactsReader:
    """apply·검증까지 끝난 durable 사실. VERIFIED로 파생되도록 구성한다."""

    def get_deployment_facts(self, *, customer_id, deployment_id) -> DeploymentFacts:
        assert customer_id == CUSTOMER
        return DeploymentFacts(
            job_status=JobStatus.COMPLETED,
            current_step=JobCurrentStep.POST_DEPLOY_VERIFICATION,
            is_approved=True,
            apply_outcome=ApplyOutcome.SUCCEEDED,
            verification_outcome=VerificationOutcome.COMPARABLE,
        )


class ComparisonReader:
    """before(FAIL 20) / after(PASS 100) 완전 입력. comparable delta를 낸다."""

    def get_comparison_inputs(
        self, *, customer_id, source_assessment_id, verification_assessment_id
    ):
        assert customer_id == CUSTOMER
        return (
            _comparison(source_assessment_id, EvaluationStatus.FAIL, 20.0),
            _comparison(verification_assessment_id, EvaluationStatus.PASS, 100.0),
        )


def _comparison(assessment_id, status: EvaluationStatus, score: float) -> ComparisonAssessment:
    result = EvaluationResult(
        resource_id="bucket-001",
        rule_id="S3-001",
        perspective=EvaluationPerspective.AWS_ACTUAL,
        status=status,
        severity="HIGH",
        score=100 if status is EvaluationStatus.PASS else 20,
        rationale="fixture",
        evidence_references=("aws:s3:fixture",),
        rule_version="v1",
        rubric_version="m1-v1",
        model_profile_id="assessment-profile-v1",
    )
    report = AssessmentReport(
        assessment_id=assessment_id,
        results=(result,),
        findings=(),
        coverage=AssessmentCoverage(planned_evaluations=1, completed_evaluations=1),
        readiness_score=ReadinessScore(score=score, evaluated_evaluations=1),
    )
    return ComparisonAssessment(
        assessment_id=assessment_id,
        model_profile_id="assessment-profile-v1",
        rubric_version="m1-v1",
        planned_evaluations=(
            PlannedEvaluation(
                resource_id="bucket-001",
                rule_id="S3-001",
                perspective=EvaluationPerspective.AWS_ACTUAL,
            ),
        ),
        report=report,
    )


class JobStore:
    def __init__(self) -> None:
        self.jobs: dict[tuple[str, str], object] = {}

    def create_assessment_workflow(self, assessment, job, outbox) -> None:  # pragma: no cover
        raise AssertionError("not used")

    def get_job(self, customer_id, job_id):  # pragma: no cover - unused in happy path
        return self.jobs.get((customer_id, job_id))

    def mark_outbox_dispatched(self, entry) -> None:
        return None

    def record_outbox_dispatch_failure(self, entry) -> None:  # pragma: no cover - unused
        return None


class OutboxRepo:
    """create_deployment이 outbox 인자를 만들 수 있도록 하는 최소 dispatcher 대상."""

    def __init__(self) -> None:
        self.pending: list = []

    def enqueue_outbox(self, entry) -> None:  # pragma: no cover - shape varies
        self.pending.append(entry)


class Dispatcher:
    def dispatch(self, task) -> None:
        return None


class ApprovedScope:
    def authorize(self, principal, *, repository_id, policy_profile_id) -> None:
        return None


def _handler(audit: AuditStore) -> JobHttpHandler:
    source = DeploymentSource(
        remediation_id=REMEDIATION,
        customer_id=CUSTOMER,
        repository_id=REPO,
        commit_sha=COMMIT,
        source_assessment_id=SOURCE_ASM,
        action=RemediationAction.TERRAFORM_PATCH,
        has_worker_result=True,
        commit_reachable_from_default_branch=True,
    )
    deployments = DeploymentStore(audit, source)
    jobs = JobStore()
    outbox_dispatcher = OutboxDispatcher(repository=jobs, dispatcher=Dispatcher())

    deployment_service = DeploymentApiService(
        approvals=DeploymentApprovalService(ApprovalStore(audit)),
        plans=PlanReader(),
        sources=deployments,
        deployments=deployments,
        facts=FactsReader(),
        comparisons=ComparisonReader(),
        jobs=jobs,
        outbox_dispatcher=outbox_dispatcher,
        deployment_id_factory=lambda: DEPLOYMENT,
        job_id_factory=lambda: "job-001",
        now=lambda: _FixedNow(),
    )
    job_service = JobApiService(
        repository=jobs,
        assessment_scope=ApprovedScope(),
        outbox_dispatcher=outbox_dispatcher,
        job_id_factory=lambda: "job-001",
        assessment_id_factory=lambda: "asm-001",
    )
    handler = JobHttpHandler(
        job_service,
        deployments=deployment_service,
        audit_events=AuditEventApiService(audit),
    )
    return handler, deployments


class _FixedNow:
    def isoformat(self) -> str:  # pragma: no cover - reject path only
        return "2026-09-03T02:00:00+00:00"


def event(method: str, path: str, *, groups=("User",), body=None, query=None) -> dict:
    request: dict = {
        "rawPath": path,
        "body": body,
        "requestContext": {
            "http": {"method": method},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "token_use": "access",
                        "sub": "subject-001",
                        "client_id": "client-001",
                        "custom:customer_id": CUSTOMER,
                        "cognito:groups": list(groups),
                    }
                }
            },
        },
    }
    if query is not None:
        request["queryStringParameters"] = query
    return request


class DeploymentLifecycleE2ETest(unittest.TestCase):
    def test_create_approve_apply_verify_and_read_audit_trail(self) -> None:
        audit = AuditStore()
        handler, deployments = _handler(audit)

        # 1) User가 승인된 remediation으로 Deployment 생성 → 202 + Job.
        created = handler.handle(
            event("POST", f"/remediations/{REMEDIATION}/deployments", groups=("User",))
        )
        self.assertEqual(created["statusCode"], 202)

        # 2) Admin이 저장된 plan/hash로 승인 → 200.
        approved = handler.handle(
            event(
                "POST",
                f"/deployments/{DEPLOYMENT}/approve",
                groups=("Admin",),
                body=json.dumps({"commit_sha": COMMIT, "plan_hash": PLAN_HASH}),
            )
        )
        self.assertEqual(approved["statusCode"], 200)
        self.assertEqual(json.loads(approved["body"])["plan_hash"], PLAN_HASH)

        # apply·검증 Worker가 새 검증 Assessment를 record에 결합한 상태로 진행한다.
        deployments.mark_verified(DEPLOYMENT, VERIFY_ASM)

        # 3) apply·검증이 끝난 durable 사실로 status를 조회 → VERIFIED.
        view = handler.handle(event("GET", f"/deployments/{DEPLOYMENT}", groups=("User",)))
        self.assertEqual(view["statusCode"], 200)
        self.assertEqual(json.loads(view["body"])["status"], DeploymentStatus.VERIFIED.value)

        # 4) before/after 비교 조회 → comparable delta.
        verification = handler.handle(
            event("GET", f"/deployments/{DEPLOYMENT}/verification", groups=("User",))
        )
        self.assertEqual(verification["statusCode"], 200)
        body = json.loads(verification["body"])
        self.assertTrue(body["comparable"])
        self.assertEqual(body["readiness_score_delta"], 80.0)

        # 5) Admin이 감사 이력을 최신순으로 조회 → APPROVED가 먼저, REQUESTED가 뒤.
        events = handler.handle(event("GET", "/audit-events", groups=("Admin",)))
        self.assertEqual(events["statusCode"], 200)
        trail = json.loads(events["body"])["events"]
        self.assertEqual(
            [e["event_type"] for e in trail],
            [
                AuditEventType.DEPLOYMENT_APPROVED.value,
                AuditEventType.DEPLOYMENT_REQUESTED.value,
            ],
        )
        self.assertTrue(all(e["deployment_id"] == DEPLOYMENT for e in trail))


if __name__ == "__main__":
    unittest.main()
