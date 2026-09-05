"""Deterministic M2 C remediation-context assembly from M1 evidence."""

from collections.abc import Iterable

from packages.contracts import (
    EvaluationPerspective,
    EvaluationResult,
    Finding,
    IaCSnapshot,
)
from packages.contracts.remediation import RemediationContext


class RemediationContextError(ValueError):
    """Raised when M1 evidence cannot safely support a remediation handoff."""


def build_remediation_context(
    *,
    finding: Finding,
    snapshot: IaCSnapshot,
    results: Iterable[EvaluationResult],
    source_assessment_id: str | None = None,
) -> RemediationContext:
    """Validate one evidence set without making a remediation action decision.

    Action selection belongs exclusively to B's ``RemediationPolicy``. C keeps
    the evidence and immutable snapshot that the stored decision will consume.

    `results`는 이 Finding의 좌표가 **실제로 평가된** 관점의 결과들이다. 두 관점을 모두 요구하지
    않는다 — authoring이 만든 Rule은 `evaluation_type` 하나를 선언하므로 관점 하나만 평가된다.
    어느 관점이 있고 없는지로 조치 유형을 가르는 것은 `RemediationPolicy.decide()`의 일이다.
    """
    if not isinstance(finding, Finding):
        raise TypeError("finding must be a Finding")
    if not isinstance(snapshot, IaCSnapshot):
        raise TypeError("snapshot must be an IaCSnapshot")
    results = tuple(results)
    _require_matching_results(finding, results)

    evidence = tuple(
        dict.fromkeys(
            (
                *finding.evidence_references,
                *(reference for result in results for reference in result.evidence_references),
            )
        )
    )
    if not evidence:
        raise RemediationContextError("remediation context requires evidence")
    return RemediationContext(
        finding=finding,
        snapshot=snapshot,
        evidence_references=evidence,
        source_assessment_id=source_assessment_id,
    )


def _require_matching_results(finding: Finding, results: tuple[EvaluationResult, ...]) -> None:
    if not results:
        raise RemediationContextError("remediation context requires at least one evaluation result")
    perspectives = [result.perspective for result in results]
    if len(set(perspectives)) != len(perspectives):
        raise RemediationContextError("remediation results must not repeat a perspective")
    if finding.perspective is EvaluationPerspective.DRIFT:
        # DRIFT는 저장된 판정이 아니라 두 관점의 비교다. 그 비교를 뒷받침하려면 둘 다 있어야 한다.
        missing = {EvaluationPerspective.IAC, EvaluationPerspective.AWS_ACTUAL} - set(perspectives)
        if missing:
            raise RemediationContextError(
                "a drift finding requires both the IAC and AWS_ACTUAL results"
            )
    elif finding.perspective not in perspectives:
        # Finding이 나온 그 관점의 결과가 없으면 증거가 서로 어긋난 것이다.
        raise RemediationContextError("remediation results must include the finding's perspective")
    expected = (finding.resource_id, finding.rule_id, finding.rule_version)
    for result in results:
        if (result.resource_id, result.rule_id, result.rule_version) != expected:
            raise RemediationContextError("evaluation result is outside the Finding identity")
