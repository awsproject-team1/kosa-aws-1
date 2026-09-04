"""Composition roots must actually assemble — with the real wiring, not injected fakes.

이 저장소의 테스트는 대부분 service·worker에 fake port를 주입한다. 그래서 production 조립
(`apps/backend/*/runtime.py`)이 어떤 service를 빠뜨려도 잡히지 않았다 — 실제로
`POST /findings/{id}/remediations`는 route와 handler branch가 있는데도 composition root가
`RemediationApiService`를 만들지 않아 배포 환경에서 항상 404였다(2026-09-03 검토 D1).

이 테스트는 boto3를 client·resource 호출만 흉내 내는 stub으로 바꿔 끼우고, 실제 composition
함수를 그대로 호출해 **모든 경로가 조립되는지**만 확인한다. AWS를 부르지 않으므로 어떤 자격
증명도 필요 없다. 새 endpoint나 worker port를 추가했으면 여기에 한 줄을 더하는 것이 규칙이다.
"""

import json
import os
import sys
import unittest
from types import ModuleType
from unittest import mock


class _Stub:
    """Absorbs any attribute access or call without touching AWS."""

    def __init__(self, name: str = "stub") -> None:
        self._name = name

    def __getattr__(self, item: str) -> "_Stub":
        return _Stub(f"{self._name}.{item}")

    def __call__(self, *args: object, **kwargs: object) -> "_Stub":
        return _Stub(f"{self._name}()")

    def get(self, *args: object, **kwargs: object) -> None:  # Mapping-like reads stay empty
        return None


class _FakeBoto3(ModuleType):
    def __init__(self) -> None:
        super().__init__("boto3")
        self.clients: list[str] = []

    def client(self, service: str, **kwargs: object) -> _Stub:
        self.clients.append(service)
        return _Stub(f"client:{service}")

    def resource(self, service: str, **kwargs: object) -> _Stub:
        return _Stub(f"resource:{service}")


DEPLOYMENT_TARGET = {
    "customer_id": "cust-001",
    "repository_id": "repo-001",
    "repository_full_name": "acme/iac",
    "github_token_secret_id": "arn:aws:secretsmanager:us-east-1:111122223333:secret:gh",
    "aws_account_id": "111122223333",
    "aws_read_role_arn": "arn:aws:iam::111122223333:role/read",
    "aws_external_id_secret_id": "arn:aws:secretsmanager:us-east-1:111122223333:secret:ext",
    "resource_types": ["AWS::S3::Bucket"],
}

API_ENVIRONMENT = {
    "METADATA_TABLE_NAME": "metadata",
    "ASSESSMENT_QUEUE_URL": "https://sqs.example/assessment",
    "REMEDIATION_QUEUE_URL": "https://sqs.example/remediation",
    "DEPLOYMENT_QUEUE_URL": "https://sqs.example/deployment",
    "POLICY_SOURCE_BUCKET_NAME": "policy-sources",
    "POLICY_AUTHORING_QUEUE_URL": "https://sqs.example/authoring",
    "DEPLOYMENT_RUNTIME_JSON": json.dumps([DEPLOYMENT_TARGET]),
}


class CompositionSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.boto3 = _FakeBoto3()
        self._modules = mock.patch.dict(sys.modules, {"boto3": self.boto3})
        self._modules.start()
        self.addCleanup(self._modules.stop)

    def test_api_lambda_composes_every_handler_dependency(self) -> None:
        from apps.backend.api.runtime import _http_handler

        with mock.patch.dict(os.environ, API_ENVIRONMENT, clear=False):
            handler = _http_handler()

        # 각 endpoint 계열의 service가 None이면 handler는 그 경로를 404로 답한다.
        for attribute in (
            "_remediations",
            "_deployments",
            "_policy_sources",
            "_policy_approvals",
            "_policy_candidates",
            "_remediation_exceptions",
            "_audit_events",
            # Parent Q&A grounding is composed here too (POLICY_SOURCE_BUCKET_NAME is set above).
            "_orchestrations",
        ):
            with self.subTest(attribute=attribute):
                self.assertIsNotNone(
                    getattr(handler, attribute), f"{attribute} is not composed in api runtime"
                )

    def test_remediation_worker_composes_the_pull_request_port_when_configured(self) -> None:
        from apps.backend.remediation.runtime import _live_worker

        with mock.patch.dict(os.environ, API_ENVIRONMENT, clear=False):
            worker = _live_worker()
        self.assertIsNotNone(worker._pull_request_action)

    def test_remediation_worker_without_deployment_scope_has_no_pull_request_port(self) -> None:
        from apps.backend.remediation.runtime import _live_worker

        environment = {k: v for k, v in API_ENVIRONMENT.items() if k != "DEPLOYMENT_RUNTIME_JSON"}
        with mock.patch.dict(os.environ, environment, clear=True):
            worker = _live_worker()
        self.assertIsNone(worker._pull_request_action)

    def test_outbox_sweeper_routes_every_workflow_command(self) -> None:
        from apps.backend.api.runtime import _all_command_dispatcher
        from packages.contracts import WorkflowCommand, WorkflowTask

        with mock.patch.dict(os.environ, API_ENVIRONMENT, clear=False):
            dispatcher = _all_command_dispatcher()
        # ASSESS_RESOURCE, GENERATE_REMEDIATION, RUN_DEPLOYMENT가 모두 큐를 갖는다. 하나라도
        # 빠지면 sweeper가 그 command의 outbox를 영원히 PENDING으로 남긴다(#65가 고친 결함).
        for command in (
            WorkflowCommand.ASSESS_RESOURCE,
            WorkflowCommand.GENERATE_REMEDIATION,
            WorkflowCommand.SYNC_ACTUAL_STATE,
            WorkflowCommand.RUN_DEPLOYMENT,
        ):
            with self.subTest(command=command.value):
                dispatcher.dispatch(
                    WorkflowTask(job_id="job-1", expected_revision=0, command=command)
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
