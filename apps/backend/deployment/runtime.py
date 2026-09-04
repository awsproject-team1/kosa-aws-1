"""AWS Lambda composition root for the D Deployment Worker (ADR-0019).

이 핸들러는 A가 `DEPLOYMENT_QUEUE_URL`로 보낸 배포 command
(`RUN_DEPLOYMENT`/`PLAN_COMPLETED`/`APPLY_COMPLETED`)를 SQS event source로 소비해
`DeploymentWorker`를 구동한다. Queue payload는 `job_id`/`expected_revision`/`command`만 담고,
authoritative work는 `DynamoDbDeploymentWorkRepository`가 DynamoDB에서 다시 읽는다(ADR-0013).

책임 분리:
- `parse_tasks(event)`: SQS Records → `WorkflowTask` (세 배포 command 허용). 순수 함수.
- `run_tasks(event, worker)`: 파싱 후 각 task를 주입된 Worker로 구동. mode와 무관한 구동 루프.
- `lambda_handler(event, context)`: mode를 fail-closed로 판단해 live Worker를 조립하고 구동.

**범위:** 완료 Event 경계는 확정됐고(ADR-0019 §7, DATABASE.md "완료 Event 경계") D는 예약 item에서
`run_reference`를 읽어 검증·확정하는 경로를 구현했다. `_live_worker`가 승인된 단일 target으로 D 실행
port 4종(`LivePlanRequestPort`/`LiveApplyDispatchPort`/`LiveWorkflowRunReader`/`LiveActualRereadPort`)·
store 3종·`DynamoDbDeploymentWorkRepository`를 조립한다. 조립 로직은 I/O seam(`plan_outputs_fetcher`,
boto3 client, secret_reader)을 주입받아 테스트하고, `lambda_handler`가 실제 I/O를 주입한다.

`_live_plan_outputs_fetcher`는 customer-installed plan workflow를 dispatch한 뒤 GitHub API에서 exact
commit/display-title run을 재조회·폴링하고, GitHub API artifact ZIP에서 canonical plan/state/binary를
검증해 회수한다. 실제 protected sandbox 실행은 customer approval와 credential이 필요한 외부 단계다.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import time
import uuid
import zipfile
from collections.abc import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from agent.runtime.actual_resource_tool_factory import (
    ClientFactoryProvider,
    build_actual_resource_tool,
)
from agent.runtime.github_app_token import GitHubAppTokenProvider
from agent.runtime.live_deployment_ports import (
    LiveActualRereadPort,
    LiveApplyDispatchPort,
    LivePlanRequestPort,
    LiveWorkflowRunReader,
    PlanRunOutputs,
)
from apps.backend.assessment.plan_facts import project_plan_evidence
from apps.backend.assessment.reporting import DynamoDbAssessmentReportStore
from apps.backend.deployment.runtime_config import (
    DeploymentRuntimeConfiguration,
    DeploymentTarget,
)
from apps.backend.deployment.verification import PostDeployVerificationService
from apps.backend.deployment.worker import DeploymentWorker
from apps.backend.jobs.outbox import OutboxDispatcher, WorkflowDispatcher
from apps.backend.jobs.sqs import SqsWorkflowDispatcher
from apps.backend.policy import DynamoDbPolicyCatalog, PolicyContextResolver
from apps.backend.policy.control_catalog import MVP_CONTROL_CATALOG
from apps.backend.repositories import (
    DynamoDbAssessmentWorkflowRepository,
    DynamoDbDeploymentPlanStore,
    DynamoDbDeploymentRepository,
    DynamoDbDeploymentRunStore,
    DynamoDbDeploymentVerificationStore,
    DynamoDbDeploymentWorkRepository,
    DynamoDbPostDeployVerificationStore,
    DynamoDbVerificationSourceReader,
)
from packages.contracts import WorkflowCommand, WorkflowTask


class DeploymentRuntimeError(RuntimeError):
    """Deployment Worker runtime가 설정되지 않았거나 승인 범위 밖일 때 발생한다."""


class LivePlanUnavailableError(DeploymentRuntimeError):
    """Retained for callers that need to distinguish an unavailable plan runner."""


# `LivePlanRequestPort.fetch_outputs`가 요구하는 실제 GitHub plan run I/O의 타입.
# (deployment_id, commit_sha) → PlanRunOutputs. plan `workflow_dispatch` 트리거, run 매칭·완료
# 폴링, artifact 다운로드·파싱은 이 콜백이 담당한다. 실제 구현은 sandbox 자격 증명·네트워크가
# 있어야 검증되므로 lambda_handler가 주입하고, 조립 로직(_live_worker)은 이 seam으로 테스트한다.
PlanOutputsFetcher = Callable[[DeploymentTarget, str, str], PlanRunOutputs]


def lambda_handler(event: Mapping[str, object], context: object) -> None:
    """SQS event source entrypoint. mode를 fail-closed로 판단해 Worker를 구동한다."""
    raw_configuration = os.environ.get("DEPLOYMENT_RUNTIME_JSON")
    if not raw_configuration:
        raise DeploymentRuntimeError("deployment worker runtime is not configured")
    # 설정 검증을 AWS client 생성보다 먼저 끝낸다. boto3 resource/client 생성은 region 등
    # 자체 환경을 요구하므로, 순서가 뒤집히면 "설정 누락"이 boto3의 다른 오류로 가려진다.
    table_name = _required_env("METADATA_TABLE_NAME")
    # 검증 Assessment task는 Assessment Queue로 간다. URL이 없으면 apply는 검증 없이 끝나므로
    # 설정 누락을 여기서 fail-closed로 잡는다.
    assessment_queue_url = _required_env("ASSESSMENT_QUEUE_URL")
    secret_reader = _live_secret_reader()
    worker = _live_worker(
        raw_configuration,
        plan_outputs_fetcher=_live_plan_outputs_fetcher(secret_reader),
        table=_metadata_table(table_name),
        table_name=table_name,
        transaction_client=_boto3_client("dynamodb"),
        secret_reader=secret_reader,
        sts_client=_boto3_client("sts"),
        client_factory_provider=_live_client_factory_provider(),
        assessment_dispatcher=SqsWorkflowDispatcher(
            _boto3_client("sqs"), queue_url=assessment_queue_url
        ),
        assessment_id_factory=lambda: f"asm-{uuid.uuid4()}",
    )
    run_tasks(event, worker)


def run_tasks(event: Mapping[str, object], worker: DeploymentWorker) -> None:
    """파싱한 각 task를 주입된 Worker로 구동한다(mode와 무관한 구동 루프)."""
    if not isinstance(worker, DeploymentWorker):
        raise TypeError("worker must be a DeploymentWorker")
    for task in parse_tasks(event):
        worker.handle(task)


def parse_tasks(event: Mapping[str, object]) -> tuple[WorkflowTask, ...]:
    """SQS Records를 WorkflowTask로 파싱한다(세 배포 command 허용)."""
    records = event.get("Records")
    if not isinstance(records, list):
        raise ValueError("SQS Records are required")
    tasks: list[WorkflowTask] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("SQS record is invalid")
        body = record.get("body")
        if not isinstance(body, str):
            raise ValueError("SQS record body is invalid")
        task_data = json.loads(body)
        if not isinstance(task_data, Mapping):
            raise ValueError("WorkflowTask body is invalid")
        tasks.append(
            WorkflowTask(
                job_id=_string(task_data.get("job_id")),
                expected_revision=task_data.get("expected_revision"),
                command=WorkflowCommand(_string(task_data.get("command"))),
            )
        )
    return tuple(tasks)


def _live_worker(
    raw_configuration: str,
    *,
    plan_outputs_fetcher: PlanOutputsFetcher,
    table: object,
    table_name: str,
    transaction_client: object,
    secret_reader: Callable[[str], str],
    sts_client: object,
    client_factory_provider: ClientFactoryProvider,
    assessment_dispatcher: WorkflowDispatcher,
    assessment_id_factory: Callable[[], str],
) -> DeploymentWorker:
    """승인된 단일 target으로 D 실행 port·store·work repository를 조립해 Worker를 만든다.

    I/O seam(`plan_outputs_fetcher`, boto3 client, secret_reader)은 주입받아 이 조립 로직 자체를
    테스트할 수 있게 한다. `lambda_handler`가 실제 GitHub/AWS I/O를 주입한다. `DeploymentRuntimeJSON`은
    현재 단일 승인 target 구성을 요구한다 — 어댑터는 (customer_id, repository_id) scope로 고정
    생성되기 때문이다. 다중 target 처리는 A의 다중 배포 운영이 확정되면 task별 재구성으로 확장한다.
    """
    configuration = DeploymentRuntimeConfiguration.from_json(raw_configuration)
    targets = configuration.targets
    if len(targets) != 1:
        raise DeploymentRuntimeError("live deployment worker requires exactly one approved target")
    target = targets[0]

    # 하나의 provider를 두 소비자가 공유한다. 발급한 token을 그 실행 안에서 재사용하려면
    # 인스턴스가 하나여야 한다.
    github_token = GitHubAppTokenProvider(
        secret_reader=lambda: secret_reader(target.github_token_secret_id)
    )

    # Post-deploy verification re-reads Actual through the same routing tool the Assessment
    # Worker uses. Hardwiring one service adapter here would either refuse the target's other
    # resource types or quietly re-read only S3, and ADR-0020 compares the re-read against
    # the Findings the deployment was supposed to fix.
    resource_tool = build_actual_resource_tool(
        customer_id=target.customer_id,
        aws_account_id=target.aws_account_id,
        role_arn=target.aws_read_role_arn,
        external_id=secret_reader(target.aws_external_id_secret_id),
        resource_types=target.resource_types,
        client_factory_provider=client_factory_provider,
        sts=sts_client,
    )
    plan_port = LivePlanRequestPort(
        customer_id=target.customer_id,
        repository_id=target.repository_id,
        repository_full_name=target.repository_full_name,
        # 실제 GitHub plan run I/O는 주입된 fetcher가 담당한다(target을 함께 넘겨 폴링/다운로드에 씀).
        fetch_outputs=lambda deployment_id, commit_sha: plan_outputs_fetcher(
            target, deployment_id, commit_sha
        ),
        # Catalog가 선언한 plan 위치의 `after` 값을 요약에 싣는다. readiness가 그 값으로 "이 plan이
        # Finding을 해소하는가"를 apply 전에 판정한다(ADR-0024 §E).
        evidence_projector=lambda document: project_plan_evidence(document, MVP_CONTROL_CATALOG),
    )
    apply_port = LiveApplyDispatchPort(
        customer_id=target.customer_id,
        repository_id=target.repository_id,
        repository_full_name=target.repository_full_name,
        token_provider=github_token,
    )
    run_reader = LiveWorkflowRunReader(
        customer_id=target.customer_id,
        repository_id=target.repository_id,
        repository_full_name=target.repository_full_name,
        token_provider=github_token,
    )
    actual_port = LiveActualRereadPort(
        customer_id=target.customer_id,
        aws_account_id=target.aws_account_id,
        resource_tool=resource_tool,
        resource_types=target.resource_types,
    )
    work_repository = DynamoDbDeploymentWorkRepository(
        table, aws_account_id_for=configuration.aws_account_id_for
    )
    plan_store = DynamoDbDeploymentPlanStore(
        table_name=table_name, transaction_client=transaction_client
    )
    run_store = DynamoDbDeploymentRunStore(
        table_name=table_name, transaction_client=transaction_client
    )
    verification_store = DynamoDbDeploymentVerificationStore(
        table_name=table_name, transaction_client=transaction_client
    )
    # apply 확정 뒤 검증 Assessment를 시작하는 A 경계(ADR-0020 §7). Deployment record와 Job을
    # 다시 읽고, 원 Assessment의 판본·plan·Model Profile을 pin한 새 Assessment와 ASSESS_RESOURCE
    # task를 한 transaction으로 쓴다.
    reports = DynamoDbAssessmentReportStore(table)
    workflow_repository = DynamoDbAssessmentWorkflowRepository(
        table, table_name=table_name, transaction_client=transaction_client
    )
    verification_starter = PostDeployVerificationService(
        deployments=DynamoDbDeploymentRepository(
            table=table, table_name=table_name, transaction_client=transaction_client
        ),
        jobs=workflow_repository,
        sources=DynamoDbVerificationSourceReader(table, reports=reports),
        context_resolvers=lambda *, customer_id: PolicyContextResolver(
            DynamoDbPolicyCatalog(table, customer_id=customer_id)
        ),
        resource_types_for=lambda customer_id, repository_id: (
            configuration.resolve(
                customer_id=customer_id, repository_id=repository_id
            ).resource_types
        ),
        store=DynamoDbPostDeployVerificationStore(
            table_name=table_name, transaction_client=transaction_client
        ),
        outbox_dispatcher=OutboxDispatcher(
            repository=workflow_repository, dispatcher=assessment_dispatcher
        ),
        assessment_id_factory=assessment_id_factory,
    )
    return DeploymentWorker(
        work_repository=work_repository,
        plan_port=plan_port,
        apply_port=apply_port,
        run_reader=run_reader,
        actual_port=actual_port,
        plan_store=plan_store,
        run_store=run_store,
        verification_store=verification_store,
        verification_starter=verification_starter,
    )


def _live_plan_outputs_fetcher(
    secret_reader: Callable[[str], str],
    *,
    opener: Callable[..., object] = urlopen,
    sleeper: Callable[[float], None] = time.sleep,
    max_polls: int = 60,
) -> PlanOutputsFetcher:
    """Dispatch, re-read, and retrieve an approved repository's Terraform plan.

    The only GitHub write is the customer-installed `terraform-plan.yml`
    ``workflow_dispatch``.  The runner is identified again from GitHub by its exact
    commit and deterministic display title; EventBridge values are deliberately not
    used.  Its artifact archive is accepted only from GitHub's API origin and is
    reduced to the three files required by the execution contract.
    """
    if not callable(secret_reader) or not callable(opener) or not callable(sleeper):
        raise TypeError("plan runner dependencies must be callable")
    if isinstance(max_polls, bool) or not isinstance(max_polls, int) or max_polls < 1:
        raise ValueError("max_polls must be a positive integer")

    def fetch(target: DeploymentTarget, deployment_id: str, commit_sha: str) -> PlanRunOutputs:
        if not isinstance(target, DeploymentTarget):
            raise TypeError("target must be a DeploymentTarget")
        if not isinstance(deployment_id, str) or not deployment_id.strip():
            raise ValueError("deployment_id must be a non-empty string")
        if not isinstance(commit_sha, str) or len(commit_sha) != 40:
            raise ValueError("commit_sha must be a 40-character SHA")
        token = secret_reader(target.github_token_secret_id)
        headers = _github_headers(token)
        repository = quote(target.repository_full_name, safe="/")
        workflow = "terraform-plan.yml"
        base = f"https://api.github.com/repos/{repository}/actions"
        _github_json(
            f"{base}/workflows/{workflow}/dispatches",
            method="POST",
            headers=headers,
            body=json.dumps(
                {
                    "ref": commit_sha,
                    "inputs": {"deployment_id": deployment_id, "commit_sha": commit_sha},
                },
                separators=(",", ":"),
            ).encode("utf-8"),
            opener=opener,
            accepted_statuses=frozenset({204}),
        )
        expected_title = f"terraform-plan deployment={deployment_id} commit={commit_sha}"
        run: Mapping[str, object] | None = None
        for attempt in range(max_polls):
            payload = _github_json(
                f"{base}/workflows/{workflow}/runs?event=workflow_dispatch&branch={quote(commit_sha)}&per_page=100",
                method="GET",
                headers=headers,
                opener=opener,
                accepted_statuses=frozenset({200}),
            )
            runs = payload.get("workflow_runs")
            if isinstance(runs, list):
                run = next(
                    (
                        candidate
                        for candidate in runs
                        if isinstance(candidate, Mapping)
                        and candidate.get("head_sha") == commit_sha
                        and candidate.get("display_title") == expected_title
                    ),
                    None,
                )
            if run is not None and run.get("status") == "completed":
                break
            run = None
            if attempt + 1 < max_polls:
                sleeper(5)
        if run is None or run.get("conclusion") != "success":
            raise DeploymentRuntimeError("approved Terraform plan run did not succeed")
        run_id = run.get("id")
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
            raise DeploymentRuntimeError("Terraform plan run id is invalid")
        artifacts = _github_json(
            f"{base}/runs/{run_id}/artifacts",
            method="GET",
            headers=headers,
            opener=opener,
            accepted_statuses=frozenset({200}),
        ).get("artifacts")
        if not isinstance(artifacts, list):
            raise DeploymentRuntimeError("Terraform plan artifacts response is invalid")
        expected_artifact = f"terraform-plan-{deployment_id}"
        artifact = next(
            (
                candidate
                for candidate in artifacts
                if isinstance(candidate, Mapping)
                and candidate.get("name") == expected_artifact
                and candidate.get("expired") is False
            ),
            None,
        )
        if artifact is None:
            raise DeploymentRuntimeError("Terraform plan artifact is missing or expired")
        archive_url = artifact.get("archive_download_url")
        if not isinstance(archive_url, str) or not archive_url.startswith(
            "https://api.github.com/"
        ):
            raise DeploymentRuntimeError("Terraform plan artifact URL is invalid")
        archive = _github_bytes(archive_url, headers=headers, opener=opener)
        return _plan_outputs_from_archive(archive, run_id=str(run_id))

    return fetch


def _github_headers(token: str) -> dict[str, str]:
    if not isinstance(token, str) or not token.strip():
        raise DeploymentRuntimeError("GitHub token is invalid")
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _github_json(
    url: str,
    *,
    method: str,
    headers: Mapping[str, str],
    opener: Callable[..., object],
    accepted_statuses: frozenset[int],
    body: bytes | None = None,
) -> Mapping[str, object]:
    request = Request(url, data=body, headers=dict(headers), method=method)
    try:
        with opener(request, timeout=10) as response:
            status = getattr(response, "status", response.getcode())
            raw = response.read()
    except (HTTPError, URLError, TimeoutError) as error:
        raise DeploymentRuntimeError("GitHub plan request failed") from error
    if status not in accepted_statuses:
        raise DeploymentRuntimeError("GitHub plan request returned an unexpected status")
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeploymentRuntimeError("GitHub plan response is invalid") from error
    if not isinstance(payload, Mapping):
        raise DeploymentRuntimeError("GitHub plan response is invalid")
    return payload


def _github_bytes(url: str, *, headers: Mapping[str, str], opener: Callable[..., object]) -> bytes:
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with opener(request, timeout=10) as response:
            status = getattr(response, "status", response.getcode())
            content = response.read()
    except (HTTPError, URLError, TimeoutError) as error:
        raise DeploymentRuntimeError("GitHub plan artifact download failed") from error
    if status != 200 or not isinstance(content, bytes):
        raise DeploymentRuntimeError("GitHub plan artifact download failed")
    return content


def _plan_outputs_from_archive(archive: bytes, *, run_id: str) -> PlanRunOutputs:
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            names = set(bundle.namelist())
            required = {"tfplan.binary", "plan.canonical.json", "plan.state.json"}
            if not required.issubset(names):
                raise DeploymentRuntimeError("Terraform plan artifact files are incomplete")
            binary = bundle.read("tfplan.binary")
            canonical = bundle.read("plan.canonical.json")
            state = json.loads(bundle.read("plan.state.json").decode("utf-8"))
            changes = json.loads(canonical.decode("utf-8"))
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeploymentRuntimeError("Terraform plan artifact is invalid") from error
    if not isinstance(state, Mapping) or set(state) != {"lineage", "serial"}:
        raise DeploymentRuntimeError("Terraform plan state artifact is invalid")
    lineage, serial = state.get("lineage"), state.get("serial")
    if (
        not isinstance(lineage, str)
        or not lineage
        or isinstance(serial, bool)
        or not isinstance(serial, int)
    ):
        raise DeploymentRuntimeError("Terraform plan state artifact is invalid")
    if not isinstance(changes, list) or not all(isinstance(change, Mapping) for change in changes):
        raise DeploymentRuntimeError("Terraform canonical plan artifact is invalid")
    return PlanRunOutputs(
        run_id=run_id,
        plan_hash=hashlib.sha256(canonical).hexdigest(),
        binary_sha256=hashlib.sha256(binary).hexdigest(),
        state_lineage=lineage,
        state_serial=serial,
        canonical_changes=changes,
        refreshed=True,
    )


def _live_secret_reader() -> Callable[[str], str]:
    client = _boto3_client("secretsmanager")

    def read(secret_id: str) -> str:
        try:
            response = client.get_secret_value(SecretId=secret_id)
        except Exception:
            raise DeploymentRuntimeError("deployment runtime secret read failed") from None
        if not isinstance(response, Mapping):
            raise DeploymentRuntimeError("deployment runtime secret response is invalid")
        return _string(response.get("SecretString"))

    return read


def _live_client_factory_provider() -> ClientFactoryProvider:
    """Return a per-service provider of lazy, credential-taking read clients."""
    boto3 = _boto3()

    def provider(service: str) -> Callable[[Mapping[str, str]], object]:
        def factory(credentials: Mapping[str, str]) -> object:
            return boto3.client(
                service,
                aws_access_key_id=credentials["AccessKeyId"],
                aws_secret_access_key=credentials["SecretAccessKey"],
                aws_session_token=credentials["SessionToken"],
            )

        return factory

    return provider


def _metadata_table(table_name: str) -> object:
    return _boto3().resource("dynamodb").Table(table_name)


def _boto3_client(service: str) -> object:
    return _boto3().client(service)


def _boto3() -> object:
    try:
        import boto3
    except ImportError as error:  # pragma: no cover - boto3는 Lambda 런타임이 제공한다.
        raise DeploymentRuntimeError("AWS Lambda boto3 runtime is required") from error
    return boto3


def _required_env(name: str) -> str:
    """필수 runtime 환경 변수를 읽는다. 누락은 설정 실패이므로 fail-closed한다."""
    value = os.environ.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DeploymentRuntimeError(f"deployment worker runtime requires {name}")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("value must be a non-empty string")
    return value
