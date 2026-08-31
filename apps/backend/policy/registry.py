"""Load the committed MVP Rule Registry into a read-only, deterministic Policy boundary.

Registry는 `fixtures/rules/`에 커밋된 Rule 정의, Control 매핑, Policy Profile, Policy Source
식별자로 구성된다. Resource 유형은 `rules.<type>.json` 파일을 추가하는 것만으로 확장한다.
정책 원문은 저장소에 없고 (ADR-0004), 여기서 다루는 것은 locator와 content hash뿐이다.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from apps.backend.policy.catalog import InMemoryPolicyCatalog
from apps.backend.policy.context import PolicyCatalog, PolicyContext, PolicyNotFoundError
from apps.backend.policy.serialization import (
    control_from_dict,
    profile_from_dict,
    rule_from_dict,
    source_from_dict,
)
from packages.contracts import PolicyControl, PolicyProfile, PolicyRule, PolicySource

SOURCES_FILE = "sources.json"
CONTROLS_FILE = "controls.json"
PROFILES_FILE = "profiles.json"
RULE_FILE_PATTERN = "rules.*.json"


class PolicyRegistryError(ValueError):
    """Raised when the committed registry files are missing or inconsistent."""


class ControlMapping:
    """Control ↔ Rule ↔ Resource 유형 매핑. Coverage 설명의 근거가 된다."""

    def __init__(self, controls: Iterable[PolicyControl]) -> None:
        self._controls: dict[str, PolicyControl] = {}
        self._by_rule: dict[tuple[str, str], list[str]] = {}
        for control in controls:
            if not isinstance(control, PolicyControl):
                raise TypeError("controls must contain PolicyControl values")
            if control.control_id in self._controls:
                raise PolicyRegistryError(f"duplicate control {control.control_id!r}")
            self._controls[control.control_id] = control
            for reference in control.rule_references:
                key = (reference.rule_id, reference.version)
                self._by_rule.setdefault(key, []).append(control.control_id)

    @property
    def control_ids(self) -> tuple[str, ...]:
        return tuple(self._controls)

    def get_control(self, control_id: str) -> PolicyControl | None:
        return self._controls.get(control_id)

    def controls_for_rule(self, *, rule_id: str, version: str) -> tuple[PolicyControl, ...]:
        """Return the controls one exact Rule version implements, in registry order."""
        control_ids = self._by_rule.get((rule_id, version), [])
        return tuple(self._controls[control_id] for control_id in control_ids)

    def resource_types_for_control(
        self, control_id: str, *, catalog: PolicyCatalog
    ) -> tuple[str, ...]:
        """Expand a control to the Resource 유형 its rules apply to, without duplicates."""
        control = self._controls.get(control_id)
        if control is None:
            raise PolicyNotFoundError(f"policy control {control_id!r} not found")
        resource_types: list[str] = []
        for reference in control.rule_references:
            rule = catalog.get_rule(reference.rule_id, reference.version)
            if rule is None:
                raise PolicyNotFoundError("policy control references an unavailable rule")
            for resource_type in rule.resource_types:
                if resource_type not in resource_types:
                    resource_types.append(resource_type)
        return tuple(resource_types)

    def control_rule_coverage(self, context: PolicyContext) -> tuple[ControlRuleCoverage, ...]:
        """Report evaluated/total Rule counts per cited control, in registry order."""
        evaluated = {(rule.rule_id, rule.version) for rule in context.rules}
        coverage: list[ControlRuleCoverage] = []
        for control in self.covered_controls(context):
            hits = sum(
                1
                for reference in control.rule_references
                if (reference.rule_id, reference.version) in evaluated
            )
            coverage.append(
                ControlRuleCoverage(
                    control_id=control.control_id,
                    evaluated_rules=hits,
                    total_rules=len(control.rule_references),
                )
            )
        return tuple(coverage)

    def covered_controls(self, context: PolicyContext) -> tuple[PolicyControl, ...]:
        """Return the controls a resolved Policy Context cites at least one Rule of.

        Control 하나가 여러 Rule로 구현될 수 있고 그중 일부만 이번 Context에 들어올 수 있으므로,
        이 결과는 "완전히 평가된 통제"가 아니라 "근거로 인용된 통제"다. 완전성은
        `control_rule_coverage()`의 (평가 Rule 수 / 전체 Rule 수)로 표현한다.
        """
        if not isinstance(context, PolicyContext):
            raise TypeError("context must be a PolicyContext")
        covered: list[PolicyControl] = []
        for rule in context.rules:
            for control in self.controls_for_rule(rule_id=rule.rule_id, version=rule.version):
                if control not in covered:
                    covered.append(control)
        return tuple(covered)


@dataclass(frozen=True, slots=True, kw_only=True)
class ControlRuleCoverage:
    """How much of one control this Policy Context actually evaluates."""

    control_id: str
    evaluated_rules: int
    total_rules: int

    @property
    def is_complete(self) -> bool:
        return self.evaluated_rules == self.total_rules


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyRegistry:
    """The committed policy boundary: sources, rules, controls, and profiles."""

    sources: tuple[PolicySource, ...]
    rules: tuple[PolicyRule, ...]
    profiles: tuple[PolicyProfile, ...]
    catalog: InMemoryPolicyCatalog
    controls: ControlMapping

    def get_source(self, source_id: str, version: str) -> PolicySource | None:
        """Return one exact Source version. Reference는 항상 version까지 고정한다."""
        for source in self.sources:
            if source.source_id == source_id and source.version == version:
                return source
        return None


def load_rule_registry(directory: Path) -> PolicyRegistry:
    """Load and cross-validate the registry committed under `fixtures/rules/`."""
    if not isinstance(directory, Path):
        raise TypeError("directory must be a Path")

    rule_files = sorted(directory.glob(RULE_FILE_PATTERN))
    if not rule_files:
        raise PolicyRegistryError(f"no {RULE_FILE_PATTERN} files under {directory}")

    with _registry_definition_errors():
        sources = tuple(source_from_dict(entry) for entry in _read_list(directory / SOURCES_FILE))
        profiles = tuple(
            profile_from_dict(entry) for entry in _read_list(directory / PROFILES_FILE)
        )
        controls = tuple(
            control_from_dict(entry) for entry in _read_list(directory / CONTROLS_FILE)
        )
        rules = tuple(rule_from_dict(entry) for path in rule_files for entry in _read_list(path))

    _require_unique_sources(sources)
    _require_known_sources(rules, controls, sources)
    with _registry_definition_errors():
        # InMemoryPolicyCatalog가 중복 Rule과 Profile→Rule 참조 무결성을 강제한다.
        catalog = InMemoryPolicyCatalog(profiles=profiles, rules=rules)
        mapping = ControlMapping(controls)
    _require_known_control_rules(controls, catalog)
    return PolicyRegistry(
        sources=sources, rules=rules, profiles=profiles, catalog=catalog, controls=mapping
    )


@contextmanager
def _registry_definition_errors() -> Iterator[None]:
    """Report a malformed definition as a registry failure, not a raw KeyError."""
    try:
        yield
    except PolicyRegistryError:
        raise
    except (KeyError, TypeError, ValueError, LookupError) as error:
        raise PolicyRegistryError(f"registry definition is invalid: {error}") from error


def _read_list(path: Path) -> list[object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise PolicyRegistryError(f"registry file not found: {path}") from error
    except json.JSONDecodeError as error:
        raise PolicyRegistryError(f"registry file is not valid JSON: {path}") from error
    if not isinstance(data, list):
        raise PolicyRegistryError(f"registry file must contain a list: {path}")
    return data


def _require_unique_sources(sources: Iterable[PolicySource]) -> None:
    """Rule과 Control이 (source_id, version)으로 고정하므로 그 조합은 유일해야 한다."""
    seen: set[tuple[str, str]] = set()
    for source in sources:
        key = (source.source_id, source.version)
        if key in seen:
            raise PolicyRegistryError(
                f"duplicate policy source {source.source_id}@{source.version}"
            )
        seen.add(key)


def _require_known_sources(
    rules: Iterable[PolicyRule],
    controls: Iterable[PolicyControl],
    sources: Iterable[PolicySource],
) -> None:
    """Every locator must point at a declared Policy Source version.

    `source_version`까지 대조해야 원문 개정 뒤에도 Rule이 가리키던 판본이 남는다.
    """
    known = {(source.source_id, source.version) for source in sources}
    referenced = {
        (reference.source_id, reference.source_version)
        for rule in rules
        for reference in rule.source_references
    } | {
        (control.source_reference.source_id, control.source_reference.source_version)
        for control in controls
    }
    unknown = sorted(referenced - known)
    if unknown:
        raise PolicyRegistryError(
            "source references point at undeclared source versions: "
            + ", ".join(f"{source_id}@{version}" for source_id, version in unknown)
        )


def _require_known_control_rules(
    controls: Iterable[PolicyControl], catalog: InMemoryPolicyCatalog
) -> None:
    """Control은 Registry에 실제로 존재하는 Rule version만 참조한다."""
    for control in controls:
        for reference in control.rule_references:
            if catalog.get_rule(reference.rule_id, reference.version) is None:
                raise PolicyRegistryError(
                    f"control {control.control_id!r} references an unavailable rule "
                    f"{reference.rule_id!r} version {reference.version!r}"
                )
