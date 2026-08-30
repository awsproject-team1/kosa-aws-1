"""API Gateway adapter tests for the public M0 Job routes."""

import json
import unittest

from apps.backend.api.handler import JobHttpHandler
from apps.backend.api.jobs import JobApiService


class InMemoryJobRepository:
    def __init__(self) -> None:
        self.jobs = {}

    def create_job(self, job) -> None:
        self.jobs[(job.customer_id, job.job_id)] = job

    def get_job(self, customer_id: str, job_id: str):
        return self.jobs.get((customer_id, job_id))


class ApprovedScope:
    def authorize(self, principal, *, repository_id: str, policy_profile_id: str) -> None:
        return None


class RecordingDispatcher:
    def dispatch(self, task) -> None:
        return None


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
            dispatcher=RecordingDispatcher(),
            job_id_factory=lambda: "job-001",
        )
        self.handler = JobHttpHandler(service)

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
