"""AWS Lambda composition root for the C Remediation Worker (ADR-0013, ADR-0018).

A가 `REMEDIATION_QUEUE_URL`로 보낸 두 command(`GENERATE_REMEDIATION`/`SYNC_ACTUAL_STATE`)를 SQS
event source로 소비해 `RemediationWorker`를 구동한다. Queue payload는
`job_id`/`expected_revision`/`command`만 담고, authoritative work는
`DynamoDbRemediationWorkRepository`가 DynamoDB에서 다시 읽는다.

책임 분리는 Deployment Worker runtime과 같다:
- `parse_tasks(event)`: SQS Records → `WorkflowTask` (두 remediation command만 허용). 순수 함수.
- `run_tasks(event, worker)`: 파싱 후 각 task를 주입된 Worker로 구동.
- `lambda_handler(event, context)`: Worker를 조립해 구동한다.

**범위:** `ACTUAL_SYNC`는 완결 배선됐다 — 대상이 평가된 snapshot commit이므로 port가 결정적이고
외부 I/O가 없다(`SnapshotSyncAction`). `TERRAFORM_PATCH`는 Bedrock Remediation Agent가 patch를
생성하고(`BedrockPatchGenerator`), 그 바이트를 content-addressed로 저장한 뒤
(`DynamoDbPatchContentStore`), D의 live GitHub write 어댑터가 branch/commit/PR을 연다
(`LiveGitHubWriteTool`, ADR-0019 §3·§6). PR write에 필요한 승인 repository·token scope는 Deployment와
같은 `DEPLOYMENT_RUNTIME_JSON`에서 읽는다. 그 설정이 비어 있으면 Worker는 patch를 생성하기 전에
fail-closed하고, `ACTUAL_SYNC`만 동작한다.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping

from agent.runtime.github_app_token import GitHubAppTokenProvider
from agent.runtime.github_rest_snapshot_tool import GitHubRestSnapshotTool
from agent.runtime.github_tool import IaCDocumentReader
from agent.runtime.live_github_write_tool import LiveGitHubWriteTool
from apps.backend.deployment.runtime_config import (
    DeploymentRuntimeConfiguration,
    DeploymentTarget,
)
from apps.backend.remediation.patch_content import PatchContentStore
from apps.backend.remediation.pull_request import PatchPullRequestAction
from apps.backend.remediation.sync import SnapshotSyncAction
from apps.backend.remediation.worker import RemediationWorker
from apps.backend.repositories import (
    DynamoDbPatchContentStore,
    DynamoDbRemediationResultStore,
    DynamoDbRemediationWorkRepository,
)
from packages.contracts import WorkflowCommand, WorkflowTask

_REMEDIATION_COMMANDS = frozenset(
    {WorkflowCommand.GENERATE_REMEDIATION, WorkflowCommand.SYNC_ACTUAL_STATE}
)


class RemediationRuntimeError(RuntimeError):
    """Remediation Worker runtime가 설정되지 않았거나 실행할 수 없다."""


def lambda_handler(event: Mapping[str, object], context: object) -> None:
    """SQS event source entrypoint. Worker를 조립해 구동한다."""
    run_tasks(event, _live_worker())


def run_tasks(event: Mapping[str, object], worker: RemediationWorker) -> None:
    """파싱한 각 task를 주입된 Worker로 구동한다."""
    if not isinstance(worker, RemediationWorker):
        raise TypeError("worker must be a RemediationWorker")
    for task in parse_tasks(event):
        worker.handle(task)


def parse_tasks(event: Mapping[str, object]) -> tuple[WorkflowTask, ...]:
    """SQS Records를 WorkflowTask로 파싱한다(두 remediation command만 허용).

    Assessment나 Deployment command가 이 큐로 잘못 흘러들면 Worker가 "지원하지 않는 command"로
    실패하기 전에 여기서 막는다. 큐를 잘못 지목한 것은 재시도로 나아지지 않는다.
    """
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
        command = WorkflowCommand(_string(task_data.get("command")))
        if command not in _REMEDIATION_COMMANDS:
            raise ValueError("command is not a remediation command")
        tasks.append(
            WorkflowTask(
                job_id=_string(task_data.get("job_id")),
                expected_revision=task_data.get("expected_revision"),
                command=command,
            )
        )
    return tuple(tasks)


def _live_worker() -> RemediationWorker:
    table_name = _required_env("METADATA_TABLE_NAME")
    boto3 = _boto3()
    table = boto3.resource("dynamodb").Table(table_name)
    content_store = DynamoDbPatchContentStore(table)
    target = _approved_target(os.environ.get("DEPLOYMENT_RUNTIME_JSON"))
    # 평가가 IaC를 읽은 것과 같은 read-only GitHub 경계로 같은 commit의 본문을 읽는다. patch
    # 생성과 PR 본문의 diff가 같은 원본을 본다.
    iac_documents = None if target is None else _iac_document_reader(boto3, target)
    return RemediationWorker(
        work_repository=DynamoDbRemediationWorkRepository(table),
        patch_action=_patch_action(boto3, content_store, iac_documents),
        sync_action=SnapshotSyncAction(),
        result_store=DynamoDbRemediationResultStore(
            table_name=table_name, transaction_client=boto3.client("dynamodb")
        ),
        pull_request_action=(
            None
            if target is None
            else _pull_request_action_for(boto3, content_store, target, iac_documents)
        ),
    )


def _patch_action(
    boto3: object, content_store: PatchContentStore, iac_documents: IaCDocumentReader | None
) -> object:
    """Build the C Remediation Agent that generates the Terraform patch (ADR-0018).

    TERRAFORM_PATCH produces a real, snapshot-bound patch from the approved remediation
    Model Profile via Bedrock, and the generator stores the patch bytes under their
    digest so the pull request writer can read exactly what was digested. `iac_documents`
    is the read-only Terraform body reader; without it the generator refuses to run (the
    Worker already refuses TERRAFORM_PATCH when no pull request port exists).
    """
    from apps.backend.remediation.bedrock import BedrockPatchGenerator

    profile = _remediation_model_profile()
    return BedrockPatchGenerator(
        client=boto3.client("bedrock-runtime", region_name=profile.region),
        model_profile=profile,
        content_store=content_store,
        iac_documents=iac_documents,
    )


def _approved_target(raw_configuration: object) -> DeploymentTarget | None:
    """The single approved repository this Worker may read from and open PRs against.

    `None`은 "구성되지 않았다"는 값이고, Worker는 그 상태에서 TERRAFORM_PATCH를 생성 전에
    거부한다(patch는 만들었는데 올릴 곳이 없는 상태를 만들지 않는다). 설정은 Deployment와 같은
    `DEPLOYMENT_RUNTIME_JSON`이며 단일 승인 target을 요구한다 — 어댑터가 (customer_id,
    repository_id) scope로 고정 생성되기 때문이다(deployment runtime과 같은 규칙).
    """
    if not isinstance(raw_configuration, str) or not raw_configuration.strip():
        return None
    configuration = DeploymentRuntimeConfiguration.from_json(raw_configuration)
    targets = configuration.targets
    if len(targets) != 1:
        raise RemediationRuntimeError("pull request write requires exactly one approved target")
    return targets[0]


def _iac_document_reader(boto3: object, target: DeploymentTarget) -> IaCDocumentReader:
    """The same GET-only GitHub reader the Assessment Worker uses for the IAC perspective."""
    secrets = boto3.client("secretsmanager")
    return GitHubRestSnapshotTool(
        customer_id=target.customer_id,
        repository_id=target.repository_id,
        repository_full_name=target.repository_full_name,
        token_provider=GitHubAppTokenProvider(
            secret_reader=lambda: _secret_string(secrets, target.github_token_secret_id)
        ),
    )


def _pull_request_action(
    boto3: object, content_store: PatchContentStore, raw_configuration: object
) -> PatchPullRequestAction | None:
    """Build D's pull request writer for the one approved repository, or None if unconfigured."""
    target = _approved_target(raw_configuration)
    if target is None:
        return None
    return _pull_request_action_for(
        boto3, content_store, target, _iac_document_reader(boto3, target)
    )


def _pull_request_action_for(
    boto3: object,
    content_store: PatchContentStore,
    target: DeploymentTarget,
    iac_documents: IaCDocumentReader | None,
) -> PatchPullRequestAction:
    secrets = boto3.client("secretsmanager")
    writer = LiveGitHubWriteTool(
        customer_id=target.customer_id,
        repository_id=target.repository_id,
        repository_full_name=target.repository_full_name,
        token_provider=GitHubAppTokenProvider(
            secret_reader=lambda: _secret_string(secrets, target.github_token_secret_id)
        ),
    )
    return PatchPullRequestAction(
        writer=writer, content_store=content_store, iac_documents=iac_documents
    )


def _secret_string(client: object, secret_id: str) -> str:
    try:
        response = client.get_secret_value(SecretId=secret_id)
    except Exception:
        raise RemediationRuntimeError("remediation runtime secret read failed") from None
    if not isinstance(response, Mapping):
        raise RemediationRuntimeError("remediation runtime secret response is invalid")
    return _string(response.get("SecretString"))


def _remediation_model_profile() -> object:
    from pathlib import Path

    from packages.contracts import ModelProfile, ModelProfileRole

    raw = (
        Path(__file__).parents[3] / "fixtures" / "m1" / "remediation_model_profile.json"
    ).read_text()
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


def _boto3() -> object:
    try:
        import boto3
    except ImportError as error:  # pragma: no cover - boto3는 Lambda 런타임이 제공한다.
        raise RemediationRuntimeError("AWS Lambda boto3 runtime is required") from error
    return boto3


def _required_env(name: str) -> str:
    """필수 runtime 환경 변수를 읽는다. 누락은 설정 실패이므로 fail-closed한다."""
    value = os.environ.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RemediationRuntimeError(f"remediation worker runtime requires {name}")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("value must be a non-empty string")
    return value
