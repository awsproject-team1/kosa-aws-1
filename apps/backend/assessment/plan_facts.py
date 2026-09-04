"""Judge the same declared predicates against a `terraform show -json` plan's `after` values.

**왜 plan인가.** IaC 원문(HCL)은 파일 단위 근거이고 값의 위치가 authoritative하지 않다 — 그래서
IAC 관점은 모델이 판단하고(ADR-0023 §2), 코드 AWS FAIL과 모델 IaC PASS의 조합은 drift가 아니라
`MANUAL_REVIEW`다(ADR-0024 §4). 그런데 plan의 `resource_changes[].change.after`는 provider가
해석을 끝낸 구조화된 값이고, 이 저장소는 그것을 이미 결정적으로 투영해 `plan_hash`를 만든다
(ADR-0019 §1). 그 위에서는 AWS 문서에 선언한 **같은 술어**를 같은 경로 문법으로 판정할 수 있다.

**어디에 쓰는가.** 배포 readiness다. patch → PR → plan → 승인 → apply 흐름에서 "이 patch가
Finding을 실제로 해소하는가"를 apply 전에 코드가 답한다. 답이 FAIL이면 승인을 막는다
(`FINDING_UNRESOLVED_IN_PLAN`). 답할 수 없으면(경로가 plan에 없음, 계산 중, 술어가 plan 모양과
맞지 않음) 아무 신호도 내지 않는다 — "판정 없음"이지 "해소됨"이 아니다.

**AWS 게이트와의 비대칭.** AWS pre-flight는 선언된 경로가 하나라도 비면 근거 부족이다. plan은
다르다: Terraform은 설정하지 않은 block을 아예 내지 않으므로(`ebs_block_device`가 없는 인스턴스),
capability의 plan 경로 중 **값이 하나라도 있으면** 판정하고 하나도 없으면 판정하지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from apps.backend.assessment.deterministic import (
    DeterministicVerdict,
    aws_bindings,
    observe,
    verdict_from_observations,
)
from apps.backend.policy.evidence_paths import document_path_values
from packages.contracts import (
    EvaluationPerspective,
    EvidenceCapabilityBinding,
    GovernanceControl,
    GovernanceControlCatalog,
)
from packages.contracts.terraform_plan import project_plan_changes, resource_identity

PlanEvidence = Mapping[str, Mapping[str, tuple[object, ...]]]


def evidence_location(terraform_resource_type: str, path: str) -> str:
    """The `plan_evidence` key for one declared plan path."""
    return f"{terraform_resource_type}:{path}"


def project_plan_evidence(
    show_json: Mapping[str, object], catalog: GovernanceControlCatalog
) -> dict[str, dict[str, tuple[object, ...]]]:
    """Collect, per Finding-vocabulary resource id, the `after` values at every catalog plan path.

    Catalog가 선언한 위치만 읽는다 — allow-list다. 새 provider attribute가 조용히 근거가 되지
    않고, 저장되는 것은 판정에 필요한 값뿐이다. `after`만 본다: 승인 대상은 바뀐 뒤의 상태다.
    """
    wanted: dict[str, list[str]] = {}
    for control in catalog.controls:
        for binding in control.available_evidence_capabilities:
            for entry in binding.plan_paths:
                wanted.setdefault(entry.terraform_resource_type, []).append(entry.path)
    evidence: dict[str, dict[str, tuple[object, ...]]] = {}
    for change in project_plan_changes(show_json):
        resource_type = change.get("type")
        if not isinstance(resource_type, str) or resource_type not in wanted:
            continue
        resource_id = resource_identity(change)
        if resource_id is None:
            continue
        after = change["change"].get("after") if isinstance(change.get("change"), Mapping) else None
        if not isinstance(after, Mapping):
            continue
        facts = evidence.setdefault(resource_id, {})
        for path in wanted[resource_type]:
            values = document_path_values(after, path)
            if not values:
                continue
            location = evidence_location(resource_type, path)
            facts[location] = (*facts.get(location, ()), *values)
    return evidence


def decide_from_plan_evidence(
    control: GovernanceControl,
    required: Sequence[str],
    *,
    resource_type: str,
    resource_id: str,
    evidence: PlanEvidence,
) -> DeterministicVerdict | None:
    """Apply the Rule's required predicates to the plan facts for one resource, if they are there.

    `None`은 "plan으로는 판정하지 않는다"이다: 요구 capability 중 하나라도 술어나 plan 경로가
    없거나, 그 경로의 값이 plan에 하나도 없으면. 일부만 코드가 판정하는 결과를 만들지 않는다 —
    AWS 쪽 `decidable_bindings_for`와 같은 이유다.
    """
    facts = evidence.get(resource_id)
    if not facts or not required:
        return None
    bindings = aws_bindings(control, resource_type=resource_type)
    observations = []
    for capability_key in required:
        binding = bindings.get(capability_key)
        if binding is None or not binding.is_decidable or not binding.plan_paths:
            return None
        values = _plan_values(binding, facts)
        if not values:
            return None
        observations.extend(observe(binding, path, value) for path, value in values)
    return verdict_from_observations(observations)


def _plan_values(
    binding: EvidenceCapabilityBinding, facts: Mapping[str, tuple[object, ...]]
) -> list[tuple[str, object]]:
    if binding.perspective is not EvaluationPerspective.AWS_ACTUAL:  # pragma: no cover
        return []
    resolved: list[tuple[str, object]] = []
    for entry in binding.plan_paths:
        location = evidence_location(entry.terraform_resource_type, entry.path)
        for value in facts.get(location, ()):
            resolved.append((location, value))
    return resolved
