"""M4 customer-sandbox Golden Dataset release quality gate.

Actual evaluations run in the customer-deployed Assessment runtime. This module
consumes an identifier-only observation bundle and emits aggregate evidence without
copying prompts, responses, resource identifiers, policy text, or IaC bodies.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from apps.backend.assessment.drift import derive_drift_results
from apps.backend.policy.demo import DemoPolicyCoverageManifest
from packages.contracts import (
    AssessmentPhase,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    GoldenDatasetCase,
    ModelProfile,
    ScoringMode,
)
from packages.contracts.model_profiles import ModelProfileRole

OBSERVATION_SCHEMA_VERSION = "m4-golden-observations-v1"
REPORT_SCHEMA_VERSION = "m4-golden-release-report-v1"
RELEASE_PHASE = AssessmentPhase.POST_DEPLOY_VERIFICATION
REQUIRED_REPETITIONS = 5
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")


class GoldenReleaseQualityError(ValueError):
    """Raised when release evidence is malformed, incomplete, or unbound."""


class ObservationExecutionKind(StrEnum):
    BEDROCK = "BEDROCK"
    CODE_DERIVED = "CODE_DERIVED"


@dataclass(frozen=True, slots=True, kw_only=True)
class ReleaseGoldenCase:
    rule_id: str
    rule_version: str
    case: GoldenDatasetCase


@dataclass(frozen=True, slots=True, kw_only=True)
class GoldenReleaseObservation:
    case_id: str
    run_number: int
    rule_id: str
    rule_version: str
    phase: AssessmentPhase
    perspective: EvaluationPerspective
    status: EvaluationStatus
    severity: str
    score: float
    evidence_references: tuple[str, ...]
    model_profile_id: str
    rubric_version: str
    scoring_mode: ScoringMode
    resource_id_sha256: str
    input_artifact_sha256: str
    evaluation_output_sha256: str | None
    execution_kind: ObservationExecutionKind
    latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    error_code: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class GoldenObservationBundle:
    schema_version: str
    execution_id: str
    generated_at: str
    scenario_id: str
    runtime_mode: str
    platform_commit_sha: str
    repository_commit_sha256: str
    deployment_id_sha256: str
    artifact_set_sha256: str
    model_profile: ModelProfile
    observations: tuple[GoldenReleaseObservation, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class QualityMetrics:
    runs: int
    status_accuracy: float
    score_accuracy: float
    evidence_accuracy: float
    same_case_agreement: float
    score_spread: float
    execution_errors: int
    passes: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class CaseQualityReport:
    case_id: str
    rule_id: str
    perspective: str
    metrics: QualityMetrics


@dataclass(frozen=True, slots=True, kw_only=True)
class PerspectiveQualityReport:
    perspective: str
    case_count: int
    status_accuracy: float
    score_accuracy: float
    evidence_accuracy: float
    minimum_case_agreement: float
    maximum_case_score_spread: float
    execution_errors: int
    passes: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class GoldenReleaseQualityReport:
    schema_version: str
    execution_id: str
    generated_at: str
    scenario_id: str
    runtime_mode: str
    platform_commit_sha: str
    repository_commit_sha256: str
    deployment_id_sha256: str
    artifact_set_sha256: str
    observation_set_sha256: str
    model_profile: dict[str, str]
    repetitions: int
    case_count: int
    observation_count: int
    bedrock_call_count: int
    code_derived_count: int
    total_input_tokens: int
    total_output_tokens: int
    bedrock_p95_latency_ms: int
    case_reports: tuple[CaseQualityReport, ...]
    perspective_reports: tuple[PerspectiveQualityReport, ...]
    status_accuracy: float
    score_accuracy: float
    evidence_accuracy: float
    minimum_case_agreement: float
    maximum_case_score_spread: float
    execution_errors: int
    passes: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def load_release_golden_cases(
    path: Path, *, manifest: DemoPolicyCoverageManifest
) -> tuple[ReleaseGoldenCase, ...]:
    """Load the 18-case M4 live gate and bind every case to a manifest Rule version."""
    try:
        entries = _list(json.loads(path.read_text(encoding="utf-8")), "Golden cases")
        versions = {rule.rule_id: rule.rule_version for rule in manifest.rules}
        cases: list[ReleaseGoldenCase] = []
        for entry in entries:
            fields = _mapping(entry, "Golden case")
            rule_id = _text(fields["rule_id"], "rule_id")
            if rule_id not in versions:
                raise GoldenReleaseQualityError("Golden case Rule is outside the demo profile")
            case = GoldenDatasetCase(
                case_id=fields["case_id"],
                phase=AssessmentPhase(fields["phase"]),
                perspective=EvaluationPerspective(fields["perspective"]),
                rubric_version=fields["rubric_version"],
                scoring_mode=ScoringMode(fields["scoring_mode"]),
                resource_snapshot_artifact_id=fields["resource_snapshot_artifact_id"],
                expected_status=EvaluationStatus(fields["expected_status"]),
                expected_score_min=fields["expected_score_min"],
                expected_score_max=fields["expected_score_max"],
                expected_evidence_references=tuple(fields["expected_evidence_references"]),
            )
            if case.phase is not RELEASE_PHASE:
                raise GoldenReleaseQualityError("M4 live Golden case must be post-deploy")
            cases.append(
                ReleaseGoldenCase(rule_id=rule_id, rule_version=versions[rule_id], case=case)
            )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, GoldenReleaseQualityError):
            raise
        raise GoldenReleaseQualityError(f"release Golden cases are invalid: {error}") from error

    coordinates = {(case.rule_id, case.case.perspective) for case in cases}
    expected = {
        (rule.rule_id, perspective)
        for rule in manifest.rules
        for perspective in EvaluationPerspective
    }
    if coordinates != expected or len(cases) != len(expected):
        raise GoldenReleaseQualityError(
            "release Golden cases must be exactly six Rules × three perspectives"
        )
    _unique((case.case.case_id for case in cases), "Golden case_id")
    return tuple(cases)


def load_approved_model_profile(path: Path) -> ModelProfile:
    """Load the immutable Assessment Model Profile used by the release observations."""
    try:
        return _model_profile(json.loads(path.read_text(encoding="utf-8")))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, GoldenReleaseQualityError):
            raise
        raise GoldenReleaseQualityError(f"approved Model Profile is invalid: {error}") from error


def load_golden_observation_bundle(path: Path) -> GoldenObservationBundle:
    """Load strict local execution evidence. Raw model material has no schema field."""
    try:
        fields = _mapping(json.loads(path.read_text(encoding="utf-8")), "observation bundle")
        _exact_keys(
            fields,
            {
                "schema_version",
                "execution_id",
                "generated_at",
                "scenario_id",
                "runtime_mode",
                "platform_commit_sha",
                "repository_commit_sha256",
                "deployment_id_sha256",
                "artifact_set_sha256",
                "model_profile",
                "observations",
            },
            "observation bundle",
        )
        profile = _model_profile(fields["model_profile"])
        observations = tuple(
            _observation(value) for value in _list(fields["observations"], "observations")
        )
        bundle = GoldenObservationBundle(
            schema_version=_text(fields["schema_version"], "schema_version"),
            execution_id=_text(fields["execution_id"], "execution_id"),
            generated_at=_timestamp(fields["generated_at"], "generated_at"),
            scenario_id=_text(fields["scenario_id"], "scenario_id"),
            runtime_mode=_text(fields["runtime_mode"], "runtime_mode"),
            platform_commit_sha=_commit(fields["platform_commit_sha"], "platform_commit_sha"),
            repository_commit_sha256=_digest(
                fields["repository_commit_sha256"], "repository_commit_sha256"
            ),
            deployment_id_sha256=_digest(fields["deployment_id_sha256"], "deployment_id_sha256"),
            artifact_set_sha256=_digest(fields["artifact_set_sha256"], "artifact_set_sha256"),
            model_profile=profile,
            observations=observations,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, GoldenReleaseQualityError):
            raise
        raise GoldenReleaseQualityError(f"observation bundle is invalid: {error}") from error

    if bundle.schema_version != OBSERVATION_SCHEMA_VERSION:
        raise GoldenReleaseQualityError("unsupported observation schema_version")
    if bundle.runtime_mode != "CUSTOMER_SANDBOX":
        raise GoldenReleaseQualityError("release evidence must come from CUSTOMER_SANDBOX")
    if not bundle.observations:
        raise GoldenReleaseQualityError("observations must not be empty")
    return bundle


def evaluate_golden_release_quality(
    bundle: GoldenObservationBundle,
    *,
    manifest: DemoPolicyCoverageManifest,
    cases: tuple[ReleaseGoldenCase, ...],
    approved_model_profile: ModelProfile,
    repetitions: int = REQUIRED_REPETITIONS,
) -> GoldenReleaseQualityReport:
    """Validate complete evidence and apply the blocking ADR-0021 quality thresholds."""
    if not isinstance(bundle, GoldenObservationBundle):
        raise TypeError("bundle must be a GoldenObservationBundle")
    if not isinstance(approved_model_profile, ModelProfile):
        raise TypeError("approved_model_profile must be a ModelProfile")
    if approved_model_profile.role is not ModelProfileRole.ASSESSMENT:
        raise GoldenReleaseQualityError("approved Model Profile must have ASSESSMENT role")
    if isinstance(repetitions, bool) or not isinstance(repetitions, int) or repetitions < 2:
        raise ValueError("repetitions must be an integer of at least 2")
    if bundle.scenario_id != manifest.scenario_id:
        raise GoldenReleaseQualityError("observation scenario does not match the demo manifest")
    if bundle.model_profile != approved_model_profile:
        raise GoldenReleaseQualityError("observation Model Profile is not the approved profile")

    expected_by_case = {case.case.case_id: case for case in cases}
    expected_keys = {
        (case.case.case_id, run_number)
        for case in cases
        for run_number in range(1, repetitions + 1)
    }
    observed_by_key: dict[tuple[str, int], GoldenReleaseObservation] = {}
    for observation in bundle.observations:
        key = (observation.case_id, observation.run_number)
        if key in observed_by_key:
            raise GoldenReleaseQualityError("duplicate case_id and run_number observation")
        if key not in expected_keys:
            raise GoldenReleaseQualityError("observation is outside the release case/run plan")
        expected = expected_by_case[observation.case_id]
        _require_observation_binding(observation, expected, approved_model_profile)
        observed_by_key[key] = observation
    if set(observed_by_key) != expected_keys:
        raise GoldenReleaseQualityError("release observations are incomplete")

    _require_drift_derivation(tuple(observed_by_key.values()), cases, repetitions)

    case_reports: list[CaseQualityReport] = []
    for expected in cases:
        entries = tuple(
            observed_by_key[(expected.case.case_id, run_number)]
            for run_number in range(1, repetitions + 1)
        )
        metrics = _metrics(entries, expected.case)
        case_reports.append(
            CaseQualityReport(
                case_id=expected.case.case_id,
                rule_id=expected.rule_id,
                perspective=expected.case.perspective.value,
                metrics=metrics,
            )
        )

    perspective_reports = tuple(
        _perspective_report(perspective, tuple(case_reports))
        for perspective in EvaluationPerspective
    )
    all_observations = tuple(observed_by_key[key] for key in sorted(observed_by_key))
    status_accuracy = _ratio(
        observation.status is expected_by_case[observation.case_id].case.expected_status
        for observation in all_observations
    )
    score_accuracy = _ratio(
        expected_by_case[observation.case_id].case.expected_score_min
        <= observation.score
        <= expected_by_case[observation.case_id].case.expected_score_max
        for observation in all_observations
    )
    evidence_accuracy = _ratio(
        set(expected_by_case[observation.case_id].case.expected_evidence_references).issubset(
            observation.evidence_references
        )
        for observation in all_observations
    )
    execution_errors = sum(
        observation.error_code is not None or observation.status is EvaluationStatus.EXECUTION_ERROR
        for observation in all_observations
    )
    bedrock = tuple(
        observation
        for observation in all_observations
        if observation.execution_kind is ObservationExecutionKind.BEDROCK
    )
    derived = tuple(
        observation
        for observation in all_observations
        if observation.execution_kind is ObservationExecutionKind.CODE_DERIVED
    )
    latencies = sorted(
        observation.latency_ms for observation in bedrock if observation.latency_ms is not None
    )
    minimum_agreement = min(report.metrics.same_case_agreement for report in case_reports)
    maximum_spread = max(report.metrics.score_spread for report in case_reports)
    passes = (
        all(report.metrics.passes for report in case_reports)
        and all(report.passes for report in perspective_reports)
        and status_accuracy >= 0.9
        and score_accuracy >= 0.9
        and evidence_accuracy >= 0.9
        and minimum_agreement >= 0.9
        and maximum_spread <= 10
        and execution_errors == 0
    )
    return GoldenReleaseQualityReport(
        schema_version=REPORT_SCHEMA_VERSION,
        execution_id=bundle.execution_id,
        generated_at=bundle.generated_at,
        scenario_id=bundle.scenario_id,
        runtime_mode=bundle.runtime_mode,
        platform_commit_sha=bundle.platform_commit_sha,
        repository_commit_sha256=bundle.repository_commit_sha256,
        deployment_id_sha256=bundle.deployment_id_sha256,
        artifact_set_sha256=bundle.artifact_set_sha256,
        observation_set_sha256=_observation_digest(all_observations),
        model_profile={key: str(value) for key, value in approved_model_profile.to_dict().items()},
        repetitions=repetitions,
        case_count=len(cases),
        observation_count=len(all_observations),
        bedrock_call_count=len(bedrock),
        code_derived_count=len(derived),
        total_input_tokens=sum(observation.input_tokens or 0 for observation in bedrock),
        total_output_tokens=sum(observation.output_tokens or 0 for observation in bedrock),
        bedrock_p95_latency_ms=_nearest_rank_p95(latencies),
        case_reports=tuple(case_reports),
        perspective_reports=perspective_reports,
        status_accuracy=status_accuracy,
        score_accuracy=score_accuracy,
        evidence_accuracy=evidence_accuracy,
        minimum_case_agreement=minimum_agreement,
        maximum_case_score_spread=maximum_spread,
        execution_errors=execution_errors,
        passes=passes,
    )


def render_golden_release_markdown(report: GoldenReleaseQualityReport) -> str:
    """Render only aggregate, non-sensitive release evidence."""
    lines = [
        "# M4 Golden Dataset customer-sandbox 품질 Gate",
        "",
        f"- 결과: **{'PASS' if report.passes else 'FAIL'}**",
        f"- 실행 ID: `{report.execution_id}`",
        f"- 생성 시각: `{report.generated_at}`",
        f"- Platform revision: `{report.platform_commit_sha}`",
        f"- Observation digest: `{report.observation_set_sha256}`",
        f"- Model Profile: `{report.model_profile['model_profile_id']}`",
        f"- Case/반복/Observation: {report.case_count} / {report.repetitions} / {report.observation_count}",
        f"- Bedrock 호출/Code 파생: {report.bedrock_call_count} / {report.code_derived_count}",
        f"- Input/Output token: {report.total_input_tokens} / {report.total_output_tokens}",
        f"- Bedrock p95 latency: {report.bedrock_p95_latency_ms} ms",
        "",
        "| 관점 | Case | 상태 정확도 | Score 정확도 | Evidence 정확도 | 최소 일치율 | 최대 Score 편차 | 오류 | Gate |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for perspective in report.perspective_reports:
        lines.append(
            f"| {perspective.perspective} | {perspective.case_count} | "
            f"{perspective.status_accuracy:.0%} | {perspective.score_accuracy:.0%} | "
            f"{perspective.evidence_accuracy:.0%} | "
            f"{perspective.minimum_case_agreement:.0%} | "
            f"{perspective.maximum_case_score_spread:g} | "
            f"{perspective.execution_errors} | "
            f"{'PASS' if perspective.passes else 'FAIL'} |"
        )
    lines.extend(
        [
            "",
            "이 보고서는 Prompt, 원시 응답, resource identifier, 정책 원문, IaC 본문, credential을 포함하지 않는다.",
            "원본 observation bundle은 customer-approved 실행 환경에 보관하고 digest로만 결합한다.",
            "",
        ]
    )
    return "\n".join(lines)


def _require_observation_binding(
    observation: GoldenReleaseObservation,
    expected: ReleaseGoldenCase,
    profile: ModelProfile,
) -> None:
    case = expected.case
    if (
        observation.rule_id != expected.rule_id
        or observation.rule_version != expected.rule_version
        or observation.phase is not case.phase
        or observation.perspective is not case.perspective
        or observation.model_profile_id != profile.model_profile_id
        or observation.rubric_version != profile.rubric_version
        or observation.rubric_version != case.rubric_version
        or observation.scoring_mode is not case.scoring_mode
    ):
        raise GoldenReleaseQualityError(
            "observation metadata does not match the pinned case/profile"
        )
    expected_kind = (
        ObservationExecutionKind.CODE_DERIVED
        if case.perspective is EvaluationPerspective.DRIFT
        else ObservationExecutionKind.BEDROCK
    )
    if observation.execution_kind is not expected_kind:
        raise GoldenReleaseQualityError("observation execution kind violates the AI/Code boundary")
    if observation.evaluation_output_sha256 is None and (
        expected_kind is ObservationExecutionKind.CODE_DERIVED or observation.error_code is None
    ):
        raise GoldenReleaseQualityError("successful observation requires an output digest")
    if expected_kind is ObservationExecutionKind.CODE_DERIVED:
        if any(
            value is not None
            for value in (
                observation.latency_ms,
                observation.input_tokens,
                observation.output_tokens,
            )
        ):
            raise GoldenReleaseQualityError("Code-derived DRIFT cannot claim Bedrock usage")
    elif observation.error_code is None and any(
        value is None
        for value in (
            observation.latency_ms,
            observation.input_tokens,
            observation.output_tokens,
        )
    ):
        raise GoldenReleaseQualityError("successful Bedrock observation requires usage and latency")


def _require_drift_derivation(
    observations: tuple[GoldenReleaseObservation, ...],
    cases: tuple[ReleaseGoldenCase, ...],
    repetitions: int,
) -> None:
    case_by_coordinate = {(case.rule_id, case.case.perspective): case for case in cases}
    by_coordinate = {
        (observation.rule_id, observation.perspective, observation.run_number): observation
        for observation in observations
    }
    for rule_id in {case.rule_id for case in cases}:
        for run_number in range(1, repetitions + 1):
            iac = by_coordinate[(rule_id, EvaluationPerspective.IAC, run_number)]
            actual = by_coordinate[(rule_id, EvaluationPerspective.AWS_ACTUAL, run_number)]
            drift = by_coordinate[(rule_id, EvaluationPerspective.DRIFT, run_number)]
            derived = derive_drift_results(
                iac_results=(_evaluation(iac),),
                actual_results=(_evaluation(actual),),
            )[0]
            if (
                drift.case_id
                != case_by_coordinate[(rule_id, EvaluationPerspective.DRIFT)].case.case_id
                or drift.resource_id_sha256 != iac.resource_id_sha256
                or drift.resource_id_sha256 != actual.resource_id_sha256
                or drift.status is not derived.status
                or drift.score != derived.score
                or set(drift.evidence_references) != set(derived.evidence_references)
            ):
                raise GoldenReleaseQualityError(
                    "DRIFT observation is not derived from its IAC/Actual pair"
                )


def _evaluation(observation: GoldenReleaseObservation) -> EvaluationResult:
    return EvaluationResult(
        resource_id=observation.resource_id_sha256,
        rule_id=observation.rule_id,
        perspective=observation.perspective,
        status=observation.status,
        severity=observation.severity,
        score=observation.score,
        rationale="Sanitized M4 release observation.",
        evidence_references=observation.evidence_references,
        rule_version=observation.rule_version,
        rubric_version=observation.rubric_version,
        model_profile_id=observation.model_profile_id,
        scoring_mode=observation.scoring_mode,
    )


def _metrics(
    entries: tuple[GoldenReleaseObservation, ...], case: GoldenDatasetCase
) -> QualityMetrics:
    status_accuracy = _ratio(entry.status is case.expected_status for entry in entries)
    score_accuracy = _ratio(
        case.expected_score_min <= entry.score <= case.expected_score_max for entry in entries
    )
    expected_evidence = set(case.expected_evidence_references)
    evidence_accuracy = _ratio(
        expected_evidence.issubset(entry.evidence_references) for entry in entries
    )
    decisions = [
        (entry.status, tuple(sorted(entry.evidence_references)))
        for entry in entries
        if entry.error_code is None and entry.status is not EvaluationStatus.EXECUTION_ERROR
    ]
    agreement = Counter(decisions).most_common(1)[0][1] / len(decisions) if decisions else 0.0
    score_spread = max(entry.score for entry in entries) - min(entry.score for entry in entries)
    execution_errors = sum(
        entry.error_code is not None or entry.status is EvaluationStatus.EXECUTION_ERROR
        for entry in entries
    )
    passes = (
        status_accuracy >= 0.9
        and score_accuracy >= 0.9
        and evidence_accuracy >= 0.9
        and agreement >= 0.9
        and score_spread <= 10
        and execution_errors == 0
    )
    return QualityMetrics(
        runs=len(entries),
        status_accuracy=status_accuracy,
        score_accuracy=score_accuracy,
        evidence_accuracy=evidence_accuracy,
        same_case_agreement=agreement,
        score_spread=score_spread,
        execution_errors=execution_errors,
        passes=passes,
    )


def _perspective_report(
    perspective: EvaluationPerspective,
    case_reports: tuple[CaseQualityReport, ...],
) -> PerspectiveQualityReport:
    selected_cases = tuple(
        report for report in case_reports if report.perspective == perspective.value
    )
    status_accuracy = sum(report.metrics.status_accuracy for report in selected_cases) / len(
        selected_cases
    )
    score_accuracy = sum(report.metrics.score_accuracy for report in selected_cases) / len(
        selected_cases
    )
    evidence_accuracy = sum(report.metrics.evidence_accuracy for report in selected_cases) / len(
        selected_cases
    )
    minimum_agreement = min(report.metrics.same_case_agreement for report in selected_cases)
    maximum_spread = max(report.metrics.score_spread for report in selected_cases)
    execution_errors = sum(report.metrics.execution_errors for report in selected_cases)
    passes = (
        all(report.metrics.passes for report in selected_cases)
        and status_accuracy >= 0.9
        and score_accuracy >= 0.9
        and evidence_accuracy >= 0.9
        and minimum_agreement >= 0.9
        and maximum_spread <= 10
        and execution_errors == 0
    )
    return PerspectiveQualityReport(
        perspective=perspective.value,
        case_count=len(selected_cases),
        status_accuracy=status_accuracy,
        score_accuracy=score_accuracy,
        evidence_accuracy=evidence_accuracy,
        minimum_case_agreement=minimum_agreement,
        maximum_case_score_spread=maximum_spread,
        execution_errors=execution_errors,
        passes=passes,
    )


def _observation(data: object) -> GoldenReleaseObservation:
    fields = _mapping(data, "observation")
    _exact_keys(
        fields,
        {
            "case_id",
            "run_number",
            "rule_id",
            "rule_version",
            "phase",
            "perspective",
            "status",
            "severity",
            "score",
            "evidence_references",
            "model_profile_id",
            "rubric_version",
            "scoring_mode",
            "resource_id_sha256",
            "input_artifact_sha256",
            "evaluation_output_sha256",
            "execution_kind",
            "latency_ms",
            "input_tokens",
            "output_tokens",
            "error_code",
        },
        "observation",
    )
    score = fields["score"]
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 100:
        raise GoldenReleaseQualityError("score must be a number from 0 to 100")
    run_number = _non_negative_integer(fields["run_number"], "run_number")
    if run_number < 1:
        raise GoldenReleaseQualityError("run_number must be positive")
    return GoldenReleaseObservation(
        case_id=_text(fields["case_id"], "case_id"),
        run_number=run_number,
        rule_id=_text(fields["rule_id"], "rule_id"),
        rule_version=_text(fields["rule_version"], "rule_version"),
        phase=AssessmentPhase(fields["phase"]),
        perspective=EvaluationPerspective(fields["perspective"]),
        status=EvaluationStatus(fields["status"]),
        severity=_text(fields["severity"], "severity"),
        score=float(score),
        evidence_references=tuple(
            _text(value, "evidence reference")
            for value in _list(fields["evidence_references"], "evidence_references")
        ),
        model_profile_id=_text(fields["model_profile_id"], "model_profile_id"),
        rubric_version=_text(fields["rubric_version"], "rubric_version"),
        scoring_mode=ScoringMode(fields["scoring_mode"]),
        resource_id_sha256=_digest(fields["resource_id_sha256"], "resource_id_sha256"),
        input_artifact_sha256=_digest(fields["input_artifact_sha256"], "input_artifact_sha256"),
        evaluation_output_sha256=_optional_digest(
            fields["evaluation_output_sha256"], "evaluation_output_sha256"
        ),
        execution_kind=ObservationExecutionKind(fields["execution_kind"]),
        latency_ms=_optional_non_negative_integer(fields["latency_ms"], "latency_ms"),
        input_tokens=_optional_non_negative_integer(fields["input_tokens"], "input_tokens"),
        output_tokens=_optional_non_negative_integer(fields["output_tokens"], "output_tokens"),
        error_code=_optional_text(fields["error_code"], "error_code"),
    )


def _model_profile(data: object) -> ModelProfile:
    fields = _mapping(data, "model_profile")
    _exact_keys(
        fields,
        {
            "model_profile_id",
            "role",
            "region",
            "model_id",
            "prompt_version",
            "rubric_version",
            "golden_dataset_version",
        },
        "model_profile",
    )
    return ModelProfile(
        model_profile_id=fields["model_profile_id"],
        role=ModelProfileRole(fields["role"]),
        region=fields["region"],
        model_id=fields["model_id"],
        prompt_version=fields["prompt_version"],
        rubric_version=fields["rubric_version"],
        golden_dataset_version=fields["golden_dataset_version"],
    )


def _observation_digest(observations: tuple[GoldenReleaseObservation, ...]) -> str:
    canonical = json.dumps(
        [asdict(observation) for observation in observations],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _nearest_rank_p95(values: list[int]) -> int:
    if not values:
        raise GoldenReleaseQualityError("release evidence has no Bedrock latency values")
    return values[math.ceil(0.95 * len(values)) - 1]


def _ratio(values: object) -> float:
    materialized = tuple(values)
    return sum(materialized) / len(materialized)


def _mapping(data: object, name: str) -> dict[str, object]:
    if not isinstance(data, dict):
        raise GoldenReleaseQualityError(f"{name} must be an object")
    return data


def _list(data: object, name: str) -> list[object]:
    if not isinstance(data, list):
        raise GoldenReleaseQualityError(f"{name} must be a list")
    return data


def _text(data: object, name: str) -> str:
    if not isinstance(data, str) or not data.strip():
        raise GoldenReleaseQualityError(f"{name} must be a non-empty string")
    return data


def _optional_text(data: object, name: str) -> str | None:
    if data is None:
        return None
    return _text(data, name)


def _digest(data: object, name: str) -> str:
    value = _text(data, name)
    if _SHA256.fullmatch(value) is None:
        raise GoldenReleaseQualityError(f"{name} must be a lowercase SHA-256")
    return value


def _optional_digest(data: object, name: str) -> str | None:
    if data is None:
        return None
    return _digest(data, name)


def _commit(data: object, name: str) -> str:
    value = _text(data, name)
    if _COMMIT_SHA.fullmatch(value) is None:
        raise GoldenReleaseQualityError(f"{name} must be a lowercase 40-character commit SHA")
    return value


def _timestamp(data: object, name: str) -> str:
    value = _text(data, name)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GoldenReleaseQualityError(f"{name} must include a UTC offset")
    return value


def _non_negative_integer(data: object, name: str) -> int:
    if isinstance(data, bool) or not isinstance(data, int) or data < 0:
        raise GoldenReleaseQualityError(f"{name} must be a non-negative integer")
    return data


def _optional_non_negative_integer(data: object, name: str) -> int | None:
    if data is None:
        return None
    return _non_negative_integer(data, name)


def _exact_keys(fields: dict[str, object], expected: set[str], name: str) -> None:
    if set(fields) != expected:
        raise GoldenReleaseQualityError(f"{name} fields must be exactly {sorted(expected)}")


def _unique(values: object, name: str) -> None:
    materialized = tuple(values)
    if len(set(materialized)) != len(materialized):
        raise GoldenReleaseQualityError(f"duplicate {name}")
