"""`GET /audit-events` HTTP 경계 테스트 (M2 A).

고정하는 불변식:
- 서비스가 주입되지 않은 배포에서는 route 자체가 없다(404). 배선 누락이 200으로 보이지 않는다.
- query는 handler가 해석하지 않고 서비스의 검증 경계로 그대로 넘어간다.
- Admin이 아니면 403이고, 잘못된 query는 400이다.
"""

import json
import unittest

from apps.backend.api.audit_events import AuditEventApiService
from apps.backend.api.handler import JobHttpHandler
from apps.backend.api.jobs import JobApiService
from apps.backend.jobs import OutboxDispatcher
from packages.contracts import AuditEvent, AuditEventPage, AuditEventType

EVENT = AuditEvent(
    event_id="audit-001",
    event_type=AuditEventType.DEPLOYMENT_REQUESTED,
    occurred_at="2026-09-02T10:00:00Z",
    customer_id="cust-001",
    details={"deployment_id": "dep-001"},
)


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


class Events:
    def __init__(self):
        self.calls = []

    def list_events(self, *, customer_id, limit, cursor=None, event_type=None):
        self.calls.append(
            {
                "customer_id": customer_id,
                "limit": limit,
                "cursor": cursor,
                "event_type": event_type,
            }
        )
        return AuditEventPage(events=(EVENT,), next_cursor="next-page")


def event(method, path, *, groups=("Admin",), query=None):
    return {
        "rawPath": path,
        "queryStringParameters": query,
        "requestContext": {
            "http": {"method": method},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "token_use": "access",
                        "sub": "subject-001",
                        "client_id": "client-001",
                        "custom:customer_id": "cust-001",
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
    events = Events()
    audit_events = AuditEventApiService(events=events) if wired else None
    return JobHttpHandler(jobs, audit_events=audit_events), events


class AuditEventsHttpHandlerTest(unittest.TestCase):
    def test_returns_a_page_of_audit_events(self):
        http, events = handler()
        response = http.handle(event("GET", "/audit-events"))
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["next_cursor"], "next-page")
        self.assertEqual(body["events"][0]["event_type"], "DEPLOYMENT_REQUESTED")
        self.assertEqual(body["events"][0]["details"], {"deployment_id": "dep-001"})
        self.assertEqual(events.calls[0]["customer_id"], "cust-001")

    def test_passes_the_query_through_to_the_service(self):
        http, events = handler()
        http.handle(
            event(
                "GET",
                "/audit-events",
                query={"limit": "5", "cursor": "abc", "event_type": "DEPLOYMENT_APPROVED"},
            )
        )
        call = events.calls[0]
        self.assertEqual(call["limit"], 5)
        self.assertEqual(call["cursor"], "abc")
        self.assertIs(call["event_type"], AuditEventType.DEPLOYMENT_APPROVED)

    def test_unwired_deployment_has_no_route(self):
        http, _ = handler(wired=False)
        self.assertEqual(http.handle(event("GET", "/audit-events"))["statusCode"], 404)

    def test_non_admin_is_forbidden(self):
        http, _ = handler()
        response = http.handle(event("GET", "/audit-events", groups=("User",)))
        self.assertEqual(response["statusCode"], 403)

    def test_invalid_query_is_a_request_error(self):
        http, _ = handler()
        response = http.handle(event("GET", "/audit-events", query={"limit": "0"}))
        self.assertEqual(response["statusCode"], 400)


if __name__ == "__main__":
    unittest.main()
