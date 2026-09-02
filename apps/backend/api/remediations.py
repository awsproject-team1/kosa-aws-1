"""M2 A public-service boundary for starting a remediation workflow."""

from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from apps.backend.auth import Action, Principal, authorize
from apps.backend.jobs import OutboxDispatcher, WorkflowOutboxEntry, create_job
from apps.backend.jobs.models import Job
from apps.backend.repositories import RepositoryError
from packages.contracts import (
    JobCurrentStep,
    RemediationAction,
    RemediationDecision,
    RemediationException,
    RemediationStartResponse,
    RemediationTarget,
    WorkflowCommand,
    WorkflowTask,
)
from packages.contracts.assessments import Finding
from packages.contracts.remediation import RemediationContext


class RemediationContextReader(Protocol):
    def get_context(self, *, customer_id: str, finding_id: str) -> RemediationContext: ...


class RemediationTargetReader(Protocol):
    def get_target(self, *, customer_id: str, finding_id: str) -> RemediationTarget: ...


class RemediationExceptionReader(Protocol):
    def list_exceptions(
        self, *, customer_id: str, finding: Finding
    ) -> tuple[RemediationException, ...]: ...


class RemediationDecisionMaker(Protocol):
    def decide(
        self,
        finding: Finding,
        *,
        customer_id: str,
        target: RemediationTarget,
        commit_sha: str,
        finding_evaluated_at: datetime,
        at: datetime,
        exceptions: Iterable[RemediationException] = (),
    ) -> RemediationDecision: ...


class RemediationWorkflowRepository(Protocol):
    def record_remediation_decision(
        self,
        *,
        context: RemediationContext,
        decision: RemediationDecision,
        remediation_id: str,
        requested_by: str,
        decided_at: datetime,
    ) -> None: ...

    def create_remediation_workflow(
        self,
        *,
        context: RemediationContext,
        decision: RemediationDecision,
        job: Job,
        remediation_id: str,
        outbox: WorkflowOutboxEntry,
        decided_at: datetime,
    ) -> None: ...


class RemediationApiService:
    """Apply B policy before A persists or dispatches any remediation work."""

    _ACTION_WORKFLOW = {
        RemediationAction.TERRAFORM_PATCH: (
            WorkflowCommand.GENERATE_REMEDIATION,
            JobCurrentStep.GENERATE_REMEDIATION,
        ),
        RemediationAction.ACTUAL_SYNC: (
            WorkflowCommand.SYNC_ACTUAL_STATE,
            JobCurrentStep.SYNC_ACTUAL_STATE,
        ),
    }

    def __init__(
        self,
        *,
        contexts: RemediationContextReader,
        targets: RemediationTargetReader,
        exceptions: RemediationExceptionReader,
        decision_maker: RemediationDecisionMaker,
        repository: RemediationWorkflowRepository,
        outbox_dispatcher: OutboxDispatcher,
        now: Callable[[], datetime],
        job_id_factory: Callable[[], str],
        remediation_id_factory: Callable[[], str],
    ) -> None:
        dependencies = (
            contexts,
            targets,
            exceptions,
            decision_maker,
            repository,
            outbox_dispatcher,
        )
        if any(dependency is None for dependency in dependencies):
            raise TypeError("all remediation API dependencies are required")
        if not callable(now):
            raise TypeError("now must be callable")
        self._contexts = contexts
        self._targets = targets
        self._exceptions = exceptions
        self._decision_maker = decision_maker
        self._repository = repository
        self._outbox_dispatcher = outbox_dispatcher
        self._now = now
        self._job_id_factory = job_id_factory
        self._remediation_id_factory = remediation_id_factory

    def create_remediation(self, principal: Principal, finding_id: str) -> RemediationStartResponse:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        if not isinstance(finding_id, str) or not finding_id.strip():
            raise ValueError("finding_id must be a non-empty string")
        authorize(principal, Action.START_REMEDIATION)
        context = self._contexts.get_context(
            customer_id=principal.customer_id, finding_id=finding_id
        )
        self._require_context_scope(
            context, customer_id=principal.customer_id, finding_id=finding_id
        )
        target = self._targets.get_target(customer_id=principal.customer_id, finding_id=finding_id)
        decided_at = self._now()
        if not isinstance(decided_at, datetime) or decided_at.tzinfo is None:
            raise ValueError("now must return an offset-aware datetime")
        finding_evaluated_at = self._require_finding_provenance(context, decided_at=decided_at)
        exceptions = self._exceptions.list_exceptions(
            customer_id=principal.customer_id, finding=context.finding
        )
        decision = self._decision_maker.decide(
            context.finding,
            customer_id=principal.customer_id,
            target=target,
            commit_sha=context.snapshot.commit_sha,
            finding_evaluated_at=finding_evaluated_at,
            at=decided_at,
            exceptions=exceptions,
        )
        self._require_decision_binding(context, decision)
        remediation_id = self._new(self._remediation_id_factory, "remediation")

        if not decision.is_actionable:
            self._repository.record_remediation_decision(
                context=context,
                decision=decision,
                remediation_id=remediation_id,
                requested_by=principal.subject,
                decided_at=decided_at,
            )
            return RemediationStartResponse(decision=decision)

        command, initial_step = self._ACTION_WORKFLOW[decision.action]
        job_id = self._new(self._job_id_factory, "job")
        job = create_job(
            job_id=job_id,
            customer_id=principal.customer_id,
            job_type="REMEDIATION",
            initial_step=initial_step,
            requested_by=principal.subject,
        )
        job = replace(job, remediation_id=remediation_id)
        outbox = WorkflowOutboxEntry(
            customer_id=principal.customer_id,
            job_id=job_id,
            task=WorkflowTask(job_id=job_id, expected_revision=0, command=command),
        )
        self._repository.create_remediation_workflow(
            context=context,
            decision=decision,
            job=job,
            remediation_id=remediation_id,
            outbox=outbox,
            decided_at=decided_at,
        )
        self._outbox_dispatcher.dispatch_entry(outbox)
        return RemediationStartResponse(decision=decision, job=job.to_response())

    @staticmethod
    def _require_context_scope(context: object, *, customer_id: str, finding_id: str) -> None:
        if not isinstance(context, RemediationContext):
            raise RepositoryError("stored remediation context is invalid")
        if context.finding.finding_id != finding_id or context.snapshot.customer_id != customer_id:
            raise RepositoryError("remediation context is outside the requested scope")

    @staticmethod
    def _require_decision_binding(context: RemediationContext, decision: object) -> None:
        if not isinstance(decision, RemediationDecision):
            raise RepositoryError("remediation policy returned an invalid decision")
        finding = context.finding
        if (
            decision.finding_id,
            decision.resource_id,
            decision.rule_id,
            decision.rule_version,
            decision.perspective,
        ) != (
            finding.finding_id,
            finding.resource_id,
            finding.rule_id,
            finding.rule_version,
            finding.perspective,
        ):
            raise RepositoryError("remediation decision is outside the context identity")

    def _require_finding_provenance(
        self, context: RemediationContext, *, decided_at: datetime
    ) -> datetime:
        finding = context.finding
        if (
            finding.assessed_commit_sha != context.snapshot.commit_sha
            or finding.evaluated_at is None
        ):
            raise RepositoryError(
                "remediation finding provenance is missing or outside the snapshot"
            )
        try:
            evaluated_at = datetime.fromisoformat(finding.evaluated_at.replace("Z", "+00:00"))
        except ValueError:
            raise RepositoryError("remediation finding evaluation time is invalid") from None
        if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
            raise RepositoryError("remediation finding evaluation time is invalid")
        if evaluated_at > decided_at:
            raise RepositoryError("remediation finding evaluation time is after decision time")
        return evaluated_at

    @staticmethod
    def _new(factory: Callable[[], str], name: str) -> str:
        value = factory()
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"generated {name}_id must be a non-empty string")
        return value
