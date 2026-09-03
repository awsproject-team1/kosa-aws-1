"""One deterministic answer to "which perspectives does this Rule get evaluated in?".

이 질문에 답하는 곳이 여러 개면 그 답들이 어긋난다. 계획된 좌표는 한 곳이 정하고, 실제 평가는
다른 곳이 정하면, coverage는 영원히 완료되지 않거나 계획에 없는 결과가 저장된다. 그래서 plan
생성·perspective별 Rule 선택·runner 선택·Drift 대상 선택이 모두 이 helper 하나를 통과한다.

    evaluation_type   실행 Perspective
    ───────────────   ──────────────────────────────
    None (legacy)     IAC + AWS_ACTUAL + DRIFT
    IAC               IAC
    AWS               AWS_ACTUAL
    HYBRID            IAC + AWS_ACTUAL + DRIFT
    MANUAL            MANUAL

`None`은 authoring 이전에 커밋된 legacy fixture Rule이며 기존 동작을 그대로 보존한다.

**IAC-only와 AWS-only Rule은 Drift로 보내지 않는다.** 한쪽만 평가하는 것이 그 Rule의 정의이므로,
`derive_drift_results()`가 없는 쪽을 "누락된 Perspective"로 해석해 `MANUAL_REVIEW`를 만들면 실제
불일치와 구별되지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from packages.contracts import EvaluationPerspective, PolicyRule, RuleEvaluationType

#: IaC를 먼저 평가해 파생 DRIFT rationale이 항상 같은 순서로 읽히게 한다.
PERSPECTIVE_ORDER: tuple[EvaluationPerspective, ...] = (
    EvaluationPerspective.IAC,
    EvaluationPerspective.AWS_ACTUAL,
    EvaluationPerspective.MANUAL,
    EvaluationPerspective.DRIFT,
)

#: 두 Perspective를 모두 평가하는 실행 유형만 Drift 비교가 성립한다.
_DUAL_PERSPECTIVE_TYPES: frozenset[RuleEvaluationType | None] = frozenset(
    {None, RuleEvaluationType.HYBRID}
)

_EVALUATED_PERSPECTIVES: dict[RuleEvaluationType | None, tuple[EvaluationPerspective, ...]] = {
    None: (EvaluationPerspective.IAC, EvaluationPerspective.AWS_ACTUAL),
    RuleEvaluationType.IAC: (EvaluationPerspective.IAC,),
    RuleEvaluationType.AWS: (EvaluationPerspective.AWS_ACTUAL,),
    RuleEvaluationType.HYBRID: (EvaluationPerspective.IAC, EvaluationPerspective.AWS_ACTUAL),
    RuleEvaluationType.MANUAL: (EvaluationPerspective.MANUAL,),
}


def evaluated_perspectives(rule: PolicyRule) -> tuple[EvaluationPerspective, ...]:
    """The perspectives a Rule is evaluated in, before the worker's own capability limits."""
    if not isinstance(rule, PolicyRule):
        raise TypeError("rule must be a PolicyRule")
    return _EVALUATED_PERSPECTIVES[rule.evaluation_type]


def is_drift_eligible(rule: PolicyRule) -> bool:
    """Whether comparing this Rule's two perspectives is meaningful."""
    return rule.evaluation_type in _DUAL_PERSPECTIVE_TYPES


class EvaluationExecutionPlanner:
    """Bind the Rule-level execution table to one worker's actual capabilities.

    worker가 어떤 Perspective의 runner를 갖고 있는지는 배포 구성이 정한다. Rule이 AWS 평가를
    요구해도 그 worker에 Actual runner가 없으면 그 Perspective는 계획되지도 실행되지도 않는다 —
    계획만 하고 실행하지 못하면 coverage가 영원히 완료되지 않는다.
    """

    def __init__(
        self,
        *,
        available_perspectives: Iterable[EvaluationPerspective],
        derive_drift: bool = False,
    ) -> None:
        available = tuple(
            perspective
            for perspective in PERSPECTIVE_ORDER
            if perspective in set(available_perspectives)
        )
        if EvaluationPerspective.DRIFT in available:
            raise ValueError("DRIFT is derived from the evaluated perspectives")
        if derive_drift and not {
            EvaluationPerspective.IAC,
            EvaluationPerspective.AWS_ACTUAL,
        } <= set(available):
            raise ValueError("deriving drift requires both IAC and AWS_ACTUAL runners")
        self._available = available
        self._derive_drift = derive_drift

    @property
    def available_perspectives(self) -> tuple[EvaluationPerspective, ...]:
        return self._available

    def perspectives_for(self, rule: PolicyRule) -> tuple[EvaluationPerspective, ...]:
        """Every coordinate this Rule produces on this worker, DRIFT included."""
        evaluated = tuple(
            perspective
            for perspective in evaluated_perspectives(rule)
            if perspective in self._available
        )
        if not evaluated:
            return ()
        if self._derive_drift and is_drift_eligible(rule) and len(evaluated) == 2:
            return (*evaluated, EvaluationPerspective.DRIFT)
        return evaluated

    def rules_for(
        self, perspective: EvaluationPerspective, rules: Sequence[PolicyRule]
    ) -> tuple[PolicyRule, ...]:
        """The Rule subset one perspective evaluates, in the order the Profile gave them."""
        return tuple(rule for rule in rules if perspective in self.perspectives_for(rule))

    def drift_rules(self, rules: Sequence[PolicyRule]) -> tuple[PolicyRule, ...]:
        """The Rules whose two perspectives may be compared.

        IAC-only와 AWS-only Rule은 여기 들어오지 않는다. 한쪽만 평가하는 것이 그 Rule의 정의이고,
        Drift는 그것을 불일치로 읽을 수 없어야 한다.
        """
        if not self._derive_drift:
            return ()
        return tuple(
            rule for rule in rules if EvaluationPerspective.DRIFT in self.perspectives_for(rule)
        )

    def planned_perspectives(
        self, rules: Sequence[PolicyRule]
    ) -> tuple[EvaluationPerspective, ...]:
        """Every perspective this Rule set produces, in canonical order."""
        produced = {perspective for rule in rules for perspective in self.perspectives_for(rule)}
        return tuple(perspective for perspective in PERSPECTIVE_ORDER if perspective in produced)
