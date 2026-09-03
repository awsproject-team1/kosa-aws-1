"""AWS Lambda composition root for the M0 authenticated Job API and Outbox sweeper."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

from apps.backend.api.assessments import AssessmentReportApiService
from apps.backend.api.audit_events import AuditEventApiService
from apps.backend.api.deployments import DeploymentApiService
from apps.backend.api.handler import JobHttpHandler
from apps.backend.api.jobs import AssessmentScope, JobApiService
from apps.backend.api.policy_approval import PolicyApprovalApiService
from apps.backend.api.policy_sources import PolicySourceApiService
from apps.backend.api.remediation_exceptions import RemediationExceptionApiService
from apps.backend.assessment import DynamoDbAssessmentReportStore
from apps.backend.auth import Principal
from apps.backend.deployment import DeploymentApprovalService
from apps.backend.deployment.runtime_config import (
    DeploymentRuntimeConfiguration,
    DeploymentRuntimeConfigurationError,
)
from apps.backend.jobs import (
    AssessmentScopeDenied,
    OutboxDispatcher,
    SqsDeploymentWorkflowDispatcher,
    SqsWorkflowDispatcher,
)
from apps.backend.repositories import (
    DynamoDbAssessmentWorkflowRepository,
    DynamoDbAuditEventRepository,
    DynamoDbDeploymentApprovalRepository,
    DynamoDbDeploymentRepository,
    DynamoDbPolicyApprovalRepository,
    DynamoDbRemediationExceptionRepository,
)
from apps.backend.repositories.deployment_source import DynamoDbDeploymentSourceReader
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
        assessment_reports=AssessmentReportApiService(
            jobs=repository,
            reports=reports,
            # Join the customer's in-force exceptions as read-time suppression
            # notes (ADR-0020 §6). Read-only: only list_exceptions is used.
            exceptions=_remediation_exception_reader(),
            now=lambda: datetime.now(UTC),
        ),
        deployments=_deployment_components(repository),
        policy_sources=policy_sources,
        policy_approvals=_policy_approval_components(),
        # 감사 이력은 읽기 전용이고 principal의 customer partition으로만 조회한다.
        audit_events=AuditEventApiService(events=DynamoDbAuditEventRepository(_metadata_table())),
        policy_reader=policy_reader,
        remediation_exceptions=_remediation_exception_components(),
    )


def _deployment_components(
    workflow_repository: DynamoDbAssessmentWorkflowRepository,
) -> DeploymentApiService:
    """Wire the deployment creation and reject paths to their durable stores.

    Create and reject persist through the deployment record store and the Job
    store; creation also reads the remediation's stored decision and worker result
    through the deployment source reader. The approve/get/verification reader
    assemblers depend on D's live plan and verification data and arrive with the D
    live adapter integration; until then those routes fail closed. The
    RUN_DEPLOYMENT outbox reuses the workflow repository's outbox bookkeeping and
    is delivered to the Deployment Worker queue.
    """
    try:
        import boto3
    except ImportError as error:  # pragma: no cover - boto3 is provided by Lambda runtime.
        raise RuntimeError("AWS Lambda boto3 runtime is required") from error
    table_name = _required_string(os.environ.get("METADATA_TABLE_NAME"), "METADATA_TABLE_NAME")
    queue_url = _required_string(os.environ.get("DEPLOYMENT_QUEUE_URL"), "DEPLOYMENT_QUEUE_URL")
    deployment_repository = DynamoDbDeploymentRepository(
        table=_metadata_table(),
        table_name=table_name,
        transaction_client=boto3.client("dynamodb"),
    )
    dispatcher = SqsDeploymentWorkflowDispatcher(boto3.client("sqs"), queue_url=queue_url)
    approval_repository = DynamoDbDeploymentApprovalRepository(
        table_name=table_name, transaction_client=boto3.client("dynamodb")
    )
    return DeploymentApiService(
        approvals=DeploymentApprovalService(approval_repository),
        sources=DynamoDbDeploymentSourceReader(
            _metadata_table(), commits=_deployment_commit_resolver()
        ),
        deployments=deployment_repository,
        jobs=workflow_repository,
        outbox_dispatcher=OutboxDispatcher(repository=workflow_repository, dispatcher=dispatcher),
        deployment_id_factory=lambda: f"dep-{uuid.uuid4()}",
        job_id_factory=lambda: f"job-{uuid.uuid4()}",
        now=lambda: datetime.now(UTC),
    )


def _remediation_exception_components() -> RemediationExceptionApiService:
    """고객 Remediation 예외 등록 서비스를 구성한다.

    예외는 `(customer_id, rule_id, rule_version)`에 묶이고 반드시 만료되며 관리자만 등록한다.
    예외 record와 audit event를 한 transaction으로 쓰므로 low-level `transaction_client`와
    resource `table`을 함께 주입한다.
    """
    try:
        import boto3
    except ImportError as error:  # pragma: no cover - boto3는 Lambda 런타임이 제공한다.
        raise RuntimeError("AWS Lambda boto3 runtime is required") from error
    table_name = _required_string(os.environ.get("METADATA_TABLE_NAME"), "METADATA_TABLE_NAME")
    repository = DynamoDbRemediationExceptionRepository(
        boto3.resource("dynamodb").Table(table_name),
        table_name=table_name,
        transaction_client=boto3.client("dynamodb"),
    )
    return RemediationExceptionApiService(
        repository=repository,
        exception_id_factory=lambda: f"rex-{uuid.uuid4()}",
        now=lambda: datetime.now(UTC),
    )


def _remediation_exception_reader() -> DynamoDbRemediationExceptionRepository:
    """Construct the read-only exception view used for read-time suppression.

    `list_exceptions` only queries the resource table, but the repository's
    constructor also requires a transaction client for its write path; we pass a
    client so the same durable type serves both. The report read path calls only
    `list_exceptions` (ADR-0020 §6).
    """
    try:
        import boto3
    except ImportError as error:  # pragma: no cover - boto3는 Lambda 런타임이 제공한다.
        raise RuntimeError("AWS Lambda boto3 runtime is required") from error
    table_name = _required_string(os.environ.get("METADATA_TABLE_NAME"), "METADATA_TABLE_NAME")
    return DynamoDbRemediationExceptionRepository(
        boto3.resource("dynamodb").Table(table_name),
        table_name=table_name,
        transaction_client=boto3.client("dynamodb"),
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


def _policy_approval_components() -> PolicyApprovalApiService:
    """정책 Source 승인·Profile 게시 서비스를 구성한다.

    write(승인 record·Profile)는 low-level `transaction_client`로 조건부 transaction을 쓰고,
    read(`load_review`/`load_publication`)는 자동 un/marshal되는 resource `table`로 읽으므로
    같은 metadata table을 두 형태로 주입한다.
    """
    try:
        import boto3
    except ImportError as error:  # pragma: no cover - boto3는 Lambda 런타임이 제공한다.
        raise RuntimeError("AWS Lambda boto3 runtime is required") from error
    table_name = _required_string(os.environ.get("METADATA_TABLE_NAME"), "METADATA_TABLE_NAME")
    repository = DynamoDbPolicyApprovalRepository(
        table_name=table_name,
        transaction_client=boto3.client("dynamodb"),
        table=_metadata_table(),
    )
    return PolicyApprovalApiService(repository)


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


class ConfiguredDeploymentCommitResolver:
    """Dispatch commit resolution to the adapter for the requested approved target.

    `LiveDeploymentCommitResolver` is bound to one `(customer_id, repository_id)`, the
    same shape as D's other live adapters. Deployment creation, though, resolves the
    repository from the stored remediation, so the composition root does the lookup and
    hands the request to the adapter for that exact scope. A target outside the approved
    configuration is refused rather than resolved with some other target's token.
    """

    def __init__(self, configuration: DeploymentRuntimeConfiguration) -> None:
        self._configuration = configuration

    def resolve_default_branch_commit(
        self, *, customer_id: str, repository_id: str, patch: object
    ) -> str | None:
        from agent.runtime.live_deployment_commit_resolver import LiveDeploymentCommitResolver

        target = self._configuration.resolve(customer_id=customer_id, repository_id=repository_id)
        resolver = LiveDeploymentCommitResolver(
            customer_id=target.customer_id,
            repository_id=target.repository_id,
            repository_full_name=target.repository_full_name,
            token_provider=lambda: _secret_value(target.github_token_secret_id),
        )
        return resolver.resolve_default_branch_commit(
            customer_id=customer_id, repository_id=repository_id, patch=patch
        )


class UnconfiguredDeploymentCommitResolver:
    """Refuse to resolve a merge commit when no approved target is configured.

    A `TERRAFORM_PATCH` deployment must apply the merge commit on the default branch
    (ADR-0019 §3), and without GitHub configuration that commit cannot be observed.
    Refusing keeps the missing configuration visible instead of letting the base commit
    stand in for code no human merged. `ACTUAL_SYNC` never reaches this resolver, so a
    stack deployed without GitHub configuration still creates sync deployments.
    """

    def resolve_default_branch_commit(
        self, *, customer_id: str, repository_id: str, patch: object
    ) -> str | None:
        raise DeploymentRuntimeConfigurationError(
            "deployment commit resolution is not configured for this deployment"
        )


def _deployment_commit_resolver() -> object:
    raw = os.environ.get("DEPLOYMENT_RUNTIME_JSON")
    if not raw or not raw.strip():
        return UnconfiguredDeploymentCommitResolver()
    return ConfiguredDeploymentCommitResolver(DeploymentRuntimeConfiguration.from_json(raw))


def _secret_value(secret_id: str) -> str:
    try:
        import boto3
    except ImportError as error:  # pragma: no cover - boto3 is provided by Lambda runtime.
        raise RuntimeError("AWS Lambda boto3 runtime is required") from error
    response = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)
    return _required_string(response.get("SecretString"), "SecretString")


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
