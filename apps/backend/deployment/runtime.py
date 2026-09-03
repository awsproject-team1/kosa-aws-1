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

유일하게 실제 검증이 남은 부분은 `_live_plan_outputs_fetcher`의 GitHub plan run I/O(dispatch·run
매칭·완료 폴링·artifact 다운로드/파싱)다. 실제 sandbox 자격 증명·네트워크가 있어야 동작·검증되므로,
그 fetcher는 호출 시 명시적으로 막아 검증되지 않은 I/O가 조용히 실행되지 않게 한다. 나머지 조립·
구동·파싱은 seam 주입으로 이미 검증된다.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping

from agent.runtime.assume_role_s3_resource_tool import AssumeRoleS3ResourceTool
from agent.runtime.live_deployment_ports import (
    LiveActualRereadPort,
    LiveApplyDispatchPort,
    LivePlanRequestPort,
    LiveWorkflowRunReader,
    PlanRunOutputs,
)
from apps.backend.deployment.runtime_config import (
    DeploymentRuntimeConfiguration,
    DeploymentTarget,
)
from apps.backend.deployment.worker import DeploymentWorker
from apps.backend.repositories import (
    DynamoDbDeploymentPlanStore,
    DynamoDbDeploymentRunStore,
    DynamoDbDeploymentVerificationStore,
    DynamoDbDeploymentWorkRepository,
)
from packages.contracts import WorkflowCommand, WorkflowTask


class DeploymentRuntimeError(RuntimeError):
    """Deployment Worker runtime가 설정되지 않았거나 승인 범위 밖일 때 발생한다."""


class LivePlanUnavailableError(DeploymentRuntimeError):
    """live plan I/O가 주입되지 않아 live 구동을 완결할 수 없다."""


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
    worker = _live_worker(
        raw_configuration,
        plan_outputs_fetcher=_live_plan_outputs_fetcher(),
        table=_metadata_table(table_name),
        table_name=table_name,
        transaction_client=_boto3_client("dynamodb"),
        secret_reader=_live_secret_reader(),
        sts_client=_boto3_client("sts"),
        s3_client_factory=_live_s3_client_factory(),
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
    s3_client_factory: Callable[[Mapping[str, str]], object],
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

    def github_token() -> str:
        return secret_reader(target.github_token_secret_id)

    resource_tool = AssumeRoleS3ResourceTool(
        customer_id=target.customer_id,
        aws_account_id=target.aws_account_id,
        role_arn=target.aws_read_role_arn,
        external_id=secret_reader(target.aws_external_id_secret_id),
        sts=sts_client,
        s3_client_factory=s3_client_factory,
    )
    plan_port = LivePlanRequestPort(
        customer_id=target.customer_id,
        repository_id=target.repository_id,
        repository_full_name=target.repository_full_name,
        # 실제 GitHub plan run I/O는 주입된 fetcher가 담당한다(target을 함께 넘겨 폴링/다운로드에 씀).
        fetch_outputs=lambda deployment_id, commit_sha: plan_outputs_fetcher(
            target, deployment_id, commit_sha
        ),
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
    return DeploymentWorker(
        work_repository=work_repository,
        plan_port=plan_port,
        apply_port=apply_port,
        run_reader=run_reader,
        actual_port=actual_port,
        plan_store=plan_store,
        run_store=run_store,
        verification_store=verification_store,
    )


def _live_plan_outputs_fetcher() -> PlanOutputsFetcher:
    """실제 GitHub plan run I/O를 수행하는 fetcher(sandbox 자격 증명·네트워크 필요).

    plan `workflow_dispatch` 트리거 → run name(`deployment=<id> commit=<sha>`)으로 run 매칭 →
    완료 폴링 → artifact(`plan.canonical.json`/`plan.state.json`) 다운로드·파싱을 담당한다. 이
    경로는 실제 GitHub API·자격 증명이 있어야 동작·검증되므로, 조립 로직(`_live_worker`)과 분리해
    여기에 둔다. 실제 sandbox 배선(protected Environment·OIDC Role) 전까지는 호출 시 명시적으로
    막아, 검증되지 않은 I/O가 조용히 실행되지 않게 한다.
    """

    def fetch(target: DeploymentTarget, deployment_id: str, commit_sha: str) -> PlanRunOutputs:
        raise LivePlanUnavailableError(
            "live GitHub plan run I/O requires sandbox credentials and is not exercised yet"
        )

    return fetch


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


def _live_s3_client_factory() -> Callable[[Mapping[str, str]], object]:
    boto3 = _boto3()

    def factory(credentials: Mapping[str, str]) -> object:
        return boto3.client(
            "s3",
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
        )

    return factory


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
