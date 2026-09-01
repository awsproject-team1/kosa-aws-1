"""Application boundary for one idempotent Assessment resource evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from apps.backend.assessment.model_profiles import ModelProfileRegistry
from apps.backend.assessment.reporting import AssessmentEvaluationPlan
from apps.backend.assessment.runner import AssessmentRunner
from apps.backend.policy import PolicyContextResolver
from packages.contracts import (
    AssessmentPhase,
    EvaluationPerspective,
    EvaluationResult,
    WorkflowCommand,
    WorkflowTask,
)


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
    """Evaluate a single resource only after reloading its approved state."""

    def __init__(
        self,
        *,
        work_repository: AssessmentWorkRepository,
        context_resolver: PolicyContextResolver,
        runner: AssessmentRunner,
        model_profiles: ModelProfileRegistry,
        result_store: EvaluationResultStore,
        plan_store: AssessmentPlanStore | None = None,
    ) -> None:
        for name, value in (
            ("work_repository", work_repository),
            ("context_resolver", context_resolver),
            ("runner", runner),
            ("model_profiles", model_profiles),
            ("result_store", result_store),
        ):
            if value is None:
                raise TypeError(f"{name} is required")
        self._work_repository = work_repository
        self._context_resolver = context_resolver
        self._runner = runner
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
                    planned_evaluations=len(context.rules),
                )
            )
        profile = self._model_profiles.get_assessment_profile(work.model_profile_id)
        results = self._runner.evaluate_resource(
            resource_id=work.resource_id,
            context=context,
            model_profile=profile,
        )
        for result in results:
            if result.perspective is not work.perspective:
                raise ValueError("evaluator result perspective is outside assessment work")
            if result.model_profile_id != profile.model_profile_id:
                raise ValueError("evaluator result model profile is outside assessment work")
        self._result_store.put_if_absent(
            customer_id=work.customer_id,
            assessment_id=work.assessment_id,
            results=results,
        )
        return results
