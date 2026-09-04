"""API Gateway adapter tests for the public M0 Job routes."""

import json
import unittest

from apps.backend.api.assessments import AssessmentReportApiService
from apps.backend.api.handler import JobHttpHandler
from apps.backend.api.jobs import JobApiService
from apps.backend.api.policy_sources import PolicySourceApiService, PolicySourceUploadSession
from apps.backend.assessment import AssessmentReport
from apps.backend.jobs import OutboxDispatcher
from packages.contracts import AssessmentCoverage


class InMemoryJobRepository:
    def __init__(self) -> None:
        self.jobs = {}
        self.outbox = []

    def create_assessment_workflow(self, assessment, job, outbox) -> None:
        self.jobs[(job.customer_id, job.job_id)] = job
        self.outbox.append(outbox)

    def create_job(self, job) -> None:
        self.jobs[(job.customer_id, job.job_id)] = job

    def get_job(self, customer_id: str, job_id: str):
        return self.jobs.get((customer_id, job_id))

    def update_job(self, job, *, expected_revision: int) -> None:
        self.jobs[(job.customer_id, job.job_id)] = job

    def mark_outbox_dispatched(self, entry) -> None:
        self.outbox.remove(entry)

    def record_outbox_dispatch_failure(self, entry) -> None:
        return None


class Dispatcher:
    def dispatch(self, task) -> None:
        return None


class PublishedProfiles:
    """A tenant-scoped Profile reader. 게시된 Profile만 존재하고, 판본이 함께 나온다."""

    def __init__(self, version: str = "v1") -> None:
        self.version = version
        self.customers: list[str] = []

    def __call__(self, *, customer_id: str) -> "PublishedProfiles":
        self.customers.append(customer_id)
        return self

    def get_profile(self, policy_profile_id: str, version: str | None = None):
        from packages.contracts import PolicyProfile, PolicyRuleReference

        if version is not None and version != self.version:
            return None
        return PolicyProfile(
            policy_profile_id=policy_profile_id,
            version=self.version,
            rule_references=(PolicyRuleReference(rule_id="RULE-1", version="v1"),),
        )


class ApprovedScope:
    def authorize(self, principal, *, repository_id: str) -> None:
        return None


class Reports:
    def get_assessment_job_id(self, *, customer_id: str, assessment_id: str) -> str:
        if customer_id != "cust-001" or assessment_id != "asm-001":
            raise LookupError
        return "job-001"

    def get_report_page(
        self,
        *,
        customer_id: str,
        assessment_id: str,
        limit: int,
        cursor: str | None,
        findings_cursor: str | None = None,
    ):
        return AssessmentReport(
            assessment_id=assessment_id,
            results=(),
            findings=(),
            coverage=AssessmentCoverage(planned_evaluations=1, completed_evaluations=0),
            readiness_score=None,
        )


class PolicyUploads:
    def create_upload_session(self, **kwargs):
        return PolicySourceUploadSession(
            source_id="source-1", source_version="v1", upload_url="https://example.invalid/upload"
        )


def event(method: str, path: str, body: str | None = None) -> dict[str, object]:
    return {
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
                        "custom:customer_id": "cust-001",
                        "cognito:groups": ["User"],
                    }
                }
            },
        },
    }


class JobHttpHandlerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = InMemoryJobRepository()
        service = JobApiService(
            repository=self.repository,
            assessment_scope=ApprovedScope(),
            policy_catalog_factory=PublishedProfiles(),
            outbox_dispatcher=OutboxDispatcher(repository=self.repository, dispatcher=Dispatcher()),
            job_id_factory=lambda: "job-001",
            assessment_id_factory=lambda: "asm-001",
        )
        self.handler = JobHttpHandler(
            service,
            assessment_reports=AssessmentReportApiService(jobs=self.repository, reports=Reports()),
        )

    def test_post_assessment_returns_accepted_public_job_projection(self) -> None:
        response = self.handler.handle(
            event(
                "POST",
                "/assessments",
                '{"repository_id":"repo-001","policy_profile_id":"profile-001"}',
            )
        )

        self.assertEqual(response["statusCode"], 202)
        self.assertEqual(json.loads(response["body"])["job_id"], "job-001")

    def test_authorizer_serialized_groups_string_is_accepted(self) -> None:
        # The HTTP API JWT authorizer flattens cognito:groups into a bracketed
        # space-separated string; the handler must restore the array shape.
        request = event(
            "POST",
            "/assessments",
            '{"repository_id":"repo-001","policy_profile_id":"profile-001"}',
        )
        request["requestContext"]["authorizer"]["jwt"]["claims"]["cognito:groups"] = "[User]"

        response = self.handler.handle(request)

        self.assertEqual(response["statusCode"], 202)

    def test_authorizer_empty_groups_string_is_denied(self) -> None:
        request = event("POST", "/assessments", "{}")
        request["requestContext"]["authorizer"]["jwt"]["claims"]["cognito:groups"] = "[]"

        response = self.handler.handle(request)

        self.assertEqual(response["statusCode"], 401)

    def test_client_cannot_supply_tenant_or_lifecycle_fields(self) -> None:
        response = self.handler.handle(
            event(
                "POST",
                "/assessments",
                '{"repository_id":"repo-001","policy_profile_id":"profile-001","customer_id":"other"}',
            )
        )

        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(json.loads(response["body"])["error"]["code"], "VALIDATION_ERROR")

    def test_missing_customer_claim_is_unauthorized(self) -> None:
        request = event("GET", "/jobs/job-001")
        claims = request["requestContext"]["authorizer"]["jwt"]["claims"]
        del claims["custom:customer_id"]

        response = self.handler.handle(request)

        self.assertEqual(response["statusCode"], 401)
        self.assertEqual(json.loads(response["body"])["error"]["code"], "UNAUTHORIZED")

    def test_missing_authorizer_is_unauthorized(self) -> None:
        request = event("GET", "/jobs/job-001")
        del request["requestContext"]["authorizer"]

        response = self.handler.handle(request)

        self.assertEqual(response["statusCode"], 401)
        self.assertEqual(json.loads(response["body"])["error"]["code"], "UNAUTHORIZED")

    def test_get_assessment_returns_paginated_report_after_job_owner_check(self) -> None:
        self.handler.handle(
            event(
                "POST",
                "/assessments",
                '{"repository_id":"repo-001","policy_profile_id":"profile-001"}',
            )
        )

        response = self.handler.handle(
            {
                **event("GET", "/assessments/asm-001"),
                "queryStringParameters": {"limit": "10"},
            }
        )

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(json.loads(response["body"])["coverage"]["percentage"], 0)

    def test_admin_can_create_a_server_scoped_policy_upload_session(self) -> None:
        service = JobApiService(
            repository=self.repository,
            assessment_scope=ApprovedScope(),
            policy_catalog_factory=PublishedProfiles(),
            outbox_dispatcher=OutboxDispatcher(repository=self.repository, dispatcher=Dispatcher()),
            job_id_factory=lambda: "job-002",
            assessment_id_factory=lambda: "asm-002",
        )
        handler = JobHttpHandler(
            service,
            policy_sources=PolicySourceApiService(
                repository=PolicyUploads(),
                source_id_factory=lambda: "source-1",
                source_version_factory=lambda: "v1",
            ),
        )
        request = event(
            "POST",
            "/policy-sources/uploads",
            '{"filename":"policy.md","declared_media_type":"text/markdown","byte_size":12}',
        )
        request["requestContext"]["authorizer"]["jwt"]["claims"]["cognito:groups"] = ["Admin"]

        response = handler.handle(request)

        self.assertEqual(response["statusCode"], 201)
        self.assertEqual(json.loads(response["body"])["source_id"], "source-1")


class _OrchestrationStub:
    """Duck-typed orchestrations service: only .orchestrate(principal, request) is called."""

    def __init__(self, *, error: Exception | None = None, decision: object | None = None) -> None:
        self._error = error
        self._decision = decision

    def orchestrate(self, _principal, _request):
        if self._error is not None:
            raise self._error
        return self._decision


def _orchestrate_handler(orchestrations: object) -> JobHttpHandler:
    service = JobApiService(
        repository=InMemoryJobRepository(),
        assessment_scope=ApprovedScope(),
        policy_catalog_factory=PublishedProfiles(),
        outbox_dispatcher=OutboxDispatcher(
            repository=InMemoryJobRepository(), dispatcher=Dispatcher()
        ),
        job_id_factory=lambda: "job-001",
        assessment_id_factory=lambda: "asm-001",
    )
    return JobHttpHandler(service, orchestrations=orchestrations)


class OrchestrateRouteTest(unittest.TestCase):
    def test_a_router_failure_maps_to_502_not_an_opaque_500(self) -> None:
        # OrchestrationError is a ValueError; without the explicit wrap it would fall through to a
        # 500 EXECUTION_ERROR that hides the assistant being the failing dependency.
        handler = _orchestrate_handler(_OrchestrationStub(error=ValueError("model returned junk")))
        response = handler.handle(event("POST", "/orchestrate", '{"message":"evaluate test repo"}'))
        self.assertEqual(response["statusCode"], 502)
        self.assertEqual(json.loads(response["body"])["error"]["code"], "ORCHESTRATION_UNAVAILABLE")

    def test_authorization_denial_is_still_a_403(self) -> None:
        from apps.backend.auth import AuthorizationDenied

        handler = _orchestrate_handler(_OrchestrationStub(error=AuthorizationDenied("no")))
        response = handler.handle(event("POST", "/orchestrate", '{"message":"hi"}'))
        self.assertEqual(response["statusCode"], 403)
        self.assertEqual(json.loads(response["body"])["error"]["code"], "SCOPE_DENIED")

    def test_a_valid_decision_returns_200(self) -> None:
        from packages.contracts import OrchestrationDecision, OrchestrationIntent

        decision = OrchestrationDecision(
            intent=OrchestrationIntent.POLICY_QA,
            rationale="asks a question",
            answer="Block public access on S3 buckets.",
        )
        handler = _orchestrate_handler(_OrchestrationStub(decision=decision))
        response = handler.handle(event("POST", "/orchestrate", '{"message":"what is our rule?"}'))
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(json.loads(response["body"])["intent"], "POLICY_QA")

    def test_a_malformed_body_is_a_400_before_the_router(self) -> None:
        handler = _orchestrate_handler(_OrchestrationStub(error=ValueError("should not run")))
        response = handler.handle(event("POST", "/orchestrate", '{"message":""}'))
        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(json.loads(response["body"])["error"]["code"], "VALIDATION_ERROR")


if __name__ == "__main__":
    unittest.main()
