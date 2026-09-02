"""Application boundary for one idempotent Assessment resource evaluation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

from apps.backend.assessment.drift import derive_drift_results
from apps.backend.assessment.model_profiles import ModelProfileRegistry
from apps.backend.assessment.reporting import AssessmentEvaluationPlan
from apps.backend.assessment.runner import AssessmentRunner
from apps.backend.policy import PolicyContext, PolicyContextResolver
from packages.contracts import (
    AssessmentPhase,
    EvaluationPerspective,
    EvaluationResult,
    ModelProfile,
    PlannedEvaluation,
    WorkflowCommand,
    WorkflowTask,
)

# Evaluate IaC before Actual so a derived DRIFT rationale always reads in that order.
_PERSPECTIVE_ORDER = (EvaluationPerspective.IAC, EvaluationPerspective.AWS_ACTUAL)


class AssessmentPlanError(ValueError):
    """Raised when the resolved Rule set cannot produce a coverable evaluation plan."""


class AssessmentWorkNotFoundError(LookupError):
    """Raised when a queue task cannot be matched to authoritative assessment state."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AssessmentResourceWork:
    """The minimal durable selectors required to evaluate one resource."""

    customer_id: str
    assessment_id: str
    job_id: str
    revision: int
    policy_profile_id: str
    phase: AssessmentPhase
    resource_id: str
    resource_type: str
    perspective: EvaluationPerspective
    model_profile_id: str
    planned_coordinates: tuple[PlannedEvaluation, ...] | None = None
    """Server-fixed `Resource × Rule × Perspective` set for the whole Assessment.

    A single-resource Assessment can leave this unset and let the Worker derive the
    plan from the resolved Rule set. Multi-resource or multi-perspective Assessments
    must supply it so the immutable plan does not depend on which task happens to
    run first. It is the set, not a count: two Assessments with equal counts but
    different coordinates are not comparable (ADR-0020 §5).
    """

    assessed_commit_sha: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "customer_id",
            "assessment_id",
            "job_id",
            "policy_profile_id",
            "resource_id",
            "resource_type",
            "model_profile_id",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise TypeError("revision must be an integer")
        if self.revision < 0:
            raise ValueError("revision must be non-negative")
        if not isinstance(self.phase, AssessmentPhase):
            raise TypeError("phase must be an AssessmentPhase")
        if not isinstance(self.perspective, EvaluationPerspective):
            raise TypeError("perspective must be an EvaluationPerspective")
        if self.planned_coordinates is not None:
            if not isinstance(self.planned_coordinates, tuple) or not self.planned_coordinates:
                raise ValueError("planned_coordinates must be a non-empty tuple or None")
            if not all(isinstance(value, PlannedEvaluation) for value in self.planned_coordinates):
                raise TypeError("planned_coordinates must contain PlannedEvaluation values")
            if len(set(self.planned_coordinates)) != len(self.planned_coordinates):
                raise ValueError("planned_coordinates must not contain duplicates")
        if self.assessed_commit_sha is not None and (
            not isinstance(self.assessed_commit_sha, str) or not self.assessed_commit_sha.strip()
        ):
            raise ValueError("assessed_commit_sha must be a non-empty string or None")


class AssessmentWorkRepository(Protocol):
    """Load durable selectors after receiving the minimal SQS payload."""

    def get_resource_work(
        self, *, job_id: str, expected_revision: int
    ) -> AssessmentResourceWork | None: ...


class EvaluationResultStore(Protocol):
    """Persist immutable, customer-scoped evaluation results idempotently."""

    def put_if_absent(
        self,
        *,
        customer_id: str,
        assessment_id: str,
        results: tuple[EvaluationResult, ...],
    ) -> None: ...


class AssessmentPlanStore(Protocol):
    def put_plan_if_absent(self, plan: AssessmentEvaluationPlan) -> None: ...


class AssessmentWorker:
    """Evaluate a single resource only after reloading its approved state.

    One task evaluates either a single declared perspective (`runner`) or the full
    Initial Assessment perspective set (`perspective_runners`, optionally with the
    derived `DRIFT` comparison). Both modes reload the approved state first and
    persist every result through the same immutable, idempotent store.
    """

    def __init__(
        self,
        *,
        work_repository: AssessmentWorkRepository,
        context_resolver: PolicyContextResolver,
        model_profiles: ModelProfileRegistry,
        result_store: EvaluationResultStore,
        runner: AssessmentRunner | None = None,
        perspective_runners: Mapping[EvaluationPerspective, AssessmentRunner] | None = None,
        derive_drift: bool = False,
        plan_store: AssessmentPlanStore | None = None,
    ) -> None:
        for name, value in (
            ("work_repository", work_repository),
            ("context_resolver", context_resolver),
            ("model_profiles", model_profiles),
            ("result_store", result_store),
        ):
            if value is None:
                raise TypeError(f"{name} is required")
        if (runner is None) == (perspective_runners is None):
            raise TypeError("supply exactly one of runner or perspective_runners")
        if perspective_runners is not None:
            if not isinstance(perspective_runners, Mapping) or not perspective_runners:
                raise TypeError("perspective_runners must be a non-empty mapping")
            for perspective, perspective_runner in perspective_runners.items():
                if not isinstance(perspective, EvaluationPerspective):
                    raise TypeError("perspective_runners keys must be EvaluationPerspective values")
                if perspective is EvaluationPerspective.DRIFT:
                    raise ValueError("DRIFT is derived from the evaluated perspectives")
                if not isinstance(perspective_runner, AssessmentRunner):
                    raise TypeError("perspective_runners values must be AssessmentRunner instances")
        elif derive_drift:
            raise ValueError("derive_drift requires perspective_runners")
        self._work_repository = work_repository
        self._context_resolver = context_resolver
        self._runner = runner
        self._perspective_runners = (
            None
            if perspective_runners is None
            else tuple(
                (perspective, perspective_runners[perspective])
                for perspective in _PERSPECTIVE_ORDER
                if perspective in perspective_runners
            )
        )
        self._derive_drift = derive_drift
        self._model_profiles = model_profiles
        self._result_store = result_store
        self._plan_store = plan_store

    def handle(self, task: WorkflowTask) -> tuple[EvaluationResult, ...]:
        """Process one `ASSESS_RESOURCE` task; no queue payload is trusted as state."""
        if not isinstance(task, WorkflowTask):
            raise TypeError("task must be a WorkflowTask")
        if task.command is not WorkflowCommand.ASSESS_RESOURCE:
            raise ValueError("assessment worker only accepts ASSESS_RESOURCE tasks")
        work = self._work_repository.get_resource_work(
            job_id=task.job_id, expected_revision=task.expected_revision
        )
        if work is None:
            raise AssessmentWorkNotFoundError("assessment work is missing or stale")
        if work.job_id != task.job_id or work.revision != task.expected_revision:
            raise AssessmentWorkNotFoundError("assessment work does not match task revision")
        context = self._context_resolver.resolve(
            policy_profile_id=work.policy_profile_id,
            phase=work.phase,
            resource_type=work.resource_type,
        )
        if self._plan_store is not None:
            self._plan_store.put_plan_if_absent(
                AssessmentEvaluationPlan(
                    customer_id=work.customer_id,
                    assessment_id=work.assessment_id,
                    planned_coordinates=work.planned_coordinates
                    or self._derived_coordinates(work, context),
                )
            )
        profile = self._model_profiles.get_assessment_profile(work.model_profile_id)
        results = self._evaluate(work, context, profile)
        if work.assessed_commit_sha is not None:
            evaluated_at = datetime.now(UTC).isoformat()
            results = tuple(
                replace(
                    result,
                    assessed_commit_sha=work.assessed_commit_sha,
                    evaluated_at=evaluated_at,
                )
                for result in results
            )
        self._result_store.put_if_absent(
            customer_id=work.customer_id,
            assessment_id=work.assessment_id,
            results=results,
        )
        return results

    def _derived_coordinates(
        self, work: AssessmentResourceWork, context: PolicyContext
    ) -> tuple[PlannedEvaluation, ...]:
        """Derive the plan for a single-resource Assessment from its resolved Rules.

        Only this task's resource is known here, so a multi-resource Assessment must
        inject `planned_coordinates` instead; deriving would fix the plan to whichever
        task ran first.
        """
        rule_ids = tuple(rule.rule_id for rule in context.rules)
        if len(set(rule_ids)) != len(rule_ids):
            # Two Rule versions under one rule_id share a result SK, so only one of
            # them could ever be stored; a plan naming both can never be covered.
            raise AssessmentPlanError(
                "policy context resolves one rule_id to more than one rule version"
            )
        perspectives = self._planned_perspectives(work)
        return tuple(
            PlannedEvaluation(
                resource_id=work.resource_id, rule_id=rule_id, perspective=perspective
            )
            for rule_id in rule_ids
            for perspective in perspectives
        )

    def _planned_perspectives(
        self, work: AssessmentResourceWork
    ) -> tuple[EvaluationPerspective, ...]:
        if self._perspective_runners is None:
            return (work.perspective,)
        evaluated = tuple(perspective for perspective, _ in self._perspective_runners)
        if self._derive_drift:
            return (*evaluated, EvaluationPerspective.DRIFT)
        return evaluated

    def _evaluate(
        self,
        work: AssessmentResourceWork,
        context: PolicyContext,
        profile: ModelProfile,
    ) -> tuple[EvaluationResult, ...]:
        if self._perspective_runners is None:
            assert self._runner is not None
            return self._checked(
                self._runner.evaluate_resource(
                    resource_id=work.resource_id, context=context, model_profile=profile
                ),
                perspective=work.perspective,
                profile=profile,
            )
        evaluated: dict[EvaluationPerspective, tuple[EvaluationResult, ...]] = {}
        for perspective, runner in self._perspective_runners:
            evaluated[perspective] = self._checked(
                runner.evaluate_resource(
                    resource_id=work.resource_id, context=context, model_profile=profile
                ),
                perspective=perspective,
                profile=profile,
            )
        results = tuple(
            result
            for perspective, _ in self._perspective_runners
            for result in evaluated[perspective]
        )
        if not self._derive_drift:
            return results
        drift = derive_drift_results(
            iac_results=evaluated.get(EvaluationPerspective.IAC, ()),
            actual_results=evaluated.get(EvaluationPerspective.AWS_ACTUAL, ()),
        )
        return results + self._checked(
            drift, perspective=EvaluationPerspective.DRIFT, profile=profile, context=context
        )

    @staticmethod
    def _checked(
        results: tuple[EvaluationResult, ...],
        *,
        perspective: EvaluationPerspective,
        profile: ModelProfile,
        context: PolicyContext | None = None,
    ) -> tuple[EvaluationResult, ...]:
        for result in results:
            if result.perspective is not perspective:
                raise ValueError("evaluator result perspective is outside assessment work")
            if result.model_profile_id != profile.model_profile_id:
                raise ValueError("evaluator result model profile is outside assessment work")
            if context is not None:
                for reference in result.evidence_references:
                    if not context.allows_evidence(reference):
                        raise ValueError("derived result cites evidence outside the policy context")
        return results
