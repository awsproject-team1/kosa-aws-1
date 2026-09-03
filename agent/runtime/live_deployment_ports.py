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
    PlanRequestPort,
    WorkflowRunReader,
)
from packages.contracts import (
    ApplyDispatchReceipt,
    ArtifactReference,
    ArtifactType,
    AwsResourceOperation,
    AwsResourceQuery,
    DeploymentApproval,
    PlanExecutionResult,
    PlanProjectionError,
    PlanSummary,
    TerraformPlan,
    TerraformStateVersion,
    WorkflowConclusion,
    WorkflowRunFacts,
    WorkflowRunReference,
    compute_plan_hash,
    has_destructive_changes,
    mapped_resource_ids,
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


class PlanRunOutputs:
    """`terraform-plan` run이 남긴 산출물의 회수 결과(어댑터 내부 전달용).

    plan workflow는 saved binary plan, canonical projection(그 SHA-256이 `plan_hash`),
    plan 시점 state `(lineage, serial)`를 artifact로 올린다(`ci/terraform/terraform-plan.yml`).
    이 값 묶음을 GitHub 호출 세부에서 분리해 어댑터가 scope 검증·결과 조립만 하도록 한다.

    `canonical_changes`는 회수한 `plan.canonical.json`의 내용 그대로다. 어댑터가 이 값에서
    readiness 요약을 파생하고 그 digest가 `plan_hash`와 일치하는지 대조하므로, 요약은 항상
    hash된 바로 그 바이트에서 나온다.
    """

    __slots__ = (
        "run_id",
        "plan_hash",
        "binary_sha256",
        "state_lineage",
        "state_serial",
        "canonical_changes",
        "refreshed",
    )

    def __init__(
        self,
        *,
        run_id: str,
        plan_hash: str,
        binary_sha256: str,
        state_lineage: str,
        state_serial: int,
        canonical_changes: Sequence[Mapping[str, object]],
        refreshed: bool,
    ) -> None:
        self.run_id = _require_non_empty(run_id, "run_id")
        self.plan_hash = _require_non_empty(plan_hash, "plan_hash")
        self.binary_sha256 = _require_non_empty(binary_sha256, "binary_sha256")
        self.state_lineage = _require_non_empty(state_lineage, "state_lineage")
        if isinstance(state_serial, bool) or not isinstance(state_serial, int):
            raise TypeError("state_serial must be an integer")
        self.state_serial = state_serial
        if isinstance(canonical_changes, (str, bytes)) or not isinstance(
            canonical_changes, Sequence
        ):
            raise TypeError("canonical_changes must be a list of resource changes")
        self.canonical_changes = list(canonical_changes)
        if not isinstance(refreshed, bool):
            raise TypeError("refreshed must be a bool")
        self.refreshed = refreshed


class LivePlanRequestPort(PlanRequestPort):
    """승인 대상 commit에 refreshed Terraform plan을 실행하는 live 어댑터(ADR-0019 §1·§2).

    D Worker가 `RUN_DEPLOYMENT`에서 호출한다. 실제 GitHub 호출(plan `workflow_dispatch`, run 완료
    폴링, artifact 다운로드)은 주입된 `fetch_outputs` 콜백에 위임하고, 이 어댑터는 (customer_id,
    repository_id) scope 강제와 `PlanExecutionResult` 조립·정합성만 책임진다. apply가 이 run의 saved
    plan artifact를 내려받으므로(§1), 반환값의 `plan_run`은 이 plan run 좌표다. plan은 별도 실행이라
    dispatch가 run_id를 즉시 주지 않으므로, `fetch_outputs`가 run을 찾아 완료를 확인한 뒤 run_id를
    포함한 `PlanRunOutputs`를 돌려준다. write 표면은 plan `workflow_dispatch` 하나뿐이다(§6).
    """

    def __init__(
        self,
        *,
        customer_id: str,
        repository_id: str,
        repository_full_name: str,
        fetch_outputs: Callable[[str, str], PlanRunOutputs],
        artifact_id_factory: Callable[[str, str], str] | None = None,
    ) -> None:
        self._customer_id = _require_non_empty(customer_id, "customer_id")
        self._repository_id = _require_non_empty(repository_id, "repository_id")
        self._repository_full_name = require_github_repository_full_name(repository_full_name)
        if not callable(fetch_outputs):
            raise TypeError("fetch_outputs must be callable")
        self._fetch_outputs = fetch_outputs
        # artifact_id는 결정적으로 유도한다(같은 배포·commit이면 같은 id). 재실행이 새 artifact
        # 참조를 만들지 않게 하려는 것으로, 실제 저장 내용이 아니라 참조 식별자다.
        self._artifact_id_factory = artifact_id_factory or (
            lambda kind, deployment_id: f"{kind}-{deployment_id}"
        )

    def request_plan(
        self, *, customer_id: str, deployment_id: str, repository_id: str, commit_sha: str
    ) -> PlanExecutionResult:
        if customer_id != self._customer_id or repository_id != self._repository_id:
            raise LiveDeploymentPortError("customer_id/repository_id is outside the tool scope")
        _require_non_empty(deployment_id, "deployment_id")
        _require_non_empty(commit_sha, "commit_sha")
        # GitHub 호출(dispatch → run 완료 폴링 → artifact 회수)은 콜백이 담당한다. 콜백은 이
        # deployment/commit의 plan run을 식별해 완료를 확인하고 산출물을 돌려줘야 한다.
        outputs = self._fetch_outputs(deployment_id, commit_sha)
        if not isinstance(outputs, PlanRunOutputs):
            raise LiveDeploymentPortError("fetch_outputs must return PlanRunOutputs")
        plan_artifact = ArtifactReference(
            artifact_id=self._artifact_id_factory("terraform-plan", deployment_id),
            artifact_type=ArtifactType.TERRAFORM_PLAN,
            content_sha256=outputs.plan_hash,
            customer_id=self._customer_id,
            repository_id=self._repository_id,
        )
        plan = TerraformPlan(
            deployment_id=deployment_id,
            commit_sha=commit_sha,
            plan_hash=outputs.plan_hash,
            artifact=plan_artifact,
        )
        binary_artifact = ArtifactReference(
            artifact_id=self._artifact_id_factory("terraform-plan-binary", deployment_id),
            artifact_type=ArtifactType.TERRAFORM_PLAN_BINARY,
            content_sha256=outputs.binary_sha256,
            customer_id=self._customer_id,
            repository_id=self._repository_id,
        )
        state_version = TerraformStateVersion(
            lineage=outputs.state_lineage, serial=outputs.state_serial
        )
        plan_run = WorkflowRunReference(
            deployment_id=deployment_id,
            repository_id=self._repository_id,
            run_id=outputs.run_id,
        )
        # PlanExecutionResult.__post_init__이 binary와 plan_run의 deployment/repository scope를
        # 다시 대조하므로, 여기서 조립한 값이 서로 어긋나면 그 시점에 fail-closed된다.
        return PlanExecutionResult(
            plan=plan,
            binary_artifact=binary_artifact,
            state_version=state_version,
            plan_run=plan_run,
            summary=self._summary(outputs),
        )

    def _summary(self, outputs: PlanRunOutputs) -> PlanSummary:
        """Derive the readiness summary from the same bytes that produced `plan_hash`.

        The digest is re-computed from the recovered canonical changes and compared with
        the run's reported `plan_hash`. Without that check the summary could describe a
        different plan than the one being approved — the approval gate re-verifies the
        hash, not the summary, so a mismatched summary would never be caught later.
        """
        document = {"resource_changes": outputs.canonical_changes}
        try:
            recomputed = compute_plan_hash(document)
        except PlanProjectionError as error:
            raise LiveDeploymentPortError("recovered canonical plan is not projectable") from error
        if recomputed != outputs.plan_hash:
            raise LiveDeploymentPortError("recovered canonical plan does not match plan_hash")
        return PlanSummary(
            refreshed=outputs.refreshed,
            has_destructive_changes=has_destructive_changes(document),
            mapped_resource_ids=mapped_resource_ids(document),
        )


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
        plan_run: WorkflowRunReference,
    ) -> ApplyDispatchReceipt:
        if not isinstance(approval, DeploymentApproval):
            raise TypeError("approval must be a DeploymentApproval")
        if not isinstance(plan, TerraformPlan):
            raise TypeError("plan must be a TerraformPlan")
        if not isinstance(state_version, TerraformStateVersion):
            raise TypeError("state_version must be a TerraformStateVersion")
        if not isinstance(plan_run, WorkflowRunReference):
            raise TypeError("plan_run must be a WorkflowRunReference")
        if not approval.matches(plan):
            raise LiveDeploymentPortError("approval is not bound to the plan")
        if plan.artifact.repository_id not in (None, self._repository_id):
            raise LiveDeploymentPortError("plan is outside the tool scope")
        # apply는 이 run의 saved plan artifact를 내려받는다(§1). run 좌표가 승인된 배포·저장소
        # 밖이면 다른 배포의 plan을 적용하게 되므로 dispatch 전에 막는다.
        if plan_run.deployment_id != approval.deployment_id:
            raise LiveDeploymentPortError("plan run is not bound to the approved deployment")
        if plan_run.repository_id != self._repository_id:
            raise LiveDeploymentPortError("plan run is outside the tool scope")

        # workflow_dispatch input 넷은 apply workflow의 필수 입력과 정확히 일치한다(§5).
        # `plan_run_id`는 apply가 자기 run이 아니라 plan run의 saved artifact를 내려받기 때문에
        # 필요하다(§1). 이 값은 durable `PlanExecutionResult.plan_run`에서 와야 하며, 여기서
        # 만들어내지 않는다 — apply와 plan은 서로 다른 실행이다.
        body = json.dumps(
            {
                "ref": approval.commit_sha,
                "inputs": {
                    "deployment_id": approval.deployment_id,
                    "commit_sha": approval.commit_sha,
                    "plan_hash": approval.plan_hash,
                    "plan_run_id": plan_run.run_id,
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
