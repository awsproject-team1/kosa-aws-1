"""M4 demo policy coverage boundary.

The demo repository is external (ADR-0021). This module validates only identifiers,
version pins, policy locators, and Golden Dataset coordinates; it never reads policy
originals or Terraform bodies.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from apps.backend.policy.registry import PolicyRegistry
from packages.contracts import (
    AssessmentPhase,
    EvaluationPerspective,
    EvaluationStatus,
    GoldenDatasetCase,
    ScoringMode,
)

DEMO_POLICY_COVERAGE_SCHEMA_VERSION = "m4-demo-policy-coverage-v1"
REQUIRED_PHASES = (
    AssessmentPhase.INITIAL,
    AssessmentPhase.POST_DEPLOY_VERIFICATION,
)
REQUIRED_PERSPECTIVES = tuple(EvaluationPerspective)


class DemoPolicyCoverageError(ValueError):
    """Raised when demo coverage cannot be proven from committed identifiers."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DemoGoldenCaseBinding:
    case_id: str
    phase: AssessmentPhase
    perspective: EvaluationPerspective


@dataclass(frozen=True, slots=True, kw_only=True)
class DemoRuleCoverage:
    rule_id: str
    rule_version: str
    demo_toggle: str
    control_ids: tuple[str, ...]
    policy_evidence_references: tuple[str, ...]
    golden_cases: tuple[DemoGoldenCaseBinding, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class DemoPolicyCoverageManifest:
    schema_version: str
    scenario_id: str
    policy_profile_id: str
    policy_profile_version: str
    resource_type: str
    rules: tuple[DemoRuleCoverage, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class DemoPolicyCoverageReport:
    scenario_id: str
    profile_rule_count: int
    control_count: int
    policy_evidence_count: int
    golden_case_count: int


def load_demo_policy_coverage(path: Path) -> DemoPolicyCoverageManifest:
    """Load a strict, identifier-only M4 demo manifest."""
    if not isinstance(path, Path):
        raise TypeError("path must be a Path")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fields = _mapping(data, "demo policy coverage")
        _exact_keys(
            fields,
            {
                "schema_version",
                "scenario_id",
                "policy_profile_id",
                "policy_profile_version",
                "resource_type",
                "rules",
            },
            "demo policy coverage",
        )
        rules = tuple(_rule(entry) for entry in _list(fields["rules"], "rules"))
        manifest = DemoPolicyCoverageManifest(
            schema_version=_text(fields["schema_version"], "schema_version"),
            scenario_id=_text(fields["scenario_id"], "scenario_id"),
            policy_profile_id=_text(fields["policy_profile_id"], "policy_profile_id"),
            policy_profile_version=_text(
                fields["policy_profile_version"], "policy_profile_version"
            ),
            resource_type=_text(fields["resource_type"], "resource_type"),
            rules=rules,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, DemoPolicyCoverageError):
            raise
        raise DemoPolicyCoverageError(f"demo policy coverage is invalid: {error}") from error

    if manifest.schema_version != DEMO_POLICY_COVERAGE_SCHEMA_VERSION:
        raise DemoPolicyCoverageError("unsupported demo policy coverage schema_version")
    if not manifest.rules:
        raise DemoPolicyCoverageError("rules must not be empty")
    _unique(
        ((rule.rule_id, rule.rule_version) for rule in manifest.rules),
        "rule binding",
    )
    _unique((rule.demo_toggle for rule in manifest.rules), "demo_toggle")
    return manifest


def validate_demo_policy_coverage(
    manifest: DemoPolicyCoverageManifest,
    *,
    registry: PolicyRegistry,
    initial_cases_path: Path,
    verification_cases_path: Path,
) -> DemoPolicyCoverageReport:
    """Cross-check the demo explanation against policy and Golden sources of truth."""
    if not isinstance(manifest, DemoPolicyCoverageManifest):
        raise TypeError("manifest must be a DemoPolicyCoverageManifest")
    if not isinstance(registry, PolicyRegistry):
        raise TypeError("registry must be a PolicyRegistry")

    profile = registry.catalog.get_profile(manifest.policy_profile_id)
    if profile is None or profile.version != manifest.policy_profile_version:
        raise DemoPolicyCoverageError("demo policy profile id/version is not in the registry")

    profile_rules = {
        (reference.rule_id, reference.version) for reference in profile.rule_references
    }
    manifest_rules = {(rule.rule_id, rule.rule_version) for rule in manifest.rules}
    if manifest_rules != profile_rules:
        raise DemoPolicyCoverageError("demo rules must exactly match the policy profile allow-list")

    raw_cases = (
        *_load_golden_cases(initial_cases_path, expected_phase=AssessmentPhase.INITIAL),
        *_load_golden_cases(
            verification_cases_path,
            expected_phase=AssessmentPhase.POST_DEPLOY_VERIFICATION,
        ),
    )
    indexed_cases: dict[tuple[str, AssessmentPhase, EvaluationPerspective], str] = {}
    case_ids: set[str] = set()
    for rule_id, case in raw_cases:
        coordinate = (rule_id, case.phase, case.perspective)
        if coordinate in indexed_cases:
            raise DemoPolicyCoverageError(f"duplicate Golden coordinate: {coordinate}")
        if case.case_id in case_ids:
            raise DemoPolicyCoverageError(f"duplicate Golden case_id: {case.case_id}")
        indexed_cases[coordinate] = case.case_id
        case_ids.add(case.case_id)

    expected_coordinates = {
        (rule_id, phase, perspective)
        for rule_id, _ in profile_rules
        for phase in REQUIRED_PHASES
        for perspective in REQUIRED_PERSPECTIVES
    }
    if set(indexed_cases) != expected_coordinates:
        raise DemoPolicyCoverageError(
            "Golden fixtures must contain exactly every profile Rule × phase × perspective"
        )

    manifest_case_ids: set[str] = set()
    all_controls: set[str] = set()
    all_evidence: set[str] = set()
    for coverage in manifest.rules:
        rule = registry.catalog.get_rule(coverage.rule_id, coverage.rule_version)
        if rule is None:
            raise DemoPolicyCoverageError("demo rule id/version is not in the registry")
        if manifest.resource_type not in rule.resource_types:
            raise DemoPolicyCoverageError(
                f"{coverage.rule_id} does not apply to {manifest.resource_type}"
            )
        if not set(REQUIRED_PHASES).issubset(rule.applicable_phases):
            raise DemoPolicyCoverageError(
                f"{coverage.rule_id} does not apply to both demo assessment phases"
            )

        controls = registry.controls.controls_for_rule(
            rule_id=coverage.rule_id,
            version=coverage.rule_version,
        )
        expected_control_ids = {control.control_id for control in controls}
        if set(coverage.control_ids) != expected_control_ids:
            raise DemoPolicyCoverageError(
                f"{coverage.rule_id} control_ids do not match the registry mapping"
            )

        expected_evidence = {
            reference.evidence_reference for reference in rule.source_references
        } | {control.source_reference.evidence_reference for control in controls}
        if set(coverage.policy_evidence_references) != expected_evidence:
            raise DemoPolicyCoverageError(
                f"{coverage.rule_id} policy evidence does not match Rule and Control locators"
            )

        expected_bindings = {
            DemoGoldenCaseBinding(
                case_id=indexed_cases[(coverage.rule_id, phase, perspective)],
                phase=phase,
                perspective=perspective,
            )
            for phase in REQUIRED_PHASES
            for perspective in REQUIRED_PERSPECTIVES
        }
        if set(coverage.golden_cases) != expected_bindings:
            raise DemoPolicyCoverageError(
                f"{coverage.rule_id} Golden bindings do not cover both phases and all perspectives"
            )
        for binding in coverage.golden_cases:
            if binding.case_id in manifest_case_ids:
                raise DemoPolicyCoverageError("a Golden case is bound to more than one demo Rule")
            manifest_case_ids.add(binding.case_id)
        all_controls.update(coverage.control_ids)
        all_evidence.update(coverage.policy_evidence_references)

    if manifest_case_ids != case_ids:
        raise DemoPolicyCoverageError("demo manifest and Golden fixture case IDs differ")

    return DemoPolicyCoverageReport(
        scenario_id=manifest.scenario_id,
        profile_rule_count=len(manifest.rules),
        control_count=len(all_controls),
        policy_evidence_count=len(all_evidence),
        golden_case_count=len(manifest_case_ids),
    )


def _load_golden_cases(
    path: Path, *, expected_phase: AssessmentPhase
) -> tuple[tuple[str, GoldenDatasetCase], ...]:
    try:
        entries = _list(json.loads(path.read_text(encoding="utf-8")), str(path))
        cases: list[tuple[str, GoldenDatasetCase]] = []
        for entry in entries:
            fields = _mapping(entry, "Golden case")
            rule_id = _text(fields["rule_id"], "rule_id")
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
            if case.phase is not expected_phase:
                raise DemoPolicyCoverageError(f"{case.case_id} is in the wrong fixture phase")
            cases.append((rule_id, case))
        return tuple(cases)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, DemoPolicyCoverageError):
            raise
        raise DemoPolicyCoverageError(f"Golden fixture is invalid: {path}: {error}") from error


def _rule(data: object) -> DemoRuleCoverage:
    fields = _mapping(data, "demo rule")
    _exact_keys(
        fields,
        {
            "rule_id",
            "rule_version",
            "demo_toggle",
            "control_ids",
            "policy_evidence_references",
            "golden_cases",
        },
        "demo rule",
    )
    controls = tuple(
        _text(value, "control_id") for value in _list(fields["control_ids"], "control_ids")
    )
    evidence = tuple(
        _text(value, "policy evidence reference")
        for value in _list(fields["policy_evidence_references"], "policy_evidence_references")
    )
    bindings = tuple(
        _case_binding(value) for value in _list(fields["golden_cases"], "golden_cases")
    )
    _unique(controls, "control_id")
    _unique(evidence, "policy evidence reference")
    _unique(((binding.phase, binding.perspective) for binding in bindings), "Golden coordinate")
    return DemoRuleCoverage(
        rule_id=_text(fields["rule_id"], "rule_id"),
        rule_version=_text(fields["rule_version"], "rule_version"),
        demo_toggle=_text(fields["demo_toggle"], "demo_toggle"),
        control_ids=controls,
        policy_evidence_references=evidence,
        golden_cases=bindings,
    )


def _case_binding(data: object) -> DemoGoldenCaseBinding:
    fields = _mapping(data, "Golden case binding")
    _exact_keys(fields, {"case_id", "phase", "perspective"}, "Golden case binding")
    return DemoGoldenCaseBinding(
        case_id=_text(fields["case_id"], "case_id"),
        phase=AssessmentPhase(fields["phase"]),
        perspective=EvaluationPerspective(fields["perspective"]),
    )


def _mapping(data: object, name: str) -> dict[str, object]:
    if not isinstance(data, dict):
        raise DemoPolicyCoverageError(f"{name} must be an object")
    return data


def _list(data: object, name: str) -> list[object]:
    if not isinstance(data, list):
        raise DemoPolicyCoverageError(f"{name} must be a list")
    return data


def _text(data: object, name: str) -> str:
    if not isinstance(data, str) or not data.strip():
        raise DemoPolicyCoverageError(f"{name} must be a non-empty string")
    return data


def _exact_keys(fields: dict[str, object], expected: set[str], name: str) -> None:
    if set(fields) != expected:
        raise DemoPolicyCoverageError(f"{name} fields must be exactly {sorted(expected)}")


def _unique(values: object, name: str) -> None:
    materialized = tuple(values)
    if len(set(materialized)) != len(materialized):
        raise DemoPolicyCoverageError(f"duplicate {name}")
