"""M3 D 실행 port의 live GitHub/AWS 어댑터 (ADR-0019).

`apps/backend/deployment/ports.py`가 정의한 정본 port의 실제 구현이다. Mock과 달리 실제
GitHub Actions REST API와 read-only AWS Resource Tool을 호출하지만, 어떤 어댑터도 승인·정책
판정을 하지 않고 승인 경계를 우회하는 write 표면을 노출하지 않는다.

- `LiveApplyDispatchPort`: 승인된 approval로 `workflow_dispatch`만 호출한다(ADR-0019 §5·§6).
  이 dispatch가 D의 유일한 write 표면이다. dispatch는 run_id를 주지 않으므로 `ApplyDispatchReceipt`
  (workflow_path 확인)만 돌려준다. 권위 있는 apply 사실은 `WorkflowRunReader`가 run을 재조회해
  얻는다(§7). GitHub App에는 `workflows: write`가 없다.
- `LiveWorkflowRunReader`: `WorkflowRunReference`(deployment_id/repository_id/**실제 GitHub run_id**)
  로 Actions run을 GET 재조회한다(ADR-0019 §7). EventBridge payload를 신뢰하지 않는다. 재조회
  실패는 예외가 아니라 실패 결론(`FAILURE`)을 담은 `WorkflowRunFacts`로 반환한다.
- `LiveActualRereadPort`: apply 후 AWS Actual을 M1 read-only Resource Tool로 다시 읽어 검증
  Assessment 입력으로 넘긴다(ADR-0007, ADR-0020). 반환값이 없다(정본 port가 `None`).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agent.runtime.aws_resource_tool import AwsResourceTool, AwsResourceView
from agent.runtime.github_tool import require_github_repository_full_name
from apps.backend.deployment.ports import (
    ActualRereadPort,
    ApplyDispatchPort,
    WorkflowRunReader,
)
from packages.contracts import (
    ApplyDispatchReceipt,
    AwsResourceOperation,
    AwsResourceQuery,
    DeploymentApproval,
    TerraformPlan,
    TerraformStateVersion,
    WorkflowConclusion,
    WorkflowRunFacts,
    WorkflowRunReference,
)
from packages.contracts.remediation import RemediationSyncTarget

# apply run을 트리거하는 workflow 파일. GitHub App은 이 파일을 만들거나 수정할 수 없고(§6),
# 고객이 1회 설치한 template만 dispatch한다.
_APPLY_WORKFLOW_FILE = "terraform-apply.yml"
_APPLY_WORKFLOW_PATH = ".github/workflows/terraform-apply.yml"

# GitHub Actions conclusion 문자열 → 정본 WorkflowConclusion.
_CONCLUSION_MAP = {
    "success": WorkflowConclusion.SUCCESS,
    "failure": WorkflowConclusion.FAILURE,
    "cancelled": WorkflowConclusion.CANCELLED,
    "timed_out": WorkflowConclusion.TIMED_OUT,
}


class LiveDeploymentPortError(RuntimeError):
    """live 실행 port 어댑터 작업의 기본 실패 타입."""


def _require_non_empty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


class LiveApplyDispatchPort(ApplyDispatchPort):
    """승인된 approval로 GitHub Actions `workflow_dispatch`만 호출하는 live 어댑터.

    write 표면은 dispatch 하나뿐이다. dispatch는 run_id를 주지 않으므로 `ApplyDispatchReceipt`
    (workflow_path 확인)만 돌려준다. 이중 apply의 정본 방어는 A의 `APPROVED → APPLYING`
    조건부 전이다(§5).
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
        self._customer_id = _require_non_empty(customer_id, "customer_id")
        self._repository_id = _require_non_empty(repository_id, "repository_id")
        self._repository_full_name = require_github_repository_full_name(repository_full_name)
        if not callable(token_provider):
            raise TypeError("token_provider must be callable")
        self._token_provider = token_provider
        self._dispatch = dispatch or _github_post

    def dispatch_apply(
        self,
        *,
        approval: DeploymentApproval,
        plan: TerraformPlan,
        state_version: TerraformStateVersion,
    ) -> ApplyDispatchReceipt:
        if not isinstance(approval, DeploymentApproval):
            raise TypeError("approval must be a DeploymentApproval")
        if not isinstance(plan, TerraformPlan):
            raise TypeError("plan must be a TerraformPlan")
        if not isinstance(state_version, TerraformStateVersion):
            raise TypeError("state_version must be a TerraformStateVersion")
        if not approval.matches(plan):
            raise LiveDeploymentPortError("approval is not bound to the plan")
        if plan.artifact.repository_id not in (None, self._repository_id):
            raise LiveDeploymentPortError("plan is outside the tool scope")

        # workflow_dispatch input은 deployment_id/commit_sha/plan_hash뿐이다(§5). workflow가
        # 이 값으로 자신이 적용할 saved plan artifact를 조회·검증한다.
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
        self._dispatch(url, _github_headers(self._token_provider()), body)
        # dispatch는 204만 돌려주고 run id를 주지 않는다. 권위 있는 사실은 재조회로 얻는다(§7).
        return ApplyDispatchReceipt(
            deployment_id=approval.deployment_id,
            repository_id=self._repository_id,
            workflow_path=_APPLY_WORKFLOW_PATH,
        )


class LiveWorkflowRunReader(WorkflowRunReader):
    """`WorkflowRunReference`의 실제 run_id로 Actions run을 GET 재조회하는 live 어댑터 (§7).

    EventBridge payload를 신뢰하지 않는다. 재조회 실패(404·형식 오류·미완료)는 예외가 아니라
    실패 결론(`FAILURE`)을 담은 `WorkflowRunFacts`로 반환해, D Worker가 승인 사실과 대조해
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
        self._customer_id = _require_non_empty(customer_id, "customer_id")
        self._repository_id = _require_non_empty(repository_id, "repository_id")
        self._repository_full_name = require_github_repository_full_name(repository_full_name)
        if not callable(token_provider):
            raise TypeError("token_provider must be callable")
        self._token_provider = token_provider
        self._request = request or _github_get

    def read_run(self, reference: WorkflowRunReference) -> WorkflowRunFacts:
        if not isinstance(reference, WorkflowRunReference):
            raise TypeError("reference must be a WorkflowRunReference")
        if reference.repository_id != self._repository_id:
            raise LiveDeploymentPortError("reference repository_id is outside the tool scope")
        url = (
            f"https://api.github.com/repos/{self._repository_full_name}"
            f"/actions/runs/{reference.run_id}"
        )
        try:
            payload = self._request(url, _github_headers(self._token_provider()))
            return _facts_from_payload(reference, payload)
        except _RunReadFailure:
            return _failure_facts(reference)


class LiveActualRereadPort(ActualRereadPort):
    """apply 후 AWS Actual을 M1 read-only Resource Tool로 다시 읽는 live 어댑터.

    정본 port는 반환값이 없다(`None`) — 재조회한 Actual은 검증 Assessment가 다시 평가한다
    (ADR-0020). 이 어댑터는 승인된 (customer_id, aws_account_id) scope 안에서 주입된 read-only
    Resource Tool로 `resource_types`를 `LIST_RESOURCES` 조회하고, 그 결과를 검증 입력으로 넘기는
    콜백에 전달한다. write 표면이 없다(ADR-0007). 조회 자체가 이 어댑터의 작업이므로 publish
    콜백 유무와 무관하게 Actual 재조회가 실제로 수행된다.
    """

    def __init__(
        self,
        *,
        customer_id: str,
        aws_account_id: str,
        resource_tool: AwsResourceTool,
        resource_types: Sequence[str],
        publish: (
            Callable[[str, RemediationSyncTarget, Sequence[AwsResourceView]], None] | None
        ) = None,
    ) -> None:
        self._customer_id = _require_non_empty(customer_id, "customer_id")
        self._aws_account_id = _require_non_empty(aws_account_id, "aws_account_id")
        if not isinstance(resource_tool, AwsResourceTool):
            raise TypeError("resource_tool must be an AwsResourceTool")
        # 재조회할 resource type이 없으면 조회 자체가 no-op가 되어 apply 후 Actual이 갱신되지
        # 않는다. 최소 하나의 type을 요구해 이 port가 항상 실제 읽기를 수행하게 강제한다.
        if isinstance(resource_types, str) or not isinstance(resource_types, Sequence):
            raise TypeError("resource_types must be a sequence of resource type strings")
        frozen_types = tuple(
            _require_non_empty(item, "resource_types item") for item in resource_types
        )
        if not frozen_types:
            raise ValueError("resource_types must not be empty")
        self._resource_tool = resource_tool
        self._resource_types = frozen_types
        self._publish = publish

    def reread_actual(
        self, *, customer_id: str, deployment_id: str, sync_target: RemediationSyncTarget
    ) -> None:
        if customer_id != self._customer_id:
            raise LiveDeploymentPortError("customer_id is outside the tool scope")
        _require_non_empty(deployment_id, "deployment_id")
        if not isinstance(sync_target, RemediationSyncTarget):
            raise TypeError("sync_target must be a RemediationSyncTarget")
        if sync_target.customer_id != self._customer_id:
            raise LiveDeploymentPortError("sync_target is outside the tool scope")
        # 승인된 scope 안에서 실제 Actual을 read-only로 다시 읽는다. 이 조회가 이 어댑터의
        # 유일한 작업이며, 평가/판정은 하지 않는다(ADR-0007·ADR-0020).
        views: list[AwsResourceView] = []
        for resource_type in self._resource_types:
            query = AwsResourceQuery(
                customer_id=self._customer_id,
                aws_account_id=self._aws_account_id,
                operation=AwsResourceOperation.LIST_RESOURCES,
                resource_type=resource_type,
            )
            views.extend(self._resource_tool.list_resources(query))
        # 재조회 결과는 검증 Assessment 입력으로 넘긴다. publish 콜백이 없으면 재조회는 수행되되
        # 결과가 관측되지 않을 뿐, 조회 자체는 항상 일어난다.
        if self._publish is not None:
            self._publish(deployment_id, sync_target, tuple(views))


class _RunReadFailure(Exception):
    """내부 신호: run 재조회를 실패 결론 값으로 바꿔야 한다."""


def _facts_from_payload(
    reference: WorkflowRunReference, payload: Mapping[str, object]
) -> WorkflowRunFacts:
    if not isinstance(payload, Mapping):
        raise _RunReadFailure
    path = payload.get("path")
    head_sha = payload.get("head_sha")
    raw_conclusion = payload.get("conclusion")
    if not isinstance(path, str) or not path.strip():
        raise _RunReadFailure
    if not isinstance(head_sha, str) or not head_sha.strip():
        raise _RunReadFailure
    if not isinstance(raw_conclusion, str):
        # conclusion=null(진행 중)이면 성공도 실패도 아니다. 실패 값으로 떨어뜨린다.
        raise _RunReadFailure
    conclusion = _CONCLUSION_MAP.get(raw_conclusion.lower())
    if conclusion is None:
        raise _RunReadFailure
    plan_hash = _plan_hash_from_run_name(payload.get("name"))
    if plan_hash is None:
        raise _RunReadFailure
    return WorkflowRunFacts(
        run_id=reference.run_id,
        repository_id=reference.repository_id,
        workflow_path=path,
        ref=head_sha,
        commit_sha=head_sha,
        conclusion=conclusion,
        plan_hash=plan_hash,
    )


def _plan_hash_from_run_name(value: object) -> str | None:
    """apply workflow는 run name에 `plan_hash=<hash>`를 담아 승인 사실 대조를 가능케 한다(§7)."""
    if not isinstance(value, str):
        return None
    marker = "plan_hash="
    index = value.find(marker)
    if index < 0:
        return None
    token = value[index + len(marker) :].split()[0].strip()
    return token or None


def _failure_facts(reference: WorkflowRunReference) -> WorkflowRunFacts:
    return WorkflowRunFacts(
        run_id=reference.run_id,
        repository_id=reference.repository_id,
        workflow_path="unknown",
        ref="unknown",
        commit_sha="unknown",
        conclusion=WorkflowConclusion.FAILURE,
        plan_hash="unknown",
    )


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


# apply workflow allow-list를 밖에서도 쓸 수 있게 노출한다(worker가 대조에 재사용).
APPLY_WORKFLOW_PATHS = frozenset(
    {".github/workflows/terraform-apply.yml", ".github/workflows/terraform-apply.yaml"}
)
