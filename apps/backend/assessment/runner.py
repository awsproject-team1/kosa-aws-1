"""Validate evaluator output against the approved Policy Context before persistence."""

from typing import Protocol

from apps.backend.policy import PolicyContext
from packages.contracts import EvaluationResult, PolicyRule


class EvaluationContractError(ValueError):
    """Raised when an evaluator returns a result outside the approved rule context."""


class Evaluator(Protocol):
    def evaluate(
        self, *, resource_id: str, rule: PolicyRule, context: PolicyContext
    ) -> EvaluationResult: ...


class AssessmentRunner:
    """Run one approved Resource × Rule evaluation set using an injected model adapter."""

    def __init__(self, evaluator: Evaluator) -> None:
        if evaluator is None:
            raise TypeError("evaluator is required")
        self._evaluator = evaluator

    def evaluate_resource(
        self, *, resource_id: str, context: PolicyContext
    ) -> tuple[EvaluationResult, ...]:
        if not isinstance(resource_id, str) or not resource_id.strip():
            raise ValueError("resource_id must be a non-empty string")
        if not isinstance(context, PolicyContext):
            raise TypeError("context must be a PolicyContext")
        results = tuple(
            self._validated_result(
                self._evaluator.evaluate(resource_id=resource_id, rule=rule, context=context),
                resource_id=resource_id,
                rule_id=rule.rule_id,
                rule_version=rule.version,
            )
            for rule in context.rules
        )
        return results

    @staticmethod
    def _validated_result(
        result: EvaluationResult, *, resource_id: str, rule_id: str, rule_version: str
    ) -> EvaluationResult:
        if not isinstance(result, EvaluationResult):
            raise EvaluationContractError("evaluator must return an EvaluationResult")
        if result.resource_id != resource_id:
            raise EvaluationContractError("evaluator result resource_id is outside request context")
        if result.rule_id != rule_id or result.rule_version != rule_version:
            raise EvaluationContractError(
                "evaluator result rule is outside approved policy context"
            )
        return result
