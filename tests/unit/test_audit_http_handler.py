"""API Gateway adapter tests for GET /audit-events (Admin-only audit trail)."""

import json
import unittest

from apps.backend.api.audit import AuditEventApiService
from apps.backend.api.handler import JobHttpHandler
from apps.backend.api.jobs import JobApiService
from apps.backend.jobs import OutboxDispatcher
from packages.contracts import AuditEventPage, AuditEventType, AuditEventView


class InMemoryJobRepository:
    def __init__(self) -> None:
        self.jobs = {}
        self.outbox = []

    def create_assessment_workflow(self, assessment, job, outbox) -> None:  # pragma: no cover
        raise AssertionError("not used")

    def get_job(self, customer_id: str, job_id: str):  # pragma: no cover - unused
        return None

    def mark_outbox_dispatched(self, entry) -> None:  # pragma: no cover - unused
        return None

    def record_outbox_dispatch_failure(self, entry) -> None:  # pragma: no cover - unused
        return None


class Dispatcher:
    def dispatch(self, task) -> None:  # pragma: no cover - unused
        return None


class ApprovedScope:
    def authorize(self, principal, *, repository_id: str, policy_profile_id: str) -> None:
        return None


class AuditReader:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def list_events(self, *, customer_id, limit, cursor=None, event_type=None) -> AuditEventPage:
        self.calls.append(
            {"customer_id": customer_id, "limit": limit, "cursor": cursor, "event_type": event_type}
        )
        return AuditEventPage(
            events=(
                AuditEventView(
                    event_id="audit-001",
                    customer_id=customer_id,
                    event_type=AuditEventType.DEPLOYMENT_REQUESTED,
                    occurred_at="2026-09-03T00:00:00Z",
                    attributes={"deployment_id": "dep-001"},
                ),
            ),
            next_cursor="cursor-next",
        )


def event(method: str, path: str, *, groups=("User",), query=None) -> dict[str, object]:
    request: dict[str, object] = {
        "rawPath": path,
        "body": None,
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
    if query is not None:
        request["queryStringParameters"] = query
    return request


def _handler(reader: AuditReader) -> JobHttpHandler:
    repository = InMemoryJobRepository()
    service = JobApiService(
        repository=repository,
        assessment_scope=ApprovedScope(),
        outbox_dispatcher=OutboxDispatcher(repository=repository, dispatcher=Dispatcher()),
        job_id_factory=lambda: "job-001",
        assessment_id_factory=lambda: "asm-001",
    )
    return JobHttpHandler(service, audit_events=AuditEventApiService(reader))


class AuditHttpHandlerTest(unittest.TestCase):
    def test_admin_gets_the_audit_trail_page(self) -> None:
        reader = AuditReader()
        response = _handler(reader).handle(event("GET", "/audit-events", groups=("Admin",)))
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["events"][0]["event_type"], "DEPLOYMENT_REQUESTED")
        self.assertEqual(body["events"][0]["deployment_id"], "dep-001")
        self.assertEqual(body["next_cursor"], "cursor-next")
        self.assertEqual(reader.calls[0]["customer_id"], "cust-001")

    def test_non_admin_is_forbidden(self) -> None:
        reader = AuditReader()
        response = _handler(reader).handle(event("GET", "/audit-events", groups=("User",)))
        self.assertEqual(response["statusCode"], 403)
        self.assertEqual(reader.calls, [])

    def test_query_allow_list_is_forwarded(self) -> None:
        reader = AuditReader()
        _handler(reader).handle(
            event(
                "GET",
                "/audit-events",
                groups=("Admin",),
                query={"limit": "5", "event_type": "DEPLOYMENT_APPROVED"},
            )
        )
        self.assertEqual(reader.calls[0]["limit"], 5)
        self.assertIs(reader.calls[0]["event_type"], AuditEventType.DEPLOYMENT_APPROVED)

    def test_unknown_query_field_is_rejected(self) -> None:
        response = _handler(AuditReader()).handle(
            event("GET", "/audit-events", groups=("Admin",), query={"unexpected": "x"})
        )
        self.assertEqual(response["statusCode"], 400)

    def test_unknown_event_type_is_rejected(self) -> None:
        response = _handler(AuditReader()).handle(
            event("GET", "/audit-events", groups=("Admin",), query={"event_type": "MYSTERY"})
        )
        self.assertEqual(response["statusCode"], 400)

    def test_route_absent_without_service_is_not_found(self) -> None:
        repository = InMemoryJobRepository()
        service = JobApiService(
            repository=repository,
            assessment_scope=ApprovedScope(),
            outbox_dispatcher=OutboxDispatcher(repository=repository, dispatcher=Dispatcher()),
            job_id_factory=lambda: "job-001",
            assessment_id_factory=lambda: "asm-001",
        )
        handler = JobHttpHandler(service)
        response = handler.handle(event("GET", "/audit-events", groups=("Admin",)))
        self.assertEqual(response["statusCode"], 404)


if __name__ == "__main__":
    unittest.main()
