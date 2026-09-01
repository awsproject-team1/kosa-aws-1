"""Deterministic M2 C remediation-context derivation from M1 evidence."""

from packages.contracts import (
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    Finding,
    IaCSnapshot,
)
from packages.contracts.remediation import RemediationContext, RemediationStrategy


class RemediationContextError(ValueError):
    """Raised when M1 evidence cannot safely support a remediation handoff."""


def build_remediation_context(
    *,
    finding: Finding,
    snapshot: IaCSnapshot,
    iac_result: EvaluationResult,
    actual_result: EvaluationResult,
) -> RemediationContext:
    """Derive a D handoff without reinterpreting or inventing M1 evidence."""
    if not isinstance(finding, Finding):
        raise TypeError("finding must be a Finding")
    if not isinstance(snapshot, IaCSnapshot):
        raise TypeError("snapshot must be an IaCSnapshot")
    _require_matching_pair(finding, iac_result, actual_result)

    if iac_result.status is EvaluationStatus.FAIL:
        strategy = RemediationStrategy.PATCH_IAC
    elif (
        iac_result.status is EvaluationStatus.PASS and actual_result.status is EvaluationStatus.FAIL
    ):
        strategy = RemediationStrategy.SYNC_CURRENT_IAC
    else:
        strategy = RemediationStrategy.MANUAL_REVIEW

    evidence = tuple(
        dict.fromkeys(
            (
                *finding.evidence_references,
                *iac_result.evidence_references,
                *actual_result.evidence_references,
            )
        )
    )
    if not evidence:
        raise RemediationContextError("remediation context requires evidence")
    return RemediationContext(
        finding=finding,
        snapshot=snapshot,
        strategy=strategy,
        evidence_references=evidence,
    )


def _require_matching_pair(
    finding: Finding, iac_result: EvaluationResult, actual_result: EvaluationResult
) -> None:
    if iac_result.perspective is not EvaluationPerspective.IAC:
        raise RemediationContextError("iac_result must have IAC perspective")
    if actual_result.perspective is not EvaluationPerspective.AWS_ACTUAL:
        raise RemediationContextError("actual_result must have AWS_ACTUAL perspective")
    expected = (finding.resource_id, finding.rule_id, finding.rule_version)
    for result in (iac_result, actual_result):
        if (result.resource_id, result.rule_id, result.rule_version) != expected:
            raise RemediationContextError("evaluation result is outside the Finding identity")
