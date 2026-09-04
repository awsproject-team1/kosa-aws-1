"""Deterministic DRIFT derivation from the IAC and AWS_ACTUAL perspectives.

`docs/API.md` requires an Initial Assessment to distinguish `IAC`, `AWS_ACTUAL`, and
`DRIFT` for the same managed target, and ADR-0011 defines `DRIFT` as the mismatch
between the approved IaC and the live AWS Actual state.

Drift is a mechanical comparison, not an AI judgement: the two compliance decisions
either agree or they do not.  Deriving it in code keeps the AI boundary limited to
the per-perspective evaluations that already passed contract validation, and it
keeps `DRIFT` reproducible for the same pair of immutable results.
"""

from __future__ import annotations

from packages.contracts import (
    DecisionSource,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    ScoringMode,
)


class DriftDerivationError(ValueError):
    """Raised when two perspectives cannot be compared for the same rule."""


# A decisive status states whether the perspective satisfies the rule. The remaining
# statuses carry no compliance decision, so they cannot prove or disprove drift.
_COMPLIANT = frozenset({EvaluationStatus.PASS})
_NON_COMPLIANT = frozenset({EvaluationStatus.FAIL})
_ALIGNED_SCORE = 100.0
_DRIFTED_SCORE = 0.0


def derive_drift_results(
    *,
    iac_results: tuple[EvaluationResult, ...],
    actual_results: tuple[EvaluationResult, ...],
) -> tuple[EvaluationResult, ...]:
    """Return one `DRIFT` result per rule evaluated on both sides of the same resource.

    Rules evaluated on only one side produce `MANUAL_REVIEW` because the assessment
    cannot claim alignment it did not observe. `EXECUTION_ERROR` on either side
    propagates so Coverage keeps the evaluation in the planned denominator without
    counting it as completed.
    """
    iac = _by_rule(iac_results, EvaluationPerspective.IAC)
    actual = _by_rule(actual_results, EvaluationPerspective.AWS_ACTUAL)
    return tuple(
        _drift_result(iac.get(key), actual.get(key)) for key in _ordered_keys(iac_results, actual)
    )


def _ordered_keys(
    iac_results: tuple[EvaluationResult, ...],
    actual: dict[tuple[str, str, str], EvaluationResult],
) -> tuple[tuple[str, str, str], ...]:
    """Keep the IaC rule order and append rules only the Actual side evaluated."""
    keys: list[tuple[str, str, str]] = []
    for result in iac_results:
        key = _key(result)
        if key not in keys:
            keys.append(key)
    for key in actual:
        if key not in keys:
            keys.append(key)
    return tuple(keys)


def _by_rule(
    results: tuple[EvaluationResult, ...], perspective: EvaluationPerspective
) -> dict[tuple[str, str, str], EvaluationResult]:
    if not isinstance(results, tuple):
        raise TypeError("results must be a tuple")
    indexed: dict[tuple[str, str, str], EvaluationResult] = {}
    for result in results:
        if not isinstance(result, EvaluationResult):
            raise TypeError("results must contain EvaluationResult values")
        if result.perspective is not perspective:
            raise DriftDerivationError(f"result perspective must be {perspective.value}")
        key = _key(result)
        if key in indexed:
            raise DriftDerivationError("duplicate Resource × Rule result cannot be compared")
        indexed[key] = result
    return indexed


def _key(result: EvaluationResult) -> tuple[str, str, str]:
    return (result.resource_id, result.rule_id, result.rule_version)


def _drift_result(
    iac: EvaluationResult | None, actual: EvaluationResult | None
) -> EvaluationResult:
    present = iac if actual is None else actual
    if present is None:  # pragma: no cover - a key always comes from one side.
        raise DriftDerivationError("drift requires at least one evaluated perspective")
    if iac is not None and actual is not None:
        if iac.severity != actual.severity:
            raise DriftDerivationError("perspective severities disagree for the same rule")
        if iac.model_profile_id != actual.model_profile_id:
            raise DriftDerivationError("perspectives were evaluated under different model profiles")
        if iac.rubric_version != actual.rubric_version:
            raise DriftDerivationError("perspectives were evaluated under different rubrics")
    status, score, rationale = _decision(iac, actual)
    return EvaluationResult(
        resource_id=present.resource_id,
        rule_id=present.rule_id,
        perspective=EvaluationPerspective.DRIFT,
        status=status,
        severity=present.severity,
        score=score,
        rationale=rationale,
        evidence_references=_evidence(iac, actual),
        rule_version=present.rule_version,
        rubric_version=present.rubric_version,
        model_profile_id=present.model_profile_id,
        scoring_mode=ScoringMode.CONTINUOUS,
        decided_by=DecisionSource.CODE,
    )


def _decision(
    iac: EvaluationResult | None, actual: EvaluationResult | None
) -> tuple[EvaluationStatus, float, str]:
    if iac is None or actual is None:
        missing = "IAC" if iac is None else "AWS_ACTUAL"
        return (
            EvaluationStatus.MANUAL_REVIEW,
            _DRIFTED_SCORE,
            f"The {missing} perspective produced no result, so drift cannot be decided.",
        )
    if EvaluationStatus.EXECUTION_ERROR in {iac.status, actual.status}:
        return (
            EvaluationStatus.EXECUTION_ERROR,
            _DRIFTED_SCORE,
            "A perspective failed to execute, so drift was not evaluated.",
        )
    if (
        iac.status is EvaluationStatus.OUT_OF_SCOPE
        and actual.status is EvaluationStatus.OUT_OF_SCOPE
    ):
        return (
            EvaluationStatus.OUT_OF_SCOPE,
            _ALIGNED_SCORE,
            "The rule is out of scope for both the IaC and the AWS Actual perspective.",
        )
    iac_decisive = _decisive(iac.status)
    actual_decisive = _decisive(actual.status)
    if iac_decisive is None or actual_decisive is None:
        return (
            EvaluationStatus.MANUAL_REVIEW,
            _DRIFTED_SCORE,
            "At least one perspective returned no compliance decision, so drift needs review.",
        )
    if iac_decisive == actual_decisive:
        return (
            EvaluationStatus.PASS,
            _ALIGNED_SCORE,
            "The approved IaC and the AWS Actual state agree on this rule.",
        )
    if iac.decided_by is not actual.decided_by:
        # 두 판정의 근거 체계가 다르다: 한쪽은 코드가 선언된 값을 읽었고, 다른 쪽은 모델이
        # 문언을 해석했다. 측정에서 모델은 부분 준수를 PASS로 보는 false negative를 냈고, 그
        # 조합은 양쪽 모두 비준수인 리소스를 "IaC는 만족하나 AWS는 아니다"라는 실재하지 않는
        # drift로 보고했다. 근거 체계가 다른 불일치를 사실로 주장하지 않는다 — 사람이 본다.
        return (
            EvaluationStatus.MANUAL_REVIEW,
            _DRIFTED_SCORE,
            "The two perspectives disagree, but one was decided by code from declared "
            "evidence and the other by the model; the disagreement needs review before "
            "it is reported as drift.",
        )
    if iac_decisive:
        return (
            EvaluationStatus.FAIL,
            _DRIFTED_SCORE,
            "The approved IaC satisfies this rule but the AWS Actual state does not.",
        )
    return (
        EvaluationStatus.FAIL,
        _DRIFTED_SCORE,
        "The AWS Actual state satisfies this rule but the approved IaC does not.",
    )


def _decisive(status: EvaluationStatus) -> bool | None:
    if status in _COMPLIANT:
        return True
    if status in _NON_COMPLIANT:
        return False
    return None


def _evidence(iac: EvaluationResult | None, actual: EvaluationResult | None) -> tuple[str, ...]:
    """Cite both sides so a drift Finding stays traceable to its two sources."""
    references: list[str] = []
    for result in (iac, actual):
        if result is None:
            continue
        for reference in result.evidence_references:
            if reference not in references:
                references.append(reference)
    return tuple(references)
