"""M3 D 실행 port의 live GitHub/AWS 어댑터 (ADR-0019).

`agent/runtime/deployment_ports.py`가 정의한 세 주입 port의 실제 구현이다. Mock과 달리
실제 GitHub Actions REST API와 read-only AWS Resource Tool을 호출하지만, 어떤 어댑터도
승인·정책 판정을 하지 않고 승인 경계를 우회하는 write 표면을 노출하지 않는다.

- `LiveApplyDispatchPort`: 승인된 approval로 `workflow_dispatch`만 호출한다(ADR-0019 §5·§6).
  이 dispatch가 D의 유일한 write 표면이다. GitHub App에는 `workflows: write`가 없으므로
  workflow 파일 자체는 건드리지 않는다. 이중 apply를 막는 정본은 A의 `APPROVED → APPLYING`
  조건부 전이이며(§5), 이 어댑터는 그 성질을 깨지 않도록 결정적 run 좌표만 만든다.
- `LiveWorkflowRunReader`: `run_id`로 Actions run을 GET으로 재조회한다(ADR-0019 §7). EventBridge
  payload를 신뢰하지 않는다. 재조회 실패는 예외가 아니라 실패 결론을 담은 값으로 반환한다.
- `LiveActualRereadPort`: apply 후 AWS Actual을 M1 read-only Resource Tool로 다시 읽는다
  (ADR-0007, ADR-0020 §8). write 표면이 없다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agent.runtime.aws_resource_tool import (
    AwsResourceTool,
    AwsResourceToolError,
    AwsResourceView,
)
from agent.runtime.deployment_ports import (
    ActualRereadPort,
    ApplyDispatchPort,
    WorkflowRunReader,
)
from agent.runtime.github_tool import require_github_repository_full_name
from packages.contracts import (
    ApplyRunReference,
    AwsResourceOperation,
    AwsResourceQuery,
    AwsResourceSnapshot,
    DeploymentApproval,
    VerifiedRunOutcome,
)

# apply run을 트리거하는 workflow 파일 경로. GitHub App은 이 파일을 만들거나 수정할 수
# 없고(§6, workflows:write 없음), 고객이 1회 설치한 template만 dispatch한다.
_APPLY_WORKFLOW_FILE = "terraform-apply.yml"

# 완료된 run을 성공으로 인정할 때 workflow path가 반드시 이 allow-list 안이어야 한다(§7).
_APPLY_WORKFLOW_PATHS = frozenset(
    {".github/workflows/terraform-apply.yml", ".github/workflows/terraform-apply.yaml"}
)


class LiveDeploymentPortError(RuntimeError):
    """live 실행 port 어댑터 작업의 기본 실패 타입."""


def _require_non_empty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


class LiveApplyDispatchPort(ApplyDispatchPort):
    """승인된 approval로 GitHub Actions `workflow_dispatch`만 호출하는 live 어댑터.

    write 표면은 dispatch 하나뿐이다. run 좌표는 (deployment_id, plan_hash)에서 결정적으로
    유도하므로, at-least-once 재전달로 같은 approval이 두 번 들어와도 같은 run을 가리킨다
    — 다만 이중 apply의 정본 방어는 A의 조건부 상태 전이다(§5).
    """

    def __init__(
        self,
        *,
        customer_id: str,
        repository_id: str,
        repository_full_name: str,
        token_provider: Callable[[], str],
        dispatch: Callable[[str, Mapping[str, str], bytes], None] | None = None,
    ) -> None:
        _require_non_empty(customer_id, "customer_id")
        _require_non_empty(repository_id, "repository_id")
        self._customer_id = customer_id
        self._repository_id = repository_id
        self._repository_full_name = require_github_repository_full_name(repository_full_name)
        if not callable(token_provider):
            raise TypeError("token_provider must be callable")
        self._token_provider = token_provider
        self._dispatch = dispatch or _github_post

    def dispatch_apply(
        self,
        *,
        approval: DeploymentApproval,
        state_lineage: str,
        state_serial: int,
        repository_id: str,
    ) -> ApplyRunReference:
        if not isinstance(approval, DeploymentApproval):
            raise TypeError("approval must be a DeploymentApproval")
        _require_non_empty(state_lineage, "state_lineage")
        if isinstance(state_serial, bool) or not isinstance(state_serial, int):
            raise TypeError("state_serial must be an int")
        if repository_id != self._repository_id:
            raise LiveDeploymentPortError("repository_id is outside the tool scope")

        # workflow_dispatch input은 deployment_id/commit_sha/plan_hash뿐이다(§5). workflow가
        # 이 값으로 자신이 적용할 saved plan artifact를 조회·검증한다. state 좌표는 apply job이
        # 직접 재확인하므로 input으로 넘기지 않는다.
        body = json.dumps(
            {
                "ref": approval.commit_sha,
                "inputs": {
                    "deployment_id": approval.deployment_id,
                    "commit_sha": approval.commit_sha,
                    "plan_hash": approval.plan_hash,
                },
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        url = (
            f"https://api.github.com/repos/{self._repository_full_name}"
            f"/actions/workflows/{_APPLY_WORKFLOW_FILE}/dispatches"
        )
        self._dispatch(url, self._headers(), body)
        # dispatch는 204만 돌려주고 run id를 주지 않는다. run 좌표는 (deployment, plan_hash)에서
        # 결정적으로 유도해, 재조회 단계가 이 좌표로 run을 찾을 수 있게 한다.
        return ApplyRunReference(
            deployment_id=approval.deployment_id,
            repository_id=repository_id,
            run_id=_dispatch_run_key(approval.deployment_id, approval.plan_hash),
        )

    def _headers(self) -> dict[str, str]:
        return _github_headers(self._token_provider())


class LiveWorkflowRunReader(WorkflowRunReader):
    """`run_id`로 Actions run을 GET으로 재조회하는 live 어댑터 (ADR-0019 §7).

    EventBridge payload를 신뢰하지 않는다. 재조회 실패(404·형식 오류·네트워크)는 예외가
    아니라 실패 결론을 담은 `VerifiedRunOutcome`으로 반환해, D Worker가 승인 사실과 대조해
    걸러낸다.
    """

    def __init__(
        self,
        *,
        customer_id: str,
        repository_id: str,
        repository_full_name: str,
        token_provider: Callable[[], str],
        request: Callable[[str, Mapping[str, str]], Mapping[str, object]] | None = None,
    ) -> None:
        _require_non_empty(customer_id, "customer_id")
        _require_non_empty(repository_id, "repository_id")
        self._customer_id = customer_id
        self._repository_id = repository_id
        self._repository_full_name = require_github_repository_full_name(repository_full_name)
        if not callable(token_provider):
            raise TypeError("token_provider must be callable")
        self._token_provider = token_provider
        self._request = request or _github_get

    def read_run(self, *, customer_id: str, repository_id: str, run_id: str) -> VerifiedRunOutcome:
        if customer_id != self._customer_id or repository_id != self._repository_id:
            raise LiveDeploymentPortError("customer_id/repository_id is outside the tool scope")
        _require_non_empty(run_id, "run_id")
        url = f"https://api.github.com/repos/{self._repository_full_name}/actions/runs/{run_id}"
        try:
            payload = self._request(url, _github_headers(self._token_provider()))
            return _run_outcome_from_payload(run_id, repository_id, payload)
        except _RunReadFailure:
            return _not_found_outcome(run_id, repository_id)

    def read_run_by_reference(self, reference: ApplyRunReference) -> VerifiedRunOutcome:
        """dispatch가 돌려준 참조로 재조회한다(run_id는 결정적 dispatch 좌표)."""
        if not isinstance(reference, ApplyRunReference):
            raise TypeError("reference must be an ApplyRunReference")
        return self.read_run(
            customer_id=self._customer_id,
            repository_id=reference.repository_id,
            run_id=reference.run_id,
        )


class LiveActualRereadPort(ActualRereadPort):
    """apply 후 AWS Actual을 M1 read-only Resource Tool로 다시 읽는 live 어댑터.

    새 표면이 아니라 M1 Tool 재사용이며 write 표면이 없다(ADR-0007). 재조회 대상은 planned
    집합에서 좁혀 온 `resource_ids`다(ADR-0020 §8). 존재하지 않는 리소스는 조용히 건너뛴다.
    """

    def __init__(
        self,
        *,
        customer_id: str,
        aws_account_id: str,
        resource_tool: AwsResourceTool,
        resource_type: str = "AWS::S3::Bucket",
    ) -> None:
        _require_non_empty(customer_id, "customer_id")
        _require_non_empty(aws_account_id, "aws_account_id")
        _require_non_empty(resource_type, "resource_type")
        if not isinstance(resource_tool, AwsResourceTool):
            raise TypeError("resource_tool must be an AwsResourceTool")
        self._customer_id = customer_id
        self._aws_account_id = aws_account_id
        self._resource_tool = resource_tool
        self._resource_type = resource_type

    def reread_actual(
        self, *, customer_id: str, aws_account_id: str, resource_ids: tuple[str, ...]
    ) -> tuple[AwsResourceSnapshot, ...]:
        if customer_id != self._customer_id or aws_account_id != self._aws_account_id:
            raise LiveDeploymentPortError("customer_id/aws_account_id is outside the tool scope")
        if not isinstance(resource_ids, tuple):
            raise TypeError("resource_ids must be a tuple")
        snapshots: list[AwsResourceSnapshot] = []
        for resource_id in resource_ids:
            _require_non_empty(resource_id, "resource_ids item")
            query = AwsResourceQuery(
                customer_id=self._customer_id,
                aws_account_id=self._aws_account_id,
                operation=AwsResourceOperation.READ_RESOURCE,
                resource_type=self._resource_type,
                resource_id=resource_id,
            )
            try:
                view = self._resource_tool.read_resource(query)
            except AwsResourceToolError:
                # 재조회는 존재하는 Actual만 돌려준다. 없는 리소스는 건너뛴다(ADR-0020 §8).
                continue
            snapshots.append(_snapshot_from_view(self._customer_id, view))
        return tuple(snapshots)


class _RunReadFailure(Exception):
    """내부 신호: run 재조회를 실패 결론 값으로 바꿔야 한다."""


def _run_outcome_from_payload(
    run_id: str, repository_id: str, payload: Mapping[str, object]
) -> VerifiedRunOutcome:
    if not isinstance(payload, Mapping):
        raise _RunReadFailure
    path = payload.get("path")
    head_sha = payload.get("head_sha")
    conclusion = payload.get("conclusion")
    if not isinstance(path, str) or not path.strip():
        raise _RunReadFailure
    if not isinstance(head_sha, str) or not head_sha.strip():
        raise _RunReadFailure
    if not isinstance(conclusion, str) or not conclusion.strip():
        # 아직 완료되지 않은 run(conclusion=null)은 성공도 실패도 아니다. plan_hash를 대조에서
        # 반드시 어긋나게 하는 sentinel로 두어 D Worker가 진행하지 않게 한다.
        raise _RunReadFailure
    plan_hash = _plan_hash_from_run_name(payload.get("name"))
    if plan_hash is None:
        raise _RunReadFailure
    return VerifiedRunOutcome(
        run_id=run_id,
        workflow_path=path,
        repository_id=repository_id,
        ref=head_sha,
        conclusion=conclusion,
        plan_hash=plan_hash,
    )


def _plan_hash_from_run_name(value: object) -> str | None:
    """apply workflow는 run name에 `plan_hash=<hash>`를 담아 승인 사실 대조를 가능케 한다.

    Event를 신뢰하지 않으므로 재조회한 run 자체에서 plan_hash를 얻는다. run name 규약이
    맞지 않으면 대조가 어긋나도록 None을 돌려준다.
    """
    if not isinstance(value, str):
        return None
    marker = "plan_hash="
    index = value.find(marker)
    if index < 0:
        return None
    token = value[index + len(marker) :].split()[0].strip()
    return token or None


def _not_found_outcome(run_id: str, repository_id: str) -> VerifiedRunOutcome:
    return VerifiedRunOutcome(
        run_id=run_id,
        workflow_path="unknown",
        repository_id=repository_id,
        ref="unknown",
        conclusion="not_found",
        plan_hash="unknown",
    )


def _snapshot_from_view(customer_id: str, view: object) -> AwsResourceSnapshot:
    if not isinstance(view, AwsResourceView):
        raise LiveDeploymentPortError("resource tool returned an invalid view")
    # AwsResourceSnapshot의 attributes는 문자열 매핑만 담으므로, 서술적 read 상태를 문자열로
    # 직렬화한다. write handle이 될 수 있는 값은 애초에 view에 없다(ADR-0007).
    attributes = {
        str(key): json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        for key, value in view.to_dict().get("attributes", {}).items()
    }
    return AwsResourceSnapshot(
        customer_id=customer_id,
        aws_account_id=view.aws_account_id,
        resource_type=view.resource_type,
        resource_id=view.resource_id,
        attributes=attributes,
    )


def _dispatch_run_key(deployment_id: str, plan_hash: str) -> str:
    seed = "\x1f".join((deployment_id, plan_hash))
    return "run-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _github_headers(token: object) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {_require_non_empty(token, 'GitHub token')}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _github_get(url: str, headers: Mapping[str, str]) -> Mapping[str, object]:
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310 - fixed GitHub API origin.
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError:
        raise _RunReadFailure from None
    except (URLError, TimeoutError, ValueError):
        raise _RunReadFailure from None
    if not isinstance(payload, Mapping):
        raise _RunReadFailure
    return payload


def _github_post(url: str, headers: Mapping[str, str], body: bytes) -> None:
    request = Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310 - fixed GitHub API origin.
            status = getattr(response, "status", response.getcode())
    except (HTTPError, URLError, TimeoutError) as error:
        raise LiveDeploymentPortError("workflow_dispatch failed") from error
    if status not in (201, 204):
        raise LiveDeploymentPortError(f"workflow_dispatch returned status {status}")


# 재조회 어댑터가 참조하는 apply workflow allow-list를 밖에서도 쓸 수 있게 노출한다.
APPLY_WORKFLOW_PATHS = _APPLY_WORKFLOW_PATHS
