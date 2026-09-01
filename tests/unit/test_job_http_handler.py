"""API Gateway adapter tests for the public M0 Job routes."""

import json
import unittest

from apps.backend.api.assessments import AssessmentReportApiService
from apps.backend.api.handler import JobHttpHandler
from apps.backend.api.jobs import JobApiService
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


class ApprovedScope:
    def authorize(self, principal, *, repository_id: str, policy_profile_id: str) -> None:
        return None


class Reports:
    def get_assessment_job_id(self, *, customer_id: str, assessment_id: str) -> str:
        if customer_id != "cust-001" or assessment_id != "asm-001":
            raise LookupError
        return "job-001"

    def get_report_page(
        self, *, customer_id: str, assessment_id: str, limit: int, cursor: str | None
    ):
        return AssessmentReport(
            assessment_id=assessment_id,
            results=(),
            findings=(),
            coverage=AssessmentCoverage(planned_evaluations=1, completed_evaluations=0),
            readiness_score=None,
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
