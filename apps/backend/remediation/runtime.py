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
외부 I/O가 없다(`SnapshotSyncAction`). `TERRAFORM_PATCH`는 아직 막는다. 그 port는 승인된 snapshot에
바인딩된 Terraform 변경을 실제로 **생성**해야 하는데, 저장소에 있는 것은 변경 계획을 주입받는
fixture generator뿐이다. 미구현 생성기를 조용히 fixture로 대체하면 고객 repository에 아무도 만들지
않은 patch가 제안된다.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping

from apps.backend.remediation.sync import SnapshotSyncAction
from apps.backend.remediation.worker import RemediationWorker
from apps.backend.repositories import (
    DynamoDbRemediationResultStore,
    DynamoDbRemediationWorkRepository,
)
from packages.contracts import (
    RemediationContext,
    RemediationDecision,
    RemediationPatch,
    WorkflowCommand,
    WorkflowTask,
)

_REMEDIATION_COMMANDS = frozenset(
    {WorkflowCommand.GENERATE_REMEDIATION, WorkflowCommand.SYNC_ACTUAL_STATE}
)


class RemediationRuntimeError(RuntimeError):
    """Remediation Worker runtime가 설정되지 않았거나 실행할 수 없다."""


class PatchGenerationUnavailableError(RemediationRuntimeError):
    """승인된 snapshot에 바인딩되는 실제 Terraform patch 생성기가 아직 없다."""


class UnavailablePatchAction:
    """Refuse to produce a patch until a real generator exists (ADR-0018).

    `FixturePatchGenerator`는 "어떤 파일을 어떻게 바꿀지"를 미리 주입받는다. 고객 실행 경로에서
    그것을 쓰면 사람이 검토한 적 없는 변경이 고객 repository에 제안된다. 그래서 막는 쪽이 기본이고,
    `ACTUAL_SYNC` 경로는 이 port를 거치지 않으므로 영향을 받지 않는다.
    """

    def generate(
        self, *, context: RemediationContext, decision: RemediationDecision
    ) -> RemediationPatch:
        raise PatchGenerationUnavailableError(
            "live Terraform patch generation is not implemented; TERRAFORM_PATCH is blocked"
        )


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
    return RemediationWorker(
        work_repository=DynamoDbRemediationWorkRepository(
            boto3.resource("dynamodb").Table(table_name)
        ),
        patch_action=_patch_action(boto3),
        sync_action=SnapshotSyncAction(),
        result_store=DynamoDbRemediationResultStore(
            table_name=table_name, transaction_client=boto3.client("dynamodb")
        ),
    )


def _patch_action(boto3: object) -> object:
    """Build the C Remediation Agent that generates the Terraform patch (ADR-0018).

    Replaces the fail-closed `UnavailablePatchAction`: TERRAFORM_PATCH now produces a
    real, snapshot-bound patch from the approved remediation Model Profile via Bedrock.
    """
    from apps.backend.remediation.bedrock import BedrockPatchGenerator

    profile = _remediation_model_profile()
    return BedrockPatchGenerator(
        client=boto3.client("bedrock-runtime", region_name=profile.region),
        model_profile=profile,
    )


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
