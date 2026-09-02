"""Security: Admin-only deployment write/read routes reject non-admin principals.

승인·거절·감사 이력 조회는 관리자만 할 수 있다(ADR-0019 §4·§8). 이 테스트는 authorize()
순수 함수 수준과 HTTP handler 라우트 수준 양쪽에서, User 역할이 관리자 전용 동작을 시도하면
차단(403)되고 서비스가 아예 호출되지 않는지 fail-closed로 고정한다.
"""

import json
import unittest

from apps.backend.api.audit import AuditEventApiService
from apps.backend.api.deployments import DeploymentApiService
from apps.backend.api.handler import JobHttpHandler
from apps.backend.api.jobs import JobApiService
from apps.backend.auth import Action, AuthorizationDenied, Principal, Role, authorize
from apps.backend.deployment import DeploymentApprovalService
from apps.backend.jobs import OutboxDispatcher
from packages.contracts import AuditEventPage

CUSTOMER = "cust-001"
DEPLOYMENT = "deployment-001"
REMEDIATION = "remediation-001"


def _principal(role: Role) -> Principal:
    return Principal(
        subject="subject-001",
        client_id="client-001",
        customer_id=CUSTOMER,
        roles=frozenset({role}),
    )


class ExplodingApprovalRepo:
    def record_approval(self, *, customer_id, approval, readiness) -> None:  # pragma: no cover
        raise AssertionError("write must not be reached without authorization")


class ExplodingDeploymentRepo:
    def get_deployment_source(self, *, customer_id, remediation_id):  # pragma: no cover
        raise AssertionError("must not be reached without authorization")

    def create_deployment(self, record, *, job, outbox) -> None:  # pragma: no cover
        raise AssertionError("must not be reached without authorization")

    def get_deployment(self, *, customer_id, deployment_id):  # pragma: no cover
        raise AssertionError("must not be reached without authorization")


class ExplodingPlanReader:
    def get_approval_input(self, *, customer_id, deployment_id):  # pragma: no cover
        raise AssertionError("plan read must not be reached without authorization")


class ExplodingAuditReader:
    def list_events(self, *, customer_id, limit, cursor=None, event_type=None) -> AuditEventPage:
        raise AssertionError("audit read must not be reached without authorization")


class Dispatcher:
    def dispatch(self, task) -> None:  # pragma: no cover - unused
        return None


class ApprovedScope:
    def authorize(self, principal, *, repository_id, policy_profile_id) -> None:
        return None


class JobStore:
    def get_job(self, customer_id, job_id):  # pragma: no cover - unused
        return None

    def mark_outbox_dispatched(self, entry) -> None:  # pragma: no cover - unused
        return None

    def record_outbox_dispatch_failure(self, entry) -> None:  # pragma: no cover - unused
        return None


def _handler() -> JobHttpHandler:
    jobs = JobStore()
    deployment_service = DeploymentApiService(
        approvals=DeploymentApprovalService(ExplodingApprovalRepo()),
        plans=ExplodingPlanReader(),
        sources=ExplodingDeploymentRepo(),
        deployments=ExplodingDeploymentRepo(),
        jobs=jobs,
        outbox_dispatcher=OutboxDispatcher(repository=jobs, dispatcher=Dispatcher()),
        deployment_id_factory=lambda: DEPLOYMENT,
        job_id_factory=lambda: "job-001",
        now=lambda: _FixedNow(),
    )
    job_service = JobApiService(
        repository=jobs,
        assessment_scope=ApprovedScope(),
        outbox_dispatcher=OutboxDispatcher(repository=jobs, dispatcher=Dispatcher()),
        job_id_factory=lambda: "job-001",
        assessment_id_factory=lambda: "asm-001",
    )
    return JobHttpHandler(
        job_service,
        deployments=deployment_service,
        audit_events=AuditEventApiService(ExplodingAuditReader()),
    )


class _FixedNow:
    def isoformat(self) -> str:  # pragma: no cover - reject path only
        return "2026-09-03T02:00:00+00:00"


def event(method: str, path: str, *, groups=("User",), body=None) -> dict:
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
                        "custom:customer_id": CUSTOMER,
                        "cognito:groups": list(groups),
                    }
                }
            },
        },
    }


class DeploymentAuthorizationBoundaryTest(unittest.TestCase):
    def test_admin_only_actions_are_denied_to_a_user_principal(self) -> None:
        user = _principal(Role.USER)
        for action in (
            Action.APPROVE_DEPLOYMENT,
            Action.REJECT_DEPLOYMENT,
            Action.READ_AUDIT_EVENTS,
        ):
            with self.subTest(action=action):
                with self.assertRaises(AuthorizationDenied):
                    authorize(user, action)

    def test_admin_holds_the_admin_only_actions(self) -> None:
        admin = _principal(Role.ADMIN)
        for action in (
            Action.APPROVE_DEPLOYMENT,
            Action.REJECT_DEPLOYMENT,
            Action.READ_AUDIT_EVENTS,
        ):
            with self.subTest(action=action):
                authorize(admin, action)  # does not raise

    def test_user_cannot_approve_a_deployment_and_write_is_not_reached(self) -> None:
        response = _handler().handle(
            event(
                "POST",
                f"/deployments/{DEPLOYMENT}/approve",
                groups=("User",),
                body=json.dumps({"commit_sha": "commit-001", "plan_hash": "plan-001"}),
            )
        )
        self.assertEqual(response["statusCode"], 403)
        self.assertEqual(json.loads(response["body"])["error"]["code"], "SCOPE_DENIED")

    def test_user_cannot_reject_a_deployment(self) -> None:
        response = _handler().handle(
            event(
                "POST",
                f"/deployments/{DEPLOYMENT}/reject",
                groups=("User",),
                body=json.dumps({"reason": "SUPERSEDED"}),
            )
        )
        self.assertEqual(response["statusCode"], 403)

    def test_user_cannot_read_the_audit_trail(self) -> None:
        response = _handler().handle(event("GET", "/audit-events", groups=("User",)))
        self.assertEqual(response["statusCode"], 403)

    def test_external_group_cannot_reach_admin_actions(self) -> None:
        # 알 수 없는 그룹은 그 어떤 제품 역할도 얻지 못하므로 인증 자체가 거부된다.
        response = _handler().handle(
            event(
                "POST",
                f"/deployments/{DEPLOYMENT}/approve",
                groups=("Operator",),
                body=json.dumps({"commit_sha": "commit-001", "plan_hash": "plan-001"}),
            )
        )
        self.assertIn(response["statusCode"], (401, 403))


if __name__ == "__main__":
    unittest.main()
