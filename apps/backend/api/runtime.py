"""AWS Lambda composition root for the M0 authenticated Job API and Outbox sweeper."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping

from apps.backend.api.assessments import AssessmentReportApiService
from apps.backend.api.handler import JobHttpHandler
from apps.backend.api.jobs import AssessmentScope, JobApiService
from apps.backend.api.policy_sources import PolicySourceApiService
from apps.backend.assessment import DynamoDbAssessmentReportStore
from apps.backend.auth import Principal
from apps.backend.jobs import AssessmentScopeDenied, OutboxDispatcher, SqsWorkflowDispatcher
from apps.backend.repositories import DynamoDbAssessmentWorkflowRepository
from apps.backend.repositories.policy_ingestion import DynamoDbPolicySourceUploadRepository


class EnvironmentAssessmentScope(AssessmentScope):
    """Fail-closed deployment configuration for approved customer selectors."""

    def __init__(self, configured_scopes: Mapping[str, frozenset[tuple[str, str]]]) -> None:
        self._configured_scopes = configured_scopes

    @classmethod
    def from_environment(cls) -> EnvironmentAssessmentScope:
        raw = os.environ.get("ASSESSMENT_SCOPE_JSON")
        if not raw:
            return cls({})
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, Mapping):
                raise ValueError
            scopes = {
                _required_string(customer_id, "customer_id"): _selector_pairs(entries)
                for customer_id, entries in parsed.items()
            }
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("ASSESSMENT_SCOPE_JSON is invalid") from error
        return cls(scopes)

    def authorize(
        self, principal: Principal, *, repository_id: str, policy_profile_id: str
    ) -> None:
        if (repository_id, policy_profile_id) not in self._configured_scopes.get(
            principal.customer_id, frozenset()
        ):
            raise AssessmentScopeDenied("assessment selectors are outside configured scope")


def lambda_handler(event: Mapping[str, object], context: object) -> dict[str, object]:
    """API Gateway proxy entrypoint; Cognito JWT claims are validated by the HTTP handler."""
    return _http_handler().handle(event)


def outbox_sweeper_handler(event: object, context: object) -> dict[str, int]:
    """EventBridge-scheduled at-least-once dispatch of durable Assessment tasks."""
    repository, dispatcher = _workflow_components()
    dispatched = OutboxDispatcher(repository=repository, dispatcher=dispatcher).dispatch_pending()
    return {"dispatched": dispatched}


def _http_handler() -> JobHttpHandler:
    repository, dispatcher = _workflow_components()
    service = JobApiService(
        repository=repository,
        assessment_scope=EnvironmentAssessmentScope.from_environment(),
        outbox_dispatcher=OutboxDispatcher(repository=repository, dispatcher=dispatcher),
        job_id_factory=lambda: f"job-{uuid.uuid4()}",
        assessment_id_factory=lambda: f"asm-{uuid.uuid4()}",
    )
    reports = DynamoDbAssessmentReportStore(_metadata_table())
    policy_sources, policy_reader = _policy_source_components()
    return JobHttpHandler(
        service,
        assessment_reports=AssessmentReportApiService(jobs=repository, reports=reports),
        policy_sources=policy_sources,
        policy_reader=policy_reader,
    )


def _policy_source_components() -> tuple[PolicySourceApiService, object]:
    """고객 정책 원문 업로드 세션 서비스와 정규화 처리용 S3 reader를 구성한다.

    업로드 세션 리포지토리는 tenant-scoped S3 key를 서버가 발급하므로 client는
    저장 위치를 고를 수 없다. presigner와 reader는 같은 S3 client를 재사용한다
    (presigned URL 발급, 정규화 산출물 put, finalize 시 원문 get).
    """
    try:
        import boto3
    except ImportError as error:  # pragma: no cover - boto3는 Lambda 런타임이 제공한다.
        raise RuntimeError("AWS Lambda boto3 runtime is required") from error
    bucket = _required_string(
        os.environ.get("POLICY_SOURCE_BUCKET_NAME"), "POLICY_SOURCE_BUCKET_NAME"
    )
    s3_client = boto3.client("s3")
    repository = DynamoDbPolicySourceUploadRepository(
        table=_metadata_table(), bucket=bucket, presigner=s3_client
    )
    service = PolicySourceApiService(
        repository=repository,
        source_id_factory=lambda: f"src-{uuid.uuid4()}",
        source_version_factory=lambda: f"ver-{uuid.uuid4()}",
    )
    return service, s3_client


def _workflow_components() -> tuple[DynamoDbAssessmentWorkflowRepository, SqsWorkflowDispatcher]:
    try:
        import boto3
    except ImportError as error:  # pragma: no cover - boto3 is provided by Lambda runtime.
        raise RuntimeError("AWS Lambda boto3 runtime is required") from error
    table_name = _required_string(os.environ.get("METADATA_TABLE_NAME"), "METADATA_TABLE_NAME")
    queue_url = _required_string(os.environ.get("ASSESSMENT_QUEUE_URL"), "ASSESSMENT_QUEUE_URL")
    dynamodb = boto3.resource("dynamodb")
    return (
        DynamoDbAssessmentWorkflowRepository(
            dynamodb.Table(table_name),
            table_name=table_name,
            transaction_client=boto3.client("dynamodb"),
        ),
        SqsWorkflowDispatcher(boto3.client("sqs"), queue_url=queue_url),
    )


def _metadata_table() -> object:
    try:
        import boto3
    except ImportError as error:  # pragma: no cover - boto3 is provided by Lambda runtime.
        raise RuntimeError("AWS Lambda boto3 runtime is required") from error
    table_name = _required_string(os.environ.get("METADATA_TABLE_NAME"), "METADATA_TABLE_NAME")
    return boto3.resource("dynamodb").Table(table_name)


def _selector_pairs(value: object) -> frozenset[tuple[str, str]]:
    if not isinstance(value, list):
        raise ValueError
    pairs: set[tuple[str, str]] = set()
    for entry in value:
        if not isinstance(entry, Mapping):
            raise ValueError
        pairs.add(
            (
                _required_string(entry.get("repository_id"), "repository_id"),
                _required_string(entry.get("policy_profile_id"), "policy_profile_id"),
            )
        )
    return frozenset(pairs)


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value
