"""A-owned reader assembling the durable facts a deployment's status derives from.

ADR-0019 §8은 `DeploymentStatus`를 저장하지 않는다. 표현 상태는 이미 durable한 사실들
(Job status/step, 승인·거절 존재, apply run 결과, 검증 결과)에서 read 시 파생한다. 이 reader는
그 사실들을 모아 `DeploymentFacts`를 만든다 — 파생 자체는 `derive_deployment_status()`가 한다.

Deployment의 모든 하위 item(`#APPROVAL#`, `#REJECTION`, `#DISPATCH`, `#EVENT#`)은 같은
`DEPLOYMENT#{deployment_id}` SK prefix를 쓰므로 base table query 한 번에 다 들어온다
(`DATABASE.md`의 access pattern). Job만 별도 get이다.

검증 결과(`VerificationOutcome`)는 저장된 값이 아니라 비교 projection의 결과다. 그래서 이 reader는
`GET /deployments/{id}/verification`과 **같은** 비교 입력으로 같은 판정을 낸다. 상태 화면과 검증
화면이 서로 다른 근거로 계산되면 둘이 어긋날 수 있고, 그 불일치는 저장된 상태를 두는 것과 같은
문제를 다시 만든다.
"""

from collections.abc import Mapping
from typing import Protocol

from apps.backend.api.deployments import ComparisonInputReader
from apps.backend.assessment.comparison import compare_post_deploy_assessments
from apps.backend.deployment.record import DeploymentRecord
from apps.backend.repositories.dynamodb import DynamoTable
from apps.backend.repositories.ports import RepositoryError, StoredDataError
from packages.contracts import (
    ApplyOutcome,
    DeploymentFacts,
    DeploymentReadinessSignal,
    JobCurrentStep,
    JobStatus,
    VerificationOutcome,
    WorkflowConclusion,
)


class DeploymentRecordReader(Protocol):
    def get_deployment(
        self, *, customer_id: str, deployment_id: str
    ) -> DeploymentRecord | None: ...


class JobReader(Protocol):
    def get_job(self, customer_id: str, job_id: str) -> object | None: ...


class DynamoDbDeploymentFactsReader:
    """Assemble one deployment's durable facts from its item prefix and its Job."""

    def __init__(
        self,
        table: DynamoTable,
        *,
        deployments: DeploymentRecordReader,
        jobs: JobReader,
        comparisons: ComparisonInputReader | None = None,
        readiness: "DeploymentReadinessReader | None" = None,
    ) -> None:
        if table is None:
            raise TypeError("table is required")
        if deployments is None or jobs is None:
            raise TypeError("deployments and jobs readers are required")
        self._table = table
        self._deployments = deployments
        self._jobs = jobs
        self._comparisons = comparisons
        self._readiness = readiness

    def get_deployment_facts(self, *, customer_id: str, deployment_id: str) -> DeploymentFacts:
        for value, name in ((customer_id, "customer_id"), (deployment_id, "deployment_id")):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        record = self._deployments.get_deployment(
            customer_id=customer_id, deployment_id=deployment_id
        )
        if record is None:
            raise StoredDataError("deployment not found")
        job = self._jobs.get_job(customer_id, record.job_id)
        if job is None:
            # A deployment is written with its Job in one transaction, so a deployment
            # without one is corrupt storage rather than an ordinary missing record.
            raise StoredDataError("deployment job not found")
        items = self._deployment_items(customer_id, deployment_id)
        is_rejected = any(_suffix(item, deployment_id) == "#REJECTION" for item in items)
        is_approved = any(_suffix(item, deployment_id).startswith("#APPROVAL#") for item in items)
        return DeploymentFacts(
            job_status=_job_status(job),
            current_step=_current_step(job),
            readiness=self._readiness_signal(customer_id, record),
            # 거절과 승인이 동시에 true면 `DeploymentFacts`가 거부한다. 거절은 terminal이므로
            # 그 경우 거절만 남긴다 — 표현 상태도 REJECTED가 이긴다(ADR-0019 §8).
            is_approved=is_approved and not is_rejected,
            is_rejected=is_rejected,
            apply_outcome=_apply_outcome(items, deployment_id),
            verification_outcome=self._verification_outcome(customer_id, record),
        )

    def _readiness_signal(
        self, customer_id: str, record: DeploymentRecord
    ) -> DeploymentReadinessSignal | None:
        """Return C's readiness verdict, or `None` when there is nothing to evaluate.

        The verdict is a pure function of the remediation context and D's plan summary,
        so it is not stored (`DeploymentStatus`와 같은 원칙). Before plan facts exist
        there is no plan to judge and the status falls through to the Job's current step.

        `None` is also what a missing reader returns. That is deliberate: guessing
        `READY_FOR_APPROVAL` would present a plan C would have blocked as one waiting
        for a human to approve — exactly the state this gate exists to prevent.
        """
        if record.plan_hash is None or self._readiness is None:
            return None
        signal = self._readiness.get_readiness_signal(
            customer_id=customer_id, deployment_id=record.deployment_id
        )
        if signal is not None and not isinstance(signal, DeploymentReadinessSignal):
            raise StoredDataError("deployment readiness signal is invalid")
        return signal

    def _deployment_items(self, customer_id: str, deployment_id: str) -> tuple[Mapping, ...]:
        try:
            response = self._table.query(
                KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
                ExpressionAttributeValues={
                    ":pk": f"CUSTOMER#{customer_id}",
                    ":prefix": f"DEPLOYMENT#{deployment_id}",
                },
                ConsistentRead=True,
            )
        except Exception:
            raise RepositoryError("deployment facts read failed") from None
        items = response.get("Items", []) if isinstance(response, Mapping) else None
        if not isinstance(items, list):
            raise StoredDataError("deployment items are invalid")
        return tuple(item for item in items if isinstance(item, Mapping))

    def _verification_outcome(
        self, customer_id: str, record: DeploymentRecord
    ) -> VerificationOutcome:
        if record.verification_assessment_id is None:
            return VerificationOutcome.NOT_STARTED
        if self._comparisons is None:
            # 검증 Assessment는 생겼는데 비교 입력을 읽을 수 없다. 진행 중으로 표시한다 —
            # 판정할 근거가 없을 때 COMPARABLE/INDETERMINATE 중 하나를 고르면 그건 추측이다.
            return VerificationOutcome.RUNNING
        try:
            source, verification = self._comparisons.get_comparison_inputs(
                customer_id=customer_id,
                source_assessment_id=record.source_assessment_id,
                verification_assessment_id=record.verification_assessment_id,
            )
            comparison = compare_post_deploy_assessments(
                deployment_id=record.deployment_id, source=source, verification=verification
            )
        except (LookupError, StoredDataError, TypeError, ValueError):
            # 재평가가 아직 완결되지 않았으면 report가 불완전해 비교 입력이 만들어지지 않는다.
            # 그건 "아직 진행 중"이지 판정 불가가 아니다.
            return VerificationOutcome.RUNNING
        return (
            VerificationOutcome.COMPARABLE
            if comparison.comparable
            else VerificationOutcome.INDETERMINATE
        )


def _suffix(item: Mapping[str, object], deployment_id: str) -> str:
    sort_key = item.get("SK")
    if not isinstance(sort_key, str):
        raise StoredDataError("deployment item sort key is invalid")
    return sort_key.removeprefix(f"DEPLOYMENT#{deployment_id}")


def _apply_outcome(items: tuple[Mapping, ...], deployment_id: str) -> ApplyOutcome:
    """Derive the apply outcome from the dispatch receipt and the verified run item.

    A dispatch receipt only proves the workflow was asked to start; the conclusion is
    never taken from it, and never from an Event either. Only the `#EVENT#{run_id}` item
    that D confirmed by re-reading the run (`status=VERIFIED`) carries a conclusion
    (ADR-0019 §5·§7). A reserved-but-unverified event is still `RUNNING`.
    """
    dispatched = False
    for item in items:
        suffix = _suffix(item, deployment_id)
        if suffix == "#DISPATCH":
            dispatched = True
            continue
        if not suffix.startswith("#EVENT#") or item.get("status") != "VERIFIED":
            continue
        conclusion = item.get("conclusion")
        try:
            return (
                ApplyOutcome.SUCCEEDED
                if WorkflowConclusion(conclusion) is WorkflowConclusion.SUCCESS
                else ApplyOutcome.FAILED
            )
        except ValueError:
            raise StoredDataError("verified run conclusion is invalid") from None
    return ApplyOutcome.RUNNING if dispatched else ApplyOutcome.NOT_STARTED


class DeploymentReadinessReader(Protocol):
    """C's readiness verdict for one deployment, or `None` before a plan exists."""

    def get_readiness_signal(
        self, *, customer_id: str, deployment_id: str
    ) -> DeploymentReadinessSignal | None: ...


def _job_status(job: object) -> JobStatus:
    status = getattr(job, "status", None)
    if not isinstance(status, JobStatus):
        raise StoredDataError("deployment job status is invalid")
    return status


def _current_step(job: object) -> JobCurrentStep:
    step = getattr(job, "current_step", None)
    if not isinstance(step, JobCurrentStep):
        raise StoredDataError("deployment job current step is invalid")
    return step
