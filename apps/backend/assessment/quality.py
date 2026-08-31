"""Golden Dataset repeated-evaluation checks for the C evaluation boundary."""

from dataclasses import dataclass
from typing import Protocol

from packages.contracts import EvaluationResult, GoldenDatasetCase


class GoldenCaseEvaluator(Protocol):
    """Evaluate the immutable snapshot referenced by one Golden Dataset case."""

    def evaluate_case(self, case: GoldenDatasetCase) -> EvaluationResult: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class GoldenEvaluationReport:
    """Per-case quality result; aggregate thresholds are evaluated by the caller."""

    case_id: str
    runs: int
    status_accuracy: float
    score_accuracy: float
    evidence_accuracy: float
    same_case_agreement: float
    score_spread: float

    @property
    def passes_m0_gate(self) -> bool:
        return (
            self.status_accuracy >= 0.9
            and self.score_accuracy >= 0.9
            and self.evidence_accuracy >= 0.9
            and self.same_case_agreement >= 0.9
            and self.score_spread <= 10
        )


class GoldenDatasetRunner:
    """Run a case repeatedly without coupling evaluation quality to a model provider."""

    def __init__(self, evaluator: GoldenCaseEvaluator) -> None:
        if evaluator is None:
            raise TypeError("evaluator is required")
        self._evaluator = evaluator

    def evaluate(self, case: GoldenDatasetCase, *, repetitions: int = 3) -> GoldenEvaluationReport:
        if not isinstance(case, GoldenDatasetCase):
            raise TypeError("case must be a GoldenDatasetCase")
        if isinstance(repetitions, bool) or not isinstance(repetitions, int) or repetitions < 2:
            raise ValueError("repetitions must be an integer of at least 2")
        outcomes = tuple(self._evaluate_once(case) for _ in range(repetitions))
        expected_evidence = set(case.expected_evidence_references)
        status_accuracy = _ratio(result.status is case.expected_status for result in outcomes)
        score_accuracy = _ratio(
            case.expected_score_min <= result.score <= case.expected_score_max
            for result in outcomes
        )
        evidence_accuracy = _ratio(
            expected_evidence.issubset(result.evidence_references) for result in outcomes
        )
        first = outcomes[0]
        agreement = _ratio(
            result.status is first.status
            and set(result.evidence_references) == set(first.evidence_references)
            for result in outcomes
        )
        scores = tuple(result.score for result in outcomes)
        return GoldenEvaluationReport(
            case_id=case.case_id,
            runs=repetitions,
            status_accuracy=status_accuracy,
            score_accuracy=score_accuracy,
            evidence_accuracy=evidence_accuracy,
            same_case_agreement=agreement,
            score_spread=max(scores) - min(scores),
        )

    def _evaluate_once(self, case: GoldenDatasetCase) -> EvaluationResult:
        result = self._evaluator.evaluate_case(case)
        if not isinstance(result, EvaluationResult):
            raise TypeError("golden case evaluator must return an EvaluationResult")
        if result.perspective is not case.perspective:
            raise ValueError("golden result perspective does not match case")
        if result.rubric_version != case.rubric_version:
            raise ValueError("golden result rubric version does not match case")
        if result.scoring_mode is not case.scoring_mode:
            raise ValueError("golden result scoring mode does not match case")
        return result


def _ratio(matches: object) -> float:
    values = tuple(matches)
    return sum(values) / len(values)
