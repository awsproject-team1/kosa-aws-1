"""AWS Lambda composition root for the M0 authenticated Job API and Outbox sweeper."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from apps.backend.api.assessments import AssessmentReportApiService
from apps.backend.api.audit_events import AuditEventApiService
from apps.backend.api.deployments import DeploymentApiService
from apps.backend.api.handler import JobHttpHandler
from apps.backend.api.jobs import AssessmentScope, JobApiService
from apps.backend.api.observability import DemoRunObservabilityService
from apps.backend.api.policy_approval import PolicyApprovalApiService
from apps.backend.api.policy_candidates import PolicyCandidateApiService
from apps.backend.api.policy_sources import PolicySourceApiService
from apps.backend.api.remediation_exceptions import RemediationExceptionApiService
from apps.backend.api.remediation_reads import RemediationReadApiService
from apps.backend.api.remediations import RemediationApiService
from apps.backend.api.scope import SCOPE_ENTRY_FIELDS
from apps.backend.assessment import DynamoDbAssessmentReportStore
from apps.backend.auth import Principal
from apps.backend.deployment import DeploymentApprovalService
from apps.backend.deployment.completion import (
    ApplyCompletionService,
    parse_completion_event,
)
from apps.backend.deployment.runtime_config import (
    DeploymentRuntimeConfiguration,
    DeploymentRuntimeConfigurationError,
)
from apps.backend.jobs import (
    AssessmentScopeDenied,
    CommandRoutingWorkflowDispatcher,
    OutboxDispatcher,
    SqsDeploymentWorkflowDispatcher,
    SqsPolicyAuthoringDispatcher,
    SqsRemediationWorkflowDispatcher,
    SqsWorkflowDispatcher,
)
from apps.backend.policy import DynamoDbPolicyCatalog, load_rule_registry
from apps.backend.repositories import (
    DynamoDbAssessmentWorkflowRepository,
    DynamoDbAuditEventRepository,
    DynamoDbDeploymentApprovalRepository,
    DynamoDbDeploymentRepository,
    DynamoDbPolicyApprovalRepository,
    DynamoDbRemediationContextReader,
    DynamoDbRemediationExceptionRepository,
)
from apps.backend.repositories.comparison_input import DynamoDbComparisonInputReader
from apps.backend.repositories.deployment_completion import (
    DynamoDbDeploymentCompletionStore,
)
from apps.backend.repositories.deployment_facts import DynamoDbDeploymentFactsReader
from apps.backend.repositories.deployment_plan import DynamoDbDeploymentPlanReader
from apps.backend.repositories.deployment_source import DynamoDbDeploymentSourceReader
from apps.backend.repositories.policy_ingestion import DynamoDbPolicySourceUploadRepository
from apps.backend.repositories.remediation_read import DynamoDbRemediationReadRepository
from packages.contracts import WorkflowCommand


class EnvironmentAssessmentScope(AssessmentScope):
    """Fail-closed deployment configuration for the repositories a customer may assess.

    **Policy Profile은 여기서 판정하지 않는다.** 환경변수 allow-list에 Profile을 고정하면, 고객이
    정책을 승인·게시할 때마다 인프라 배포가 필요해진다 — 승인 직후 평가에 쓸 수 있어야 한다는
    목표와 충돌한다. 어떤 Profile을 쓸 수 있는지는 고객 partition의 Catalog가 답한다.
    """

    def __init__(self, configured_repositories: Mapping[str, frozenset[str]]) -> None:
        self._configured_repositories = configured_repositories

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
                _required_string(customer_id, "customer_id"): _repository_ids(entries)
                for customer_id, entries in parsed.items()
            }
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("ASSESSMENT_SCOPE_JSON is invalid") from error
        return cls(scopes)

    def authorize(self, principal: Principal, *, repository_id: str) -> None:
        if repository_id not in self._configured_repositories.get(
            principal.customer_id, frozenset()
        ):
            raise AssessmentScopeDenied("assessment repository is outside configured scope")


def lambda_handler(event: Mapping[str, object], context: object) -> dict[str, object]:
    """API Gateway proxy entrypoint; Cognito JWT claims are validated by the HTTP handler."""
    return _http_handler().handle(event)


def outbox_sweeper_handler(event: object, context: object) -> dict[str, int]:
    """EventBridge-scheduled at-least-once dispatch of every durable workflow task.

    The Outbox holds Assessment, Remediation, and Deployment commands in one table, so
    the sweeper must route each task to the queue its command belongs to rather than
    assume every entry is an Assessment task. A single-queue dispatcher would leave
    Remediation/Deployment entries PENDING forever.
    """
    repository, _ = _workflow_components()
    dispatched = OutboxDispatcher(
        repository=repository, dispatcher=_all_command_dispatcher()
    ).dispatch_pending()
    return {"dispatched": dispatched}


def _all_command_dispatcher() -> CommandRoutingWorkflowDispatcher:
    """Build a dispatcher that routes each workflow command to its own internal queue."""
    try:
        import boto3
    except ImportError as error:  # pragma: no cover - boto3 is provided by Lambda runtime.
        raise RuntimeError("AWS Lambda boto3 runtime is required") from error
    sqs = boto3.client("sqs")
    assessment_url = _required_string(
        os.environ.get("ASSESSMENT_QUEUE_URL"), "ASSESSMENT_QUEUE_URL"
    )
    remediation_url = _required_string(
        os.environ.get("REMEDIATION_QUEUE_URL"), "REMEDIATION_QUEUE_URL"
    )
    deployment_url = _required_string(
        os.environ.get("DEPLOYMENT_QUEUE_URL"), "DEPLOYMENT_QUEUE_URL"
    )
    return CommandRoutingWorkflowDispatcher(
        {
            WorkflowCommand.ASSESS_RESOURCE: SqsWorkflowDispatcher(sqs, queue_url=assessment_url),
            WorkflowCommand.GENERATE_REMEDIATION: SqsRemediationWorkflowDispatcher(
                sqs, queue_url=remediation_url
            ),
            WorkflowCommand.SYNC_ACTUAL_STATE: SqsRemediationWorkflowDispatcher(
                sqs, queue_url=remediation_url
            ),
            WorkflowCommand.RUN_DEPLOYMENT: SqsDeploymentWorkflowDispatcher(
                sqs, queue_url=deployment_url
            ),
        }
    )


def apply_completion_handler(event: Mapping[str, object], context: object) -> dict[str, str]:
    """EventBridge entrypoint reserving one apply run's verification coordinate.

    A는 이 경계에서 좌표만 예약한다. run의 conclusion·commit·plan digest는 읽지도 저장하지도
    않는다 — D Worker가 `run_id`로 run을 재조회해 승인 사실과 대조한 것만 정본이 된다
    (ADR-0019 §7, DATABASE.md "완료 Event 경계").
    """
    deployment_id, run_id = parse_completion_event(event)
    _apply_completion_service().record_completion(deployment_id=deployment_id, run_id=run_id)
    return {"deployment_id": deployment_id, "run_id": run_id}


def _apply_completion_service() -> ApplyCompletionService:
    try:
        import boto3
    except ImportError as error:  # pragma: no cover - boto3 is provided by Lambda runtime.
        raise RuntimeError("AWS Lambda boto3 runtime is required") from error
    table_name = _required_string(os.environ.get("METADATA_TABLE_NAME"), "METADATA_TABLE_NAME")
    queue_url = _required_string(os.environ.get("DEPLOYMENT_QUEUE_URL"), "DEPLOYMENT_QUEUE_URL")
    table = _metadata_table()
    transaction_client = boto3.client("dynamodb")
    workflow_repository = DynamoDbAssessmentWorkflowRepository(
        table, table_name=table_name, transaction_client=transaction_client
    )
    return ApplyCompletionService(
        deployments=DynamoDbDeploymentRepository(
            table=table, table_name=table_name, transaction_client=transaction_client
        ),
        jobs=workflow_repository,
        reservations=DynamoDbDeploymentCompletionStore(
            table_name=table_name, transaction_client=transaction_client
        ),
        outbox_dispatcher=OutboxDispatcher(
            repository=workflow_repository,
            dispatcher=SqsDeploymentWorkflowDispatcher(boto3.client("sqs"), queue_url=queue_url),
        ),
    )


def _http_handler() -> JobHttpHandler:
    repository, dispatcher = _workflow_components()
    metadata_table = _metadata_table()
    service = JobApiService(
        repository=repository,
        assessment_scope=EnvironmentAssessmentScope.from_environment(),
        # Profile 조회는 항상 호출자의 partition에서만 일어난다. Catalog가 생성 시점에 하나의
        # `customer_id`에 묶이므로 다른 고객의 Profile은 이 어댑터로 표현할 수 없다.
        policy_catalog_factory=lambda *, customer_id: DynamoDbPolicyCatalog(
            metadata_table, customer_id=customer_id
        ),
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
        deployments=_deployment_components(repository, reports),
        remediations=_remediation_components(repository),
        policy_sources=policy_sources,
        policy_approvals=_policy_approval_components(),
        policy_candidates=_policy_candidate_components(),
        # 감사 이력은 읽기 전용이고 principal의 customer partition으로만 조회한다.
        # 관측·비용 조회는 live metric source가 주입된 배포에서만 존재한다. source 없이
        # route만 노출하면 항상 실패하는 endpoint가 생기고, 그 실패가 "값이 없다"인지
        # "배선이 없다"인지 호출자가 구분할 수 없다.
        observability=_observability_components(),
        audit_events=AuditEventApiService(events=DynamoDbAuditEventRepository(_metadata_table())),
        policy_reader=policy_reader,
        remediation_exceptions=_remediation_exception_components(),
        orchestrations=_orchestration_components(),
        users=_user_management_components(),
        scope=_scope_components(),
        # 조치 요청 뒤 Worker가 만든 patch와 PR 좌표를 화면이 읽는 유일한 경로.
        remediation_reads=RemediationReadApiService(
            jobs=repository,
            remediations=DynamoDbRemediationReadRepository(metadata_table),
        ),
    )


def _deployment_components(
    workflow_repository: DynamoDbAssessmentWorkflowRepository,
    reports: DynamoDbAssessmentReportStore,
) -> DeploymentApiService:
    """Wire the deployment creation and reject paths to their durable stores.

    Every deployment route is wired here. Create and reject persist through the
    deployment record store and the Job store; creation also reads the remediation's
    stored decision and worker result through the deployment source reader. Read and
    verification are assembled from the deployment's own item prefix and the two
    immutable Assessments. Approve reads the stored plan and derives C's readiness
    verdict from D's persisted plan summary, and the status read uses that same
    verdict so the two views cannot disagree.

    The RUN_DEPLOYMENT outbox reuses the workflow repository's outbox bookkeeping and
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
    comparisons = DynamoDbComparisonInputReader(reports)
    plans = DynamoDbDeploymentPlanReader(_metadata_table(), deployments=deployment_repository)
    return DeploymentApiService(
        approvals=DeploymentApprovalService(approval_repository),
        plans=plans,
        sources=DynamoDbDeploymentSourceReader(
            _metadata_table(), commits=_deployment_commit_resolver()
        ),
        facts=DynamoDbDeploymentFactsReader(
            _metadata_table(),
            deployments=deployment_repository,
            jobs=workflow_repository,
            # 상태 화면의 검증 판정과 검증 조회가 같은 입력을 쓰도록 같은 reader를 넘긴다.
            comparisons=comparisons,
            # 상태 파생의 readiness도 승인이 소비하는 것과 같은 판정을 쓴다. 두 화면이
            # 다른 근거로 계산하면 "승인 대기"와 실제 승인 가능 여부가 어긋난다.
            readiness=plans,
        ),
        comparisons=comparisons,
        deployments=deployment_repository,
        jobs=workflow_repository,
        outbox_dispatcher=OutboxDispatcher(repository=workflow_repository, dispatcher=dispatcher),
        deployment_id_factory=lambda: f"dep-{uuid.uuid4()}",
        job_id_factory=lambda: f"job-{uuid.uuid4()}",
        now=lambda: datetime.now(UTC),
    )


def _remediation_components(
    workflow_repository: DynamoDbAssessmentWorkflowRepository,
) -> RemediationApiService:
    """Wire `POST /findings/{findingId}/remediations` to its durable stores and B policy.

    A(this service)는 finding_id 하나로 B의 정책 판정을 먼저 적용한 뒤에만 remediation을
    영속화·dispatch한다. 조립 순서가 곧 경계다:

    - `contexts`/`targets`: 같은 `DynamoDbRemediationContextReader`가 immutable 증거(Finding,
      IAC/Actual 결과, 평가된 commit)를 되돌린다. 조치 유형은 정하지 않는다.
    - `exceptions`: 읽기 전용 예외 view. `list_exceptions`만 쓰인다.
    - `decision_maker`: B의 `RemediationPolicy`. 커밋된 eligibility(`fixtures/rules/remediation.json`)가
      정본이며, 등록되지 않은 Rule은 자동 조치가 열리지 않고 `MANUAL_REVIEW`로 닫힌다.
    - `repository`/`outbox_dispatcher`: 판정 record와 (actionable일 때) Job·outbox를 한
      workflow repository로 쓰고, GENERATE_REMEDIATION/SYNC_ACTUAL_STATE task를 **Remediation
      Worker 큐**로 dispatch한다. Assessment dispatcher는 ASSESS_RESOURCE만 받으므로 여기에
      쓰면 remediation task가 거부되어 outbox에 PENDING으로 남는다.
    """
    try:
        import boto3
    except ImportError as error:  # pragma: no cover - boto3는 Lambda 런타임이 제공한다.
        raise RuntimeError("AWS Lambda boto3 runtime is required") from error
    queue_url = _required_string(os.environ.get("REMEDIATION_QUEUE_URL"), "REMEDIATION_QUEUE_URL")
    remediation_policy = load_rule_registry(_rules_path()).remediation
    context_reader = DynamoDbRemediationContextReader(_metadata_table())
    dispatcher = SqsRemediationWorkflowDispatcher(boto3.client("sqs"), queue_url=queue_url)
    return RemediationApiService(
        contexts=context_reader,
        targets=context_reader,
        exceptions=_remediation_exception_reader(),
        decision_maker=remediation_policy,
        repository=workflow_repository,
        outbox_dispatcher=OutboxDispatcher(repository=workflow_repository, dispatcher=dispatcher),
        now=lambda: datetime.now(UTC),
        job_id_factory=lambda: f"job-{uuid.uuid4()}",
        remediation_id_factory=lambda: f"rem-{uuid.uuid4()}",
    )


def _rules_path() -> Path:
    return Path(__file__).parents[3] / "fixtures" / "rules"


def _orchestration_components() -> object | None:
    """Wire POST /orchestrate to the LangGraph Parent Orchestrator (ADR-0012).

    The Parent classifies one natural-language message into a Policy Q&A answer or a
    workflow proposal; it starts no work. LangGraph and its dependencies live in a Lambda
    Layer, so the import is deferred to here and the whole route is disabled (returns
    None) when the Layer is absent, rather than failing the module import for every route.
    """
    try:
        import boto3
    except ImportError as error:  # pragma: no cover - boto3는 Lambda 런타임이 제공한다.
        raise RuntimeError("AWS Lambda boto3 runtime is required") from error
    try:
        from agent.agents.parent_orchestrator import ParentOrchestrator
        from apps.backend.api.orchestration import OrchestrationApiService
    except ImportError:
        # LangGraph Layer not attached to this function; leave the route unavailable
        # instead of breaking unrelated routes at import time.
        return None
    profile = _parent_model_profile()
    client = boto3.client("bedrock-runtime", region_name=profile.region)

    # Ground Policy Q&A in the caller's published rules. The catalog is customer-scoped, so the
    # factory is keyed by the caller's own customer_id — a caller cannot read another customer's
    # rules by naming their Profile. If the metadata table is not configured, skip grounding
    # rather than fail the route.
    catalog_factory = None
    table_name = os.environ.get("METADATA_TABLE_NAME")
    if table_name and table_name.strip():
        from apps.backend.policy.dynamodb_catalog import DynamoDbPolicyCatalog

        resource = boto3.resource("dynamodb").Table(table_name)

        def catalog_factory(customer_id: str) -> object:
            return DynamoDbPolicyCatalog(resource, customer_id=customer_id)

    return OrchestrationApiService(
        router=ParentOrchestrator(client=client),
        model_profile=profile,
        catalog_factory=catalog_factory,
    )


def _parent_model_profile() -> object:
    from packages.contracts import ModelProfile, ModelProfileRole

    raw = (Path(__file__).parents[3] / "fixtures" / "m1" / "parent_model_profile.json").read_text()
    data = json.loads(raw)
    return ModelProfile(
        model_profile_id=data["model_profile_id"],
        role=ModelProfileRole(data["role"]),
        region=data["region"],
        model_id=data["model_id"],
        prompt_version=data["prompt_version"],
        rubric_version=data["rubric_version"],
        golden_dataset_version=data["golden_dataset_version"],
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


def _scope_components() -> object:
    """Read-only assessment scope view from ASSESSMENT_SCOPE_JSON (deployment config)."""
    from apps.backend.api.scope import ScopeApiService

    return ScopeApiService(scope_json=os.environ.get("ASSESSMENT_SCOPE_JSON"))


def _user_management_components() -> object | None:
    """Construct the Admin user-management service. Needs USER_POOL_ID and Cognito access.

    Returns None when the pool id is not configured so the route stays closed rather than
    exposing an endpoint that always fails.
    """
    pool = os.environ.get("USER_POOL_ID")
    if not pool or not pool.strip():
        return None
    try:
        import boto3
    except ImportError as error:  # pragma: no cover - boto3는 Lambda 런타임이 제공한다.
        raise RuntimeError("AWS Lambda boto3 runtime is required") from error
    from apps.backend.api.users import UserManagementService

    return UserManagementService(client=boto3.client("cognito-idp"), user_pool_id=pool.strip())


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


def _policy_candidate_components() -> PolicyCandidateApiService | None:
    """후보 추출 요청·조회 서비스를 구성한다. queue가 없으면 route를 열지 않는다.

    queue URL 없이 service만 배선하면, 요청은 저장되지만 아무도 처리하지 않는 실행이 쌓인다.
    그 상태는 "추출 중"과 구별되지 않으므로 배선 자체를 만들지 않는다.
    """
    queue_url = os.environ.get("POLICY_AUTHORING_QUEUE_URL")
    if not queue_url or not queue_url.strip():
        return None
    try:
        import boto3
    except ImportError as error:  # pragma: no cover - boto3는 Lambda 런타임이 제공한다.
        raise RuntimeError("AWS Lambda boto3 runtime is required") from error
    table_name = _required_string(os.environ.get("METADATA_TABLE_NAME"), "METADATA_TABLE_NAME")
    return PolicyCandidateApiService(
        repository=DynamoDbPolicyApprovalRepository(
            table_name=table_name,
            transaction_client=boto3.client("dynamodb"),
            table=_metadata_table(),
        ),
        queue=SqsPolicyAuthoringDispatcher(boto3.client("sqs"), queue_url=queue_url),
    )


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


def _observability_components() -> DemoRunObservabilityService | None:
    """Return the Admin observability service, or `None` when no live source is wired.

    `DemoRunObservabilityService`는 주입된 read-only source가 돌려준 사실만 묶는다. live
    CloudWatch/CloudTrail/Cost Explorer adapter는 아직 없으므로 이 배포에서는 `None`이고,
    route는 404로 남는다 — 조회할 source가 없는 것을 500으로 보여주지 않는다.
    """
    return None


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


def _repository_ids(value: object) -> frozenset[str]:
    """Read the repositories one customer may assess.

    허용 필드는 `SCOPE_ENTRY_FIELDS`가 정한다. 여기 다시 나열하지 않는 이유는 그 목록이 늘었을
    때 설명만 낡기 때문이다. 목록 밖 필드는 fail-closed로 거부한다 — 그래야 폐기된
    `policy_profile_id`가 조용히 무시된 채 운영자가 "Profile 경계가 아직 환경변수로 강제된다"고
    믿는 일이 없고, 비밀 참조(role ARN, secret id)도 이 환경변수에 들어오지 못한다.
    """
    allowed = SCOPE_ENTRY_FIELDS
    if not isinstance(value, list):
        raise ValueError
    repositories: set[str] = set()
    for entry in value:
        if (
            not isinstance(entry, Mapping)
            or not set(entry) <= allowed
            or "repository_id" not in entry
        ):
            raise ValueError
        repositories.add(_required_string(entry.get("repository_id"), "repository_id"))
    return frozenset(repositories)


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value
