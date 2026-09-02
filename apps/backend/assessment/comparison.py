"""Deterministic Post-Deploy Verification comparison (ADR-0020).

This boundary is deliberately pure: it consumes complete, immutable Assessment
snapshots and never calls a model, writes a result, or joins mutable exceptions.
The Deployment/API owner supplies the durable selectors and complete plan sets.
"""

from dataclasses import dataclass

from apps.backend.assessment.reporting import AssessmentReport
from packages.contracts import (
    AssessmentComparison,
    ComparisonIneligibilityReason,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    FindingResolution,
    FindingResolutionResult,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class PlannedEvaluation:
    """One immutable applicable coordinate, without a Rule-version comparison claim."""

    resource_id: str
    rule_id: str
    perspective: EvaluationPerspective

    def __post_init__(self) -> None:
        for name in ("resource_id", "rule_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.perspective, EvaluationPerspective):
            raise TypeError("perspective must be an EvaluationPerspective")


@dataclass(frozen=True, slots=True, kw_only=True)
class ComparisonAssessment:
    """The complete immutable input required to compare one Assessment."""

    assessment_id: str
    model_profile_id: str
    rubric_version: str
    planned_evaluations: tuple[PlannedEvaluation, ...]
    report: AssessmentReport

    def __post_init__(self) -> None:
        for name in ("assessment_id", "model_profile_id", "rubric_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.planned_evaluations, tuple) or not self.planned_evaluations:
            raise ValueError("planned_evaluations must be a non-empty tuple")
        if not all(isinstance(value, PlannedEvaluation) for value in self.planned_evaluations):
            raise TypeError("planned_evaluations must contain PlannedEvaluation values")
        if len(set(self.planned_evaluations)) != len(self.planned_evaluations):
            raise ValueError("planned_evaluations must not contain duplicates")
        if not isinstance(self.report, AssessmentReport):
            raise TypeError("report must be an AssessmentReport")
        if self.report.assessment_id != self.assessment_id:
            raise ValueError("report assessment_id does not match comparison assessment")
        if self.report.next_cursor is not None or self.report.findings_next_cursor is not None:
            raise ValueError("report must contain complete results and findings")
        planned_coordinates = set(self.planned_evaluations)
        result_coordinates = {
            PlannedEvaluation(
                resource_id=result.resource_id,
                rule_id=result.rule_id,
                perspective=result.perspective,
            )
            for result in self.report.results
        }
        if result_coordinates != planned_coordinates:
            raise ValueError("report results must exactly match planned_evaluations")
        completed_coordinates = {
            PlannedEvaluation(
                resource_id=result.resource_id,
                rule_id=result.rule_id,
                perspective=result.perspective,
            )
            for result in self.report.results
            if result.status is not EvaluationStatus.EXECUTION_ERROR
        }
        if self.report.coverage.planned_evaluations != len(planned_coordinates) or (
            self.report.coverage.completed_evaluations != len(completed_coordinates)
        ):
            raise ValueError("report coverage does not match planned_evaluations")


def compare_post_deploy_assessments(
    *,
    deployment_id: str,
    source: ComparisonAssessment,
    verification: ComparisonAssessment,
) -> AssessmentComparison:
    """Return the read-only before/after projection specified by ADR-0020."""
    if not isinstance(deployment_id, str) or not deployment_id.strip():
        raise ValueError("deployment_id must be a non-empty string")
    if not isinstance(source, ComparisonAssessment) or not isinstance(
        verification, ComparisonAssessment
    ):
        raise TypeError("source and verification must be ComparisonAssessment values")

    reasons = _comparison_reasons(source, verification)
    source_score = source.report.readiness_score
    verification_score = verification.report.readiness_score
    return AssessmentComparison(
        source_assessment_id=source.assessment_id,
        verification_assessment_id=verification.assessment_id,
        deployment_id=deployment_id,
        comparable=not reasons,
        ineligibility_reasons=tuple(reasons),
        source_coverage=source.report.coverage,
        verification_coverage=verification.report.coverage,
        source_readiness_score=source_score,
        verification_readiness_score=verification_score,
        readiness_score_delta=(
            round(verification_score.score - source_score.score, 2) if not reasons else None
        ),
        finding_resolutions=_finding_resolutions(
            source.report.results, verification.report.results
        ),
    )


def _comparison_reasons(
    source: ComparisonAssessment, verification: ComparisonAssessment
) -> list[ComparisonIneligibilityReason]:
    reasons: list[ComparisonIneligibilityReason] = []
    if source.report.readiness_score is None:
        reasons.append(ComparisonIneligibilityReason.SOURCE_READINESS_UNAVAILABLE)
    if verification.report.readiness_score is None:
        reasons.append(ComparisonIneligibilityReason.VERIFICATION_READINESS_UNAVAILABLE)
    if set(source.planned_evaluations) != set(verification.planned_evaluations):
        reasons.append(ComparisonIneligibilityReason.PLANNED_EVALUATIONS_MISMATCH)
    if source.model_profile_id != verification.model_profile_id:
        reasons.append(ComparisonIneligibilityReason.MODEL_PROFILE_MISMATCH)
    if source.rubric_version != verification.rubric_version:
        reasons.append(ComparisonIneligibilityReason.RUBRIC_VERSION_MISMATCH)
    return reasons


def _finding_resolutions(
    source_results: tuple[EvaluationResult, ...], verification_results: tuple[EvaluationResult, ...]
) -> tuple[FindingResolutionResult, ...]:
    source_by_coordinate = _results_by_coordinate(source_results)
    verification_by_coordinate = _results_by_coordinate(verification_results)
    coordinates = sorted(
        set(source_by_coordinate) | set(verification_by_coordinate),
        key=lambda value: tuple(map(str, value)),
    )
    values: list[FindingResolutionResult] = []
    for coordinate in coordinates:
        before, after = (
            source_by_coordinate.get(coordinate),
            verification_by_coordinate.get(coordinate),
        )
        resolution = _resolution(before, after)
        if resolution is None:
            continue
        selected = after or before
        assert selected is not None
        values.append(
            FindingResolutionResult(
                resource_id=selected.resource_id,
                rule_id=selected.rule_id,
                rule_version=selected.rule_version,
                perspective=selected.perspective,
                resolution=resolution,
            )
        )
    return tuple(values)


def _results_by_coordinate(
    results: tuple[EvaluationResult, ...],
) -> dict[tuple[str, str, EvaluationPerspective], EvaluationResult]:
    values: dict[tuple[str, str, EvaluationPerspective], EvaluationResult] = {}
    for result in results:
        coordinate = (result.resource_id, result.rule_id, result.perspective)
        if coordinate in values:
            raise ValueError("assessment results contain duplicate comparison coordinates")
        values[coordinate] = result
    return values


def _resolution(
    before: EvaluationResult | None, after: EvaluationResult | None
) -> FindingResolution | None:
    if before is None or after is None or before.rule_version != after.rule_version:
        return FindingResolution.INDETERMINATE
    if after.status is EvaluationStatus.OUT_OF_SCOPE:
        return FindingResolution.NO_LONGER_APPLICABLE
    if after.status in {
        EvaluationStatus.MANUAL_REVIEW,
        EvaluationStatus.INSUFFICIENT_EVIDENCE,
        EvaluationStatus.EXECUTION_ERROR,
    }:
        return FindingResolution.INDETERMINATE
    if before.status is EvaluationStatus.FAIL and after.status is EvaluationStatus.PASS:
        return FindingResolution.RESOLVED
    if before.status is EvaluationStatus.FAIL and after.status is EvaluationStatus.FAIL:
        return FindingResolution.UNRESOLVED
    if before.status is EvaluationStatus.PASS and after.status is EvaluationStatus.FAIL:
        return FindingResolution.REGRESSED
    return None
