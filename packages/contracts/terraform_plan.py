"""`terraform show -json` 출력의 허용 목록 투영과 `plan_hash` 산출 (ADR-0019 §1).

이 모듈은 역할 경계를 넘는 **유일한** 산출 근거다. A의 승인 재검증, C의 readiness 바인딩,
D의 apply 직전 재검증이 모두 여기의 같은 함수를 호출한다. 역할마다 다시 구현하면 계산 방식이
어긋나 승인 재검증이 상시 실패한다(ADR-0019 §1, Consequences).

투영 규칙 (ADR-0019 §1):
- 대상은 `resource_changes[]`이며, 각 항목에서 남기는 필드는 열한 개다.
  `address`, `mode`, `type`, `name`, `index`, `provider_name`,
  `change.actions`, `change.before`, `change.after`, `change.after_unknown`,
  `change.replace_paths`.
- 허용 목록으로 정의한다(제외 목록이 아니다). Terraform/Provider가 출력 필드를 늘려도
  투영에 들어오지 않으므로 재현성이 깨지지 않는다. `timestamp`·`format_version`·
  `terraform_version`·`prior_state`는 목록에 없어 자동으로 빠진다.
- 정규화: `address` 기준 정렬, key 정렬, UTF-8, 구분자 `(",", ":")`, 비-ASCII escape,
  trailing newline 없음, NaN/Infinity 금지.
- `plan_hash`는 그 canonical 바이트의 SHA-256이며 `TerraformPlan.artifact.content_sha256`과 같다.

파괴적 변경 판정 (ADR-0019 §1, §8, 불변식 8):
- `change.actions`에 `delete`가 있거나 `change.replace_paths`가 비어 있지 않으면 `True`.
- 이 bool이 `PlanReadinessInput.has_destructive_changes`의 유일한 산출 근거다.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence

# resource_changes[] 항목에서 투영에 남기는 최상위 필드 (change 제외).
_RESOURCE_CHANGE_TOP_FIELDS: tuple[str, ...] = (
    "address",
    "mode",
    "type",
    "name",
    "index",
    "provider_name",
)

# change 객체에서 투영에 남기는 필드.
_CHANGE_FIELDS: tuple[str, ...] = (
    "actions",
    "before",
    "after",
    "after_unknown",
    "replace_paths",
)

# 파괴적 변경으로 취급하는 Terraform action.
_DESTRUCTIVE_ACTION = "delete"


class TerraformPlanProjectionError(ValueError):
    """`terraform show -json` 출력이 투영할 수 없는 형태일 때 발생한다."""


def _reject_non_finite(value: object) -> None:
    """NaN/Infinity를 재귀적으로 거부한다.

    JSON은 이 값들을 표준으로 표현하지 못하고, `json.dumps`의 기본 동작은 `NaN`/`Infinity`
    literal을 흘려보내 산출 hash를 실행 환경에 따라 갈리게 한다. 투영은 닫힌 값 집합이어야
    하므로 발견 즉시 fail-closed한다(ADR-0019 §1).
    """
    if isinstance(value, bool):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TerraformPlanProjectionError("plan contains a non-finite number")
        return
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_non_finite(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _reject_non_finite(item)


def _project_change(change: object) -> dict[str, object]:
    """단일 `resource_changes[].change`를 허용 필드로 투영한다."""
    if not isinstance(change, Mapping):
        raise TerraformPlanProjectionError("resource_changes[].change must be an object")
    projected: dict[str, object] = {}
    for field in _CHANGE_FIELDS:
        if field in change:
            value = change[field]
            _reject_non_finite(value)
            projected[field] = value
    if "actions" not in projected:
        # actions 없이는 파괴성/변경 종류를 판정할 수 없으므로 투영을 신뢰할 수 없다.
        raise TerraformPlanProjectionError("resource_changes[].change.actions is required")
    return projected


def _project_resource_change(resource_change: object) -> dict[str, object]:
    """단일 `resource_changes[]` 항목을 허용 필드로 투영한다."""
    if not isinstance(resource_change, Mapping):
        raise TerraformPlanProjectionError("resource_changes[] item must be an object")
    projected: dict[str, object] = {}
    for field in _RESOURCE_CHANGE_TOP_FIELDS:
        if field in resource_change:
            value = resource_change[field]
            _reject_non_finite(value)
            projected[field] = value
    if "address" not in projected or not isinstance(projected["address"], str):
        # address는 정렬 key이자 리소스 식별자이므로 반드시 문자열로 있어야 한다.
        raise TerraformPlanProjectionError("resource_changes[] item requires a string address")
    projected["change"] = _project_change(resource_change.get("change"))
    return projected


def project_plan(plan_json: Mapping[str, object]) -> list[dict[str, object]]:
    """`terraform show -json` 출력을 허용 목록 `resource_changes[]`로 투영한다.

    반환값은 `address`로 정렬된 투영 항목의 리스트다. 실제 hash 대상 바이트는
    `canonical_plan_bytes`가 이 리스트를 canonical JSON으로 직렬화해 만든다.
    """
    if not isinstance(plan_json, Mapping):
        raise TerraformPlanProjectionError("plan JSON must be an object")
    resource_changes = plan_json.get("resource_changes")
    if resource_changes is None:
        # 변경이 없는 plan도 유효하다(빈 리스트). 그러나 key 자체가 없으면 형태를 신뢰할 수 없다.
        raise TerraformPlanProjectionError("plan JSON requires a resource_changes array")
    if not isinstance(resource_changes, Sequence) or isinstance(resource_changes, (str, bytes)):
        raise TerraformPlanProjectionError("resource_changes must be an array")
    projected = [_project_resource_change(item) for item in resource_changes]
    addresses = [item["address"] for item in projected]
    if len(set(addresses)) != len(addresses):
        # address는 plan 안에서 유일하다. 중복은 손상된 출력이다.
        raise TerraformPlanProjectionError("resource_changes[] addresses must be unique")
    projected.sort(key=lambda item: item["address"])
    return projected


def canonical_plan_bytes(plan_json: Mapping[str, object]) -> bytes:
    """투영된 plan을 ADR-0019 §1 정규화 규칙에 따라 canonical JSON 바이트로 만든다."""
    projected = project_plan(plan_json)
    text = json.dumps(
        projected,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return text.encode("utf-8")


def compute_plan_hash(plan_json: Mapping[str, object]) -> str:
    """canonical 투영 바이트의 SHA-256 hex digest를 반환한다.

    이 값이 `TerraformPlan.plan_hash`이자 `TerraformPlan.artifact.content_sha256`이다.
    같은 plan에서 두 번 계산해도 같아야 한다(ADR-0019 불변식 2).
    """
    return hashlib.sha256(canonical_plan_bytes(plan_json)).hexdigest()


def has_destructive_changes(plan_json: Mapping[str, object]) -> bool:
    """파괴적 변경 존재 여부를 판정한다 (ADR-0019 §1, 불변식 8).

    `change.actions`에 `delete`가 있거나 `change.replace_paths`가 비어 있지 않으면 `True`.
    이 bool은 `PlanReadinessInput.has_destructive_changes`의 유일한 산출 근거이며,
    C의 readiness 게이트(`DESTRUCTIVE_CHANGE_REQUIRES_MANUAL_REVIEW`)가 이를 읽는다.
    """
    for resource_change in project_plan(plan_json):
        change = resource_change["change"]
        assert isinstance(change, dict)
        actions = change.get("actions")
        if isinstance(actions, Sequence) and _DESTRUCTIVE_ACTION in actions:
            return True
        replace_paths = change.get("replace_paths")
        if isinstance(replace_paths, Sequence) and len(replace_paths) > 0:
            return True
    return False
