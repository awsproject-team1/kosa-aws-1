"""Start the Post-Deploy Verification Assessment after a verified apply (ADR-0020 §1·§2·§3·§7).

apply run이 승인 사실과 대조되어 성공으로 확정되면, 원 Assessment의 scope(Repository, Policy
Profile **판본**, planned `(resource_id, rule_id, perspective)` 집합, Model Profile, rubric)를
그대로 pin한 **새** `assessment_id`의 Assessment를 만들고, Deployment Job의 write-once
`assessment_id`로 잇는다. 평가 자체는 Assessment Worker가 한다 — 이 경계는 어떤 리소스도 읽지
않고 모델도 부르지 않으며, Deployment Job을 다음 revision으로 올리고 `ASSESS_RESOURCE` task를
outbox에 넣는 것이 전부다.

이 경계가 없으면 D Worker는 Actual을 다시 읽고 끝난다. 재조회 결과는 어디에도 평가되지 않고,
`GET /deployments/{id}/verification`의 before/after 비교는 입력이 생길 수 없어 영원히 비어 있다.

멱등성: 같은 `APPLY_COMPLETED` task가 다시 전달되면 Deployment record가 이미
`verification_assessment_id`를 갖고 있으므로 그 값을 돌려주고 새 Assessment를 만들지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from apps.backend.assessment.models import Assessment
from apps.backend.assessment.verification import (
    VerificationScopeError,
    VerificationSource,
    plan_verification_assessment,
)
from apps.backend.deployment.record import DeploymentRecord
from apps.backend.deployment.worker import DeploymentWork
from apps.backend.jobs.lifecycle import transition_job
from apps.backend.jobs.models import Job
from apps.backend.jobs.outbox import OutboxDispatcher, WorkflowOutboxEntry
from apps.backend.policy import (
    NoApplicablePolicyRulesError,
    PolicyContext,
    PolicyContextResolver,
)
from apps.backend.policy.control_catalog import GOVERNANCE_ASSESSMENT_RESOURCE_TYPE
from packages.common.errors import DuplicateJobError
from packages.contracts import (
    AssessmentPhase,
    JobCurrentStep,
    JobStatus,
    PolicyRule,
    WorkflowCommand,
    WorkflowTask,
)


class PostDeployVerificationError(RuntimeError):
    """The verification Assessment could not be started from the stored facts."""


class DeploymentRecordLookup(Protocol):
    def get_deployment(
        self, *, customer_id: str, deployment_id: str
    ) -> DeploymentRecord | None: ...


class DeploymentJobLookup(Protocol):
    def get_job(self, customer_id: str, job_id: str) -> Job | None: ...


class VerificationSourceReader(Protocol):
    """Read the durable facts of the Assessment a deployment verifies."""

    def get_verification_source(
        self, *, customer_id: str, assessment_id: str
    ) -> VerificationSource: ...


class PolicyContextResolverFactory(Protocol):
    """Build a tenant-scoped resolver; another customer's Profile cannot be expressed."""

    def __call__(self, *, customer_id: str) -> PolicyContextResolver: ...


class VerificationAssessmentStore(Protocol):
    """Persist the verification Assessment, the resumed Job, and its task atomically."""

    def create_verification_assessment(
        self,
        *,
        assessment: Assessment,
        job: Job,
        expected_revision: int,
        outbox: WorkflowOutboxEntry,
    ) -> None: ...


class PostDeployVerificationService:
    """Turn one verified apply into a queued Post-Deploy Verification Assessment."""

    def __init__(
        self,
        *,
        deployments: DeploymentRecordLookup,
        jobs: DeploymentJobLookup,
        sources: VerificationSourceReader,
        context_resolvers: PolicyContextResolverFactory,
        resource_types_for: Callable[[str, str], tuple[str, ...]],
        store: VerificationAssessmentStore,
        outbox_dispatcher: OutboxDispatcher,
        assessment_id_factory: Callable[[], str],
    ) -> None:
        for value, name in (
            (deployments, "deployments"),
            (jobs, "jobs"),
            (sources, "sources"),
            (store, "store"),
        ):
            if value is None:
                raise TypeError(f"{name} is required")
        for value, name in (
            (context_resolvers, "context_resolvers"),
            (resource_types_for, "resource_types_for"),
            (assessment_id_factory, "assessment_id_factory"),
        ):
            if not callable(value):
                raise TypeError(f"{name} must be callable")
        if not isinstance(outbox_dispatcher, OutboxDispatcher):
            raise TypeError("outbox_dispatcher must be an OutboxDispatcher")
        self._deployments = deployments
        self._jobs = jobs
        self._sources = sources
        self._context_resolvers = context_resolvers
        self._resource_types_for = resource_types_for
        self._store = store
        self._outbox_dispatcher = outbox_dispatcher
        self._assessment_id_factory = assessment_id_factory

    def start_verification(self, *, work: DeploymentWork) -> str:
        """Create and queue the verification Assessment; return its `assessment_id`.

        `work`는 D Worker가 `(job_id, revision)`으로 다시 읽은 authoritative 값이다. 이 서비스는
        그 Job이 아직 같은 revision인지 다시 확인한 뒤에만 다음 revision으로 올린다 — 사이에 다른
        전이가 있었다면 그것은 재시도가 아니라 다른 실행이다.
        """
        if not isinstance(work, DeploymentWork):
            raise TypeError("work must be a DeploymentWork")
        record = self._deployments.get_deployment(
            customer_id=work.customer_id, deployment_id=work.deployment_id
        )
        if record is None:
            raise PostDeployVerificationError("deployment not found")
        if record.verification_assessment_id is not None:
            # 같은 apply 완료의 재전달이다. 검증 Assessment는 이미 있다.
            return record.verification_assessment_id
        if record.job_id != work.job_id or record.repository_id != work.repository_id:
            raise PostDeployVerificationError("deployment record does not match the work")
        job = self._jobs.get_job(work.customer_id, record.job_id)
        if job is None:
            raise PostDeployVerificationError("deployment job not found")
        if job.assessment_id is not None:
            # record와 Job은 한 transaction으로 쓰이므로 여기 도달하면 저장이 어긋난 것이다.
            # 그래도 Job이 가리키는 Assessment가 정본이니 그것을 돌려준다.
            return job.assessment_id
        if job.revision != work.revision:
            raise PostDeployVerificationError("deployment job revision moved since the task")
        if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
            raise PostDeployVerificationError("deployment job is already terminal")

        source = self._sources.get_verification_source(
            customer_id=work.customer_id, assessment_id=record.source_assessment_id
        )
        if source.customer_id != work.customer_id or source.repository_id != record.repository_id:
            raise PostDeployVerificationError("source assessment is outside the deployment scope")
        context = _verification_context(
            self._context_resolvers(customer_id=work.customer_id),
            source,
            self._resource_types_for(work.customer_id, record.repository_id),
        )
        try:
            scope = plan_verification_assessment(
                source=source,
                context=context,
                deployment_id=record.deployment_id,
                assessment_id=self._assessment_id_factory(),
                job_id=job.job_id,
            )
        except VerificationScopeError as error:
            # Profile 판본이 바뀐 경우 등은 검증이 아니라 새 Initial Assessment의 일이다
            # (ADR-0020 §2). 조용히 다른 allow-list로 재평가하지 않고 사람에게 남긴다.
            raise PostDeployVerificationError(
                f"verification cannot reuse the source scope: {error.code.value}"
            ) from error
        resumed = transition_job(
            job,
            expected_revision=job.revision,
            status=JobStatus.RUNNING,
            current_step=JobCurrentStep.POST_DEPLOY_VERIFICATION,
            assessment_id=scope.assessment.assessment_id,
        )
        outbox = WorkflowOutboxEntry(
            customer_id=work.customer_id,
            job_id=job.job_id,
            task=WorkflowTask(
                job_id=job.job_id,
                expected_revision=resumed.revision,
                command=WorkflowCommand.ASSESS_RESOURCE,
            ),
        )
        try:
            self._store.create_verification_assessment(
                assessment=scope.assessment,
                job=resumed,
                expected_revision=job.revision,
                outbox=outbox,
            )
        except DuplicateJobError:
            # 동시 재전달이 먼저 썼다. 그쪽의 Assessment가 정본이므로 다시 읽어 돌려준다.
            current = self._deployments.get_deployment(
                customer_id=work.customer_id, deployment_id=work.deployment_id
            )
            if current is None or current.verification_assessment_id is None:
                raise PostDeployVerificationError(
                    "verification assessment write conflicted without a stored winner"
                ) from None
            return current.verification_assessment_id
        # The outbox row is durable before this best-effort send; the scheduled sweeper
        # retries a failed publish exactly as it does for an Initial Assessment.
        self._outbox_dispatcher.dispatch_entry(outbox)
        return scope.assessment.assessment_id


def _verification_context(
    resolver: PolicyContextResolver,
    source: VerificationSource,
    resource_types: tuple[str, ...],
) -> PolicyContext:
    """Resolve the pinned Profile version for every type the verification re-evaluates.

    `plan_verification_assessment()`는 Context 하나를 받아 "planned Rule이 검증 phase에도
    적용되는가"를 대조한다. 원 계획은 여러 resource type(과 governance 좌표)에 걸치므로, 각 type의
    적용 가능 Rule을 합쳐 하나의 allow-list로 넘긴다. type이 하나도 Rule을 갖지 않으면 그 type은
    검증 대상이 아닐 뿐 오류가 아니다 — 모두 비어 있을 때만 실패한다.
    """
    if not isinstance(resource_types, tuple) or not all(
        isinstance(item, str) and item.strip() for item in resource_types
    ):
        raise PostDeployVerificationError("resource types for the deployment are invalid")
    rules: list[PolicyRule] = []
    seen: set[tuple[str, str]] = set()
    first: PolicyContext | None = None
    for resource_type in (*resource_types, GOVERNANCE_ASSESSMENT_RESOURCE_TYPE):
        try:
            context = resolver.resolve(
                policy_profile_id=source.policy_profile_id,
                phase=AssessmentPhase.POST_DEPLOY_VERIFICATION,
                resource_type=resource_type,
                expected_profile_version=source.policy_profile_version,
            )
        except NoApplicablePolicyRulesError:
            continue
        if first is None:
            first = context
        elif (
            context.policy_profile_id != first.policy_profile_id
            or context.policy_profile_version != first.policy_profile_version
        ):
            raise PostDeployVerificationError(
                "resolver returned different profile versions for one verification"
            )
        for rule in context.rules:
            key = (rule.rule_id, rule.version)
            if key not in seen:
                seen.add(key)
                rules.append(rule)
    if first is None or not rules:
        raise PostDeployVerificationError(
            "no approved rule applies to the post-deploy verification phase"
        )
    # 합친 allow-list를 하나의 Context로 표현한다. Profile id·판본은 **resolver가 실제로 돌려준
    # 값**이다 — source의 값을 그대로 적으면 판본이 바뀐 경우를 `plan_verification_assessment()`가
    # 잡을 수 없다. `resource_type`은 Context 계약상 필수이지만 여기서는 Rule 집합 대조에만
    # 쓰이므로 첫 번째로 resolve된 type을 적는다.
    return PolicyContext(
        policy_profile_id=first.policy_profile_id,
        policy_profile_version=first.policy_profile_version,
        phase=AssessmentPhase.POST_DEPLOY_VERIFICATION,
        resource_type=first.resource_type,
        rules=tuple(rules),
    )
