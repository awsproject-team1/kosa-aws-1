"""`GET /deployments/{id}/observability` HTTP 경계 테스트 (ADR-0021 §3).

고정하는 불변식:
- Admin 전용이고 범위는 호출자의 verified customer다.
- live metric source가 주입되지 않은 배포에서는 route 자체가 없다(404). "source가 없다"를
  500으로 보여주지 않는다.
- 이 경로가 `GET /deployments/{id}`보다 먼저 매칭돼야 관측 조회가 배포 조회로 새지 않는다.
"""

import json
import unittest

from agent.runtime.mock_observability_source import MockDemoRunMetricsSource
from apps.backend.api.handler import JobHttpHandler
from apps.backend.api.jobs import JobApiService
from apps.backend.api.observability import DemoRunObservabilityService
from apps.backend.jobs import OutboxDispatcher
from tests.unit.test_mock_observability_source import _record

CUSTOMER_ID = "cust-001"
DEPLOYMENT_ID = "dep-001"


class Repository:
    def get_job(self, customer_id, job_id):
        return None

    def create_assessment_workflow(self, assessment, job, outbox):
        return None

    def list_pending_outbox(self, *, limit):
        return ()

    def mark_outbox_dispatched(self, entry):
        return None

    def record_outbox_dispatch_failure(self, entry):
        return None


class Dispatcher:
    def dispatch(self, task):
        return None


class Scope:
    def authorize(self, principal, *, repository_id, policy_profile_id):
        return None


def event(method, path, *, groups=("Admin",)):
    return {
        "rawPath": path,
        "requestContext": {
            "http": {"method": method},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "token_use": "access",
                        "sub": "subject-001",
                        "client_id": "client-001",
                        "custom:customer_id": CUSTOMER_ID,
                        "cognito:groups": list(groups),
                    }
                }
            },
        },
    }


def handler(*, wired=True):
    repository = Repository()
    jobs = JobApiService(
        repository=repository,
        assessment_scope=Scope(),
        outbox_dispatcher=OutboxDispatcher(repository=repository, dispatcher=Dispatcher()),
        job_id_factory=lambda: "job-001",
        assessment_id_factory=lambda: "asm-001",
    )
    observability = None
    if wired:
        source = MockDemoRunMetricsSource(customer_id=CUSTOMER_ID)
        source.register_run(_record(customer_id=CUSTOMER_ID, deployment_id=DEPLOYMENT_ID))
        observability = DemoRunObservabilityService(source)
    return JobHttpHandler(jobs, observability=observability)


class ObservabilityHttpHandlerTest(unittest.TestCase):
    def test_admin_reads_the_demo_run_record(self):
        response = handler().handle(event("GET", f"/deployments/{DEPLOYMENT_ID}/observability"))
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["deployment_id"], DEPLOYMENT_ID)
        self.assertEqual(body["customer_id"], CUSTOMER_ID)

    def test_unwired_deployment_has_no_route(self):
        response = handler(wired=False).handle(
            event("GET", f"/deployments/{DEPLOYMENT_ID}/observability")
        )
        self.assertEqual(response["statusCode"], 404)

    def test_non_admin_is_forbidden(self):
        response = handler().handle(
            event("GET", f"/deployments/{DEPLOYMENT_ID}/observability", groups=("User",))
        )
        self.assertEqual(response["statusCode"], 403)

    def test_the_observability_path_is_not_read_as_a_deployment_id(self):
        """`/deployments/{id}/observability`가 `GET /deployments/{id}`로 흘러들면 안 된다."""
        response = handler(wired=False).handle(
            event("GET", f"/deployments/{DEPLOYMENT_ID}/observability")
        )
        self.assertEqual(json.loads(response["body"])["error"]["code"], "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
