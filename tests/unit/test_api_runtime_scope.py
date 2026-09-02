"""Fail-closed Lambda 배포 구성과 정책 서비스 배선 테스트."""

import os
import sys
import types
import unittest
from unittest.mock import patch

from apps.backend.api.policy_approval import PolicyApprovalApiService
from apps.backend.api.policy_sources import PolicySourceApiService
from apps.backend.api.runtime import (
    EnvironmentAssessmentScope,
    _policy_approval_components,
    _policy_source_components,
)
from apps.backend.auth import Principal, Role
from apps.backend.jobs import AssessmentScopeDenied


def _fake_boto3_module() -> types.ModuleType:
    """`client`/`resource` 호출을 기록만 하는 최소 boto3 대체 모듈을 만든다."""
    module = types.ModuleType("boto3")

    def client(service_name: str) -> object:
        return types.SimpleNamespace(service_name=service_name)

    def resource(service_name: str) -> object:
        return types.SimpleNamespace(
            service_name=service_name, Table=lambda name: types.SimpleNamespace(name=name)
        )

    module.client = client  # type: ignore[attr-defined]
    module.resource = resource  # type: ignore[attr-defined]
    return module


PRINCIPAL = Principal(
    subject="user-001",
    client_id="client-001",
    customer_id="cust-001",
    roles=frozenset({Role.USER}),
)


class EnvironmentAssessmentScopeTest(unittest.TestCase):
    def test_permits_only_the_configured_customer_selector_pair(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ASSESSMENT_SCOPE_JSON": (
                    '{"cust-001":[{"repository_id":"repo-001",'
                    '"policy_profile_id":"profile-mvp-baseline"}]}'
                )
            },
            clear=True,
        ):
            scope = EnvironmentAssessmentScope.from_environment()

        self.assertIsNone(
            scope.authorize(
                PRINCIPAL, repository_id="repo-001", policy_profile_id="profile-mvp-baseline"
            )
        )
        with self.assertRaises(AssessmentScopeDenied):
            scope.authorize(
                PRINCIPAL, repository_id="repo-002", policy_profile_id="profile-mvp-baseline"
            )

    def test_missing_configuration_denies_every_selector(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            scope = EnvironmentAssessmentScope.from_environment()

        with self.assertRaises(AssessmentScopeDenied):
            scope.authorize(PRINCIPAL, repository_id="repo-001", policy_profile_id="profile-001")


class PolicySourceComponentsTest(unittest.TestCase):
    """정책 원문 업로드 서비스가 composition root에서 실제로 구성되는지 검증한다."""

    def test_builds_service_from_configured_bucket(self) -> None:
        with (
            patch.dict(
                sys.modules,
                {"boto3": _fake_boto3_module()},
            ),
            patch.dict(
                os.environ,
                {
                    "METADATA_TABLE_NAME": "metadata-table",
                    "POLICY_SOURCE_BUCKET_NAME": "policy-bucket",
                },
                clear=True,
            ),
        ):
            service, reader = _policy_source_components()

        self.assertIsInstance(service, PolicySourceApiService)
        # reader는 finalize 시 S3 get_object를 제공하는 동일 client여야 한다.
        self.assertEqual(getattr(reader, "service_name", None), "s3")

    def test_missing_bucket_fails_closed(self) -> None:
        with (
            patch.dict(sys.modules, {"boto3": _fake_boto3_module()}),
            patch.dict(os.environ, {"METADATA_TABLE_NAME": "metadata-table"}, clear=True),
        ):
            with self.assertRaises(ValueError):
                _policy_source_components()


class PolicyApprovalComponentsTest(unittest.TestCase):
    """정책 승인·게시 서비스가 composition root에서 실제로 구성되는지 검증한다."""

    def test_builds_service_from_metadata_table(self) -> None:
        with (
            patch.dict(sys.modules, {"boto3": _fake_boto3_module()}),
            patch.dict(os.environ, {"METADATA_TABLE_NAME": "metadata-table"}, clear=True),
        ):
            service = _policy_approval_components()

        self.assertIsInstance(service, PolicyApprovalApiService)

    def test_missing_metadata_table_fails_closed(self) -> None:
        with (
            patch.dict(sys.modules, {"boto3": _fake_boto3_module()}),
            patch.dict(os.environ, {}, clear=True),
        ):
            with self.assertRaises(ValueError):
                _policy_approval_components()
