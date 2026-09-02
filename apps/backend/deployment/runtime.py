"""AWS Lambda composition root for the D Deployment Worker (ADR-0019).

이 핸들러는 A가 `DEPLOYMENT_QUEUE_URL`로 보낸 배포 command
(`RUN_DEPLOYMENT`/`PLAN_COMPLETED`/`APPLY_COMPLETED`)를 SQS event source로 소비해
`DeploymentWorker`를 구동한다. Queue payload는 `job_id`/`expected_revision`/`command`만 담고,
authoritative work는 `DynamoDbDeploymentWorkRepository`가 DynamoDB에서 다시 읽는다(ADR-0013).

책임 분리:
- `parse_tasks(event)`: SQS Records → `WorkflowTask` (세 배포 command 허용). 순수 함수.
- `run_tasks(event, worker)`: 파싱 후 각 task를 주입된 Worker로 구동. mode와 무관한 구동 루프.
- `lambda_handler(event, context)`: mode를 fail-closed로 판단해 live Worker를 조립하고 구동.

**주의(범위):** 완료 Event 경계는 확정됐다(ADR-0019 §7, DATABASE.md "완료 Event 경계"). D는
`#EVENT#{run_id}` 예약 item에서 `run_reference`를 읽어 채우고 검증 후 `VERIFIED`로 확정하는 경로를
모두 구현했다. 그 예약 item을 쓰는 주체(A/EventBridge)는 A 몫이며, 예약이 없으면 D Worker가
`APPLY_COMPLETED`를 fail-closed한다. 아직 남은 것은 live `PlanRequestPort` 구현(GitHub Actions plan
run dispatch + saved plan/plan_hash/state 회수)과 `_live_worker`의 실제 어댑터 조립이다. 그전까지
`lambda_handler`의 live 경로는 설정을 검증한 뒤 명시적 오류로 멈춘다. 구동 루프(`run_tasks`)와
파싱(`parse_tasks`)은 Worker를 주입받아 독립적으로 검증한다.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping

from apps.backend.deployment.runtime_config import DeploymentRuntimeConfiguration
from apps.backend.deployment.worker import DeploymentWorker
from packages.contracts import WorkflowCommand, WorkflowTask


class DeploymentRuntimeError(RuntimeError):
    """Deployment Worker runtime가 설정되지 않았거나 승인 범위 밖일 때 발생한다."""


class LivePlanUnavailableError(DeploymentRuntimeError):
    """live plan 어댑터·`_live_worker` 조립이 아직 없어 live 구동을 완결할 수 없다."""


def lambda_handler(event: Mapping[str, object], context: object) -> None:
    """SQS event source entrypoint. mode를 fail-closed로 판단해 Worker를 구동한다."""
    raw_configuration = os.environ.get("DEPLOYMENT_RUNTIME_JSON")
    if not raw_configuration:
        raise DeploymentRuntimeError("deployment worker runtime is not configured")
    worker = _live_worker(raw_configuration)
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


def _live_worker(raw_configuration: str) -> DeploymentWorker:
    """승인된 target 설정을 검증한다. 완결 조립은 live plan 어댑터 구현 뒤에 이어 붙인다.

    apply/verify/reread live 어댑터(`agent/runtime/live_deployment_ports.py`), 3개 store,
    `DynamoDbDeploymentWorkRepository`(완료 Event 예약 item에서 `run_reference`를 채움)는 모두
    구현돼 있다. 아직 없는 것은 live `PlanRequestPort`(GitHub Actions plan run dispatch + saved
    plan/plan_hash/state 회수)뿐이라, 그것 없이는 `RUN_DEPLOYMENT`를 완결할 수 없다. 추측 구현
    대신 설정만 fail-closed로 검증하고 명시적 오류로 멈춘다. 이 함수는 live plan 어댑터가 준비되면
    그 조각들(과 boto3 자격 증명 client)을 조립하는 자리다.
    """
    DeploymentRuntimeConfiguration.from_json(raw_configuration)  # fail-closed 설정 검증.
    raise LivePlanUnavailableError(
        "live deployment worker wiring awaits the live plan request port implementation"
    )


def _string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("value must be a non-empty string")
    return value
