"""M1 SQS worker composition with the approved, version-pinned Rule Registry.

The packaged worker remains deliberately fixture-backed until a customer-approved
AWS/GitHub integration is configured.  It must nevertheless load the same
multi-rule registry that the M1 report, coverage, and readiness flows use; the
old M0 one-rule profile is only retained for isolated compatibility tests.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from agent.context import AssessmentInputCollector, AwsResourceSelector, SnapshotReadRequest
from agent.runtime import (
    AwsResourceTool,
    GitHubAppTokenProvider,
    GitHubRestSnapshotTool,
    build_actual_resource_tool,
)
from apps.backend.assessment import (
    ActualBedrockEvaluator,
    ActualEvidenceLoader,
    AssessmentEvaluationPlan,
    AssessmentReportNotFoundError,
    AssessmentReportStoreError,
    AssessmentResourceWork,
    AssessmentRunner,
    AssessmentWorker,
    BedrockConverseClientFactory,
    BedrockStructuredEvaluator,
    DynamoDbAssessmentReportStore,
    DynamoDbEvaluationResultStore,
    InMemoryModelProfileRegistry,
    M1RuntimeConfiguration,
    M1RuntimeConfigurationError,
)
from apps.backend.assessment.execution_plan import EvaluationExecutionPlanner
from apps.backend.assessment.manual_review import ManualReviewEvaluator, governance_resource_id
from apps.backend.assessment.runtime_config import M1AssessmentResource, M1AssessmentTarget
from apps.backend.policy import (
    DynamoDbPolicyCatalog,
    NoApplicablePolicyRulesError,
    PolicyContext,
    PolicyContextResolver,
    load_rule_registry,
)
from apps.backend.policy.control_catalog import GOVERNANCE_ASSESSMENT_RESOURCE_TYPE
from packages.contracts import (
    AssessmentPhase,
    AwsResourceOperation,
    DecisionSource,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    ModelProfile,
    ModelProfileRole,
    PlannedEvaluation,
    PolicyRule,
    WorkflowCommand,
    WorkflowTask,
    score_for_status,
)


class PlannedEvaluationReader(Protocol):
    """Read the immutable planned set a verification Assessment has to reuse."""

    def get_planned_evaluations(
        self, *, customer_id: str, assessment_id: str
    ) -> tuple[PlannedEvaluation, ...]: ...


#: live Worker가 실제로 가진 runner 집합. **계획과 실행이 같은 planner를 통과한다** (ADR-0023 §8).
#: 계획을 여기 말고 다른 곳에서 세 관점으로 하드코딩하면, IaC 전용 Rule의 AWS_ACTUAL/DRIFT 좌표가
#: 계획에는 있고 실행에는 없어 coverage가 영원히 완료되지 않는다.
_LIVE_PLANNER = EvaluationExecutionPlanner(
    available_perspectives=(EvaluationPerspective.IAC, EvaluationPerspective.AWS_ACTUAL),
    derive_drift=True,
)
#: 승인된 MANUAL Rule은 governance 좌표에서 MANUAL 관점 하나만 만든다.
_MANUAL_PLANNER = EvaluationExecutionPlanner(
    available_perspectives=(EvaluationPerspective.MANUAL,), derive_drift=False
)


def _planner_for(resource_type: str) -> EvaluationExecutionPlanner:
    if resource_type == GOVERNANCE_ASSESSMENT_RESOURCE_TYPE:
        return _MANUAL_PLANNER
    return _LIVE_PLANNER


@dataclass(frozen=True, slots=True, kw_only=True)
class _StoredScope:
    """The phase and, for a verification, the scope pinned to the source Assessment."""

    phase: AssessmentPhase
    planned_coordinates: tuple[PlannedEvaluation, ...] | None = None
    expected_profile_version: str | None = None


class DynamoFixtureWorkRepository:
    """Reload authoritative selector IDs, then bind them to the M0 synthetic S3 input."""

    def __init__(
        self,
        table: object,
        snapshot: Mapping[str, object],
        *,
        model_profile: ModelProfile,
        plan_reader: PlannedEvaluationReader | None = None,
    ) -> None:
        if not isinstance(model_profile, ModelProfile):
            raise TypeError("model_profile must be a ModelProfile")
        self._table = table
        self._snapshot = snapshot
        self._model_profile = model_profile
        self._plan_reader = plan_reader

    def get_resource_work(
        self, *, job_id: str, expected_revision: int
    ) -> AssessmentResourceWork | None:
        response = self._table.query(
            IndexName="GSI1",
            KeyConditionExpression="GSI1PK = :job_id",
            ExpressionAttributeValues={":job_id": f"JOB#{job_id}"},
            Limit=2,
        )
        jobs = response.get("Items", [])
        if not isinstance(jobs, list) or len(jobs) != 1:
            return None
        job = jobs[0]
        if not isinstance(job, Mapping) or job.get("revision") != expected_revision:
            return None
        customer_id, assessment_id = job.get("customer_id"), job.get("assessment_id")
        if not isinstance(customer_id, str) or not isinstance(assessment_id, str):
            return None
        assessment = self._table.get_item(
            Key={"PK": f"CUSTOMER#{customer_id}", "SK": f"ASSESSMENT#{assessment_id}"},
            ConsistentRead=True,
        ).get("Item")
        if not isinstance(assessment, Mapping):
            return None
        profile_id = assessment.get("policy_profile_id")
        if not isinstance(profile_id, str):
            return None
        scope = _stored_assessment_scope(
            assessment,
            assessment_id=assessment_id,
            customer_id=customer_id,
            model_profile=self._model_profile,
            plan_reader=self._plan_reader,
        )
        return AssessmentResourceWork(
            customer_id=customer_id,
            assessment_id=assessment_id,
            job_id=job_id,
            revision=expected_revision,
            policy_profile_id=profile_id,
            phase=scope.phase,
            resource_id=_string(self._snapshot.get("resource_id")),
            resource_type=_string(self._snapshot.get("resource_type")),
            perspective=EvaluationPerspective(_string(self._snapshot.get("perspective"))),
            model_profile_id=self._model_profile.model_profile_id,
            planned_coordinates=scope.planned_coordinates,
            expected_profile_version=scope.expected_profile_version,
        )


class DynamoM1WorkRepository:
    """Reload a Job and resolve its live target only from protected Worker config."""

    def __init__(
        self,
        table: object,
        configuration: M1RuntimeConfiguration,
        *,
        model_profile: ModelProfile,
        plan_reader: PlannedEvaluationReader | None = None,
    ) -> None:
        if not isinstance(model_profile, ModelProfile):
            raise TypeError("model_profile must be a ModelProfile")
        self._table = table
        self._configuration = configuration
        self._model_profile = model_profile
        self._plan_reader = plan_reader
        self._targets: dict[tuple[str, int], object] = {}
        self._works: dict[tuple[str, int], tuple[AssessmentResourceWork, ...]] = {}

    def get_resource_work(
        self, *, job_id: str, expected_revision: int
    ) -> AssessmentResourceWork | None:
        """Return the sole work item for compatibility with ``AssessmentWorker``.

        The live composition expands a multi-resource Assessment with
        :meth:`get_resource_works` and gives each item to a fixed repository. Calling this
        legacy single-item method for a multi-resource Assessment is therefore an error,
        not permission to pick whichever resource happens to be first.
        """
        works = self.get_resource_works(job_id=job_id, expected_revision=expected_revision)
        if not works:
            return None
        if len(works) != 1:
            raise M1RuntimeConfigurationError(
                "multi-resource assessment must be expanded before worker execution"
            )
        return works[0]

    def get_resource_works(
        self, *, job_id: str, expected_revision: int
    ) -> tuple[AssessmentResourceWork, ...]:
        """Reload once and expand an Assessment over its approved resource set.

        Public Assessment creation deliberately accepts only repository/profile selectors;
        resource coordinates come from the protected Worker configuration. If an older or
        internal Initial Assessment record pins one approved resource, only it is used.
        A verification without an explicit selector is narrowed to resource ids in the
        source Assessment's immutable plan.
        """
        cached = self._works.get((job_id, expected_revision))
        if cached is not None:
            return cached
        response = self._table.query(
            IndexName="GSI1",
            KeyConditionExpression="GSI1PK = :job_id",
            ExpressionAttributeValues={":job_id": f"JOB#{job_id}"},
            Limit=2,
        )
        jobs = response.get("Items", [])
        if not isinstance(jobs, list) or len(jobs) != 1:
            return ()
        job = jobs[0]
        if not isinstance(job, Mapping) or job.get("revision") != expected_revision:
            return ()
        customer_id, assessment_id = job.get("customer_id"), job.get("assessment_id")
        if not isinstance(customer_id, str) or not isinstance(assessment_id, str):
            return ()
        assessment = self._table.get_item(
            Key={"PK": f"CUSTOMER#{customer_id}", "SK": f"ASSESSMENT#{assessment_id}"},
            ConsistentRead=True,
        ).get("Item")
        if not isinstance(assessment, Mapping):
            return ()
        repository_id, profile_id = (
            assessment.get("repository_id"),
            assessment.get("policy_profile_id"),
        )
        if not isinstance(repository_id, str) or not isinstance(profile_id, str):
            return ()
        scope = _stored_assessment_scope(
            assessment,
            assessment_id=assessment_id,
            customer_id=customer_id,
            model_profile=self._model_profile,
            plan_reader=self._plan_reader,
        )
        # Runtime configuration은 Repository/AWS Resource 경계만 답한다. 어떤 Profile을
        # 쓸지는 Assessment record가 고정한 판본이 정하고, 그 Profile이 이 고객에게 존재하는지는
        # Catalog가 판정한다.
        target = self._configuration.resolve(customer_id=customer_id, repository_id=repository_id)
        selector = _stored_resource_selector(assessment)
        if scope.planned_coordinates is not None:
            planned_resource_ids = {
                coordinate.resource_id for coordinate in scope.planned_coordinates
            }
            # governance 좌표는 배포 설정의 리소스가 아니라 Repository 단위 좌표다. 승인 목록에
            # 없다고 거부하면 MANUAL Rule을 가진 원 Assessment는 검증될 수 없다.
            approved_resource_ids = {resource.resource_id for resource in target.resources} | {
                governance_resource_id(repository_id)
            }
            if not planned_resource_ids.issubset(approved_resource_ids):
                raise M1RuntimeConfigurationError(
                    "verification plan contains a resource outside M1 runtime scope"
                )
            resources = tuple(
                resource
                for resource in target.resources
                if resource.resource_id in planned_resource_ids
            )
        elif selector is not None:
            resources = (target.resolve_resource(selector),)
        else:
            # Initial Assessments created by the public API carry no resource coordinates.
            # The protected target is the approval boundary, so evaluate all of it rather
            # than requiring a client-controlled selector or silently choosing one item.
            resources = target.resources
        if not resources:
            raise M1RuntimeConfigurationError("assessment resolves no approved resources")
        self._targets[(job_id, expected_revision)] = target
        works = tuple(
            AssessmentResourceWork(
                customer_id=customer_id,
                assessment_id=assessment_id,
                job_id=job_id,
                revision=expected_revision,
                policy_profile_id=profile_id,
                phase=scope.phase,
                resource_id=resource.resource_id,
                resource_type=resource.resource_type,
                # The live Worker runs the full perspective set, so this declares the
                # primary evaluated perspective rather than the only one.
                perspective=EvaluationPerspective.AWS_ACTUAL,
                model_profile_id=self._model_profile.model_profile_id,
                planned_coordinates=scope.planned_coordinates,
                expected_profile_version=scope.expected_profile_version,
                assessed_commit_sha=target.commit_sha,
            )
            for resource in resources
        )
        self._works[(job_id, expected_revision)] = works
        return works

    def target_for(self, *, job_id: str, expected_revision: int) -> object:
        works = self.get_resource_works(job_id=job_id, expected_revision=expected_revision)
        if not works:
            raise LookupError("M1 assessment work is missing or stale")
        return self._targets[(job_id, expected_revision)]


class _ResolvedM1WorkRepository:
    """Expose one already-authorized work item through the Worker repository port."""

    def __init__(self, work: AssessmentResourceWork) -> None:
        self._work = work

    def get_resource_work(
        self, *, job_id: str, expected_revision: int
    ) -> AssessmentResourceWork | None:
        if job_id != self._work.job_id or expected_revision != self._work.revision:
            return None
        return self._work


class SyntheticS3Evaluator:
    def __init__(self, snapshot: Mapping[str, object]) -> None:
        self._snapshot = snapshot

    def evaluate(
        self,
        *,
        resource_id: str,
        rule: PolicyRule,
        context: PolicyContext,
        model_profile: ModelProfile,
    ) -> EvaluationResult:
        block = self._snapshot.get("public_access_block")
        if not isinstance(block, Mapping):
            raise ValueError("M0 synthetic snapshot is invalid")
        compliant = all(value is True for value in block.values())
        evidence = self._snapshot.get("evidence_references")
        if not isinstance(evidence, list) or not all(isinstance(value, str) for value in evidence):
            raise ValueError("M0 synthetic snapshot is invalid")
        status = EvaluationStatus.PASS if compliant else EvaluationStatus.FAIL
        return EvaluationResult(
            resource_id=resource_id,
            rule_id=rule.rule_id,
            perspective=EvaluationPerspective.IAC,
            status=status,
            severity=rule.severity.value,
            score=score_for_status(status),
            rationale="M0 synthetic S3 public-access-block evaluation",
            evidence_references=tuple(evidence),
            rule_version=rule.version,
            rubric_version=model_profile.rubric_version,
            model_profile_id=model_profile.model_profile_id,
            decided_by=DecisionSource.CODE,
        )


def lambda_handler(event: Mapping[str, object], context: object) -> None:
    """Run live M1 only with an explicit protected configuration; otherwise M0 fixture mode."""
    raw_m1_configuration = os.environ.get("M1_ASSESSMENT_RUNTIME_JSON")
    if raw_m1_configuration:
        _m1_handler(event, raw_m1_configuration)
        return
    if os.environ.get("M0_SYNTHETIC_ASSESSMENT") != "true":
        raise RuntimeError("M0 synthetic assessment mode is not enabled")
    try:
        import boto3
    except ImportError as error:  # pragma: no cover
        raise RuntimeError("AWS Lambda boto3 runtime is required") from error
    snapshot = _fixture("s3_resource_snapshot.json")
    profile_data = _fixture("assessment_model_profile.json")
    profile = ModelProfile(
        model_profile_id=_string(profile_data.get("model_profile_id")),
        role=ModelProfileRole(_string(profile_data.get("role"))),
        region=_string(profile_data.get("region")),
        model_id=_string(profile_data.get("model_id")),
        prompt_version=_string(profile_data.get("prompt_version")),
        rubric_version=_string(profile_data.get("rubric_version")),
        golden_dataset_version=_string(profile_data.get("golden_dataset_version")),
    )
    table_name = _string(os.environ.get("METADATA_TABLE_NAME"))
    table = boto3.resource("dynamodb").Table(table_name)
    registry = load_rule_registry(_rules_path())
    report_store = DynamoDbAssessmentReportStore(table)
    worker = AssessmentWorker(
        work_repository=DynamoFixtureWorkRepository(
            table, snapshot, model_profile=profile, plan_reader=report_store
        ),
        context_resolver=PolicyContextResolver(registry.catalog),
        runner=AssessmentRunner(SyntheticS3Evaluator(snapshot)),
        model_profiles=InMemoryModelProfileRegistry((profile,)),
        result_store=DynamoDbEvaluationResultStore(
            table, table_name=table_name, transaction_client=boto3.client("dynamodb")
        ),
        plan_store=report_store,
    )
    for task in _tasks(event):
        worker.handle(task)


def m1_context_resolver(table: object, *, target: M1AssessmentTarget) -> PolicyContextResolver:
    """Resolve the live M1 Policy Context from the customer's own approved Rules.

    **Runtime의 정본은 고객 partition의 승인된 Rule이다.** 커밋된 fixture Registry를 읽으면,
    고객이 업로드·승인한 정책이 아니라 저장소에 커밋된 Rule로 평가하게 된다 — 그러면 업로드부터
    승인까지의 경계 전체가 결과에 아무 영향을 주지 않는다. `fixtures/rules`는 bootstrap과 테스트
    입력으로만 남는다.

    Catalog는 `target.customer_id`에 묶이므로 다른 고객의 Rule은 이 resolver로 표현할 수 없다.
    """
    return PolicyContextResolver(DynamoDbPolicyCatalog(table, customer_id=target.customer_id))


def _m1_handler(event: Mapping[str, object], raw_configuration: str) -> None:
    try:
        import boto3
    except ImportError as error:  # pragma: no cover
        raise RuntimeError("AWS Lambda boto3 runtime is required") from error
    configuration = M1RuntimeConfiguration.from_json(raw_configuration)
    table_name = _string(os.environ.get("METADATA_TABLE_NAME"))
    table = boto3.resource("dynamodb").Table(table_name)
    profile = _model_profile()
    report_store = DynamoDbAssessmentReportStore(table)
    for task in _tasks(event):
        work_repository = DynamoM1WorkRepository(
            table,
            configuration,
            model_profile=profile,
            plan_reader=report_store,
        )
        target = work_repository.target_for(
            job_id=task.job_id, expected_revision=task.expected_revision
        )
        context_resolver = m1_context_resolver(table, target=target)
        works = work_repository.get_resource_works(
            job_id=task.job_id, expected_revision=task.expected_revision
        )
        if not works:  # pragma: no cover - target_for already refused a stale Job.
            raise RuntimeError("M1 assessment work is missing or stale")
        works = _with_governance_work(works, context_resolver, repository_id=target.repository_id)
        works = _with_complete_evaluation_plan(works, context_resolver)
        _ensure_evaluation_plan(report_store, works[0])
        # 계획에 좌표가 하나도 없는 resource work는 여기서 빠진다. 남겨 두면 아래 루프가 그
        # 리소스의 AWS/GitHub read를 하고 `AssessmentWorker.handle`이 같은 resource type을 다시
        # resolve해 `NoApplicablePolicyRulesError`로 죽는다 — #89가 계획 단계에서만 막았던
        # 바로 그 재시도·DLQ 경로다.
        works = _evaluable_works(works)
        secrets = boto3.client("secretsmanager")
        github = GitHubRestSnapshotTool(
            customer_id=target.customer_id,
            repository_id=target.repository_id,
            repository_full_name=target.github_repository,
            # 저장된 자격이 App private key면 여기서 installation token을 발급한다. 예전처럼
            # token이 들어 있으면 그대로 쓴다 — secret 교체와 배포가 서로를 기다리지 않는다.
            token_provider=GitHubAppTokenProvider(
                secret_reader=lambda secrets=secrets, secret_id=target.github_token_secret_id: (
                    _secret_string(secrets, secret_id)
                )
            ),
        )
        aws = _actual_resource_tool(
            boto3,
            target=target,
            external_id=_secret_string(secrets, target.aws_external_id_secret_id),
        )
        bedrock = BedrockConverseClientFactory(boto3).for_assessment(profile)
        result_store = DynamoDbEvaluationResultStore(
            table, table_name=table_name, transaction_client=boto3.client("dynamodb")
        )
        for work in works:
            if work.resource_type == GOVERNANCE_ASSESSMENT_RESOURCE_TYPE:
                # MANUAL Rule은 아무 도구도 부르지 않는다 — Bedrock도 AWS도 GitHub도. 좌표만
                # 남겨 Coverage와 검증 비교가 그 통제를 알게 한다 (ADR-0023 §7).
                AssessmentWorker(
                    work_repository=_ResolvedM1WorkRepository(work),
                    context_resolver=context_resolver,
                    perspective_runners={
                        EvaluationPerspective.MANUAL: AssessmentRunner(ManualReviewEvaluator())
                    },
                    model_profiles=InMemoryModelProfileRegistry((profile,)),
                    result_store=result_store,
                ).handle(task)
                continue
            # Read both approved inputs before evaluation. The collector exposes no
            # mutation path. A single queue task may cover several protected resources,
            # all bound to the complete plan stored before evaluation begins.
            bundle = AssessmentInputCollector(github_tool=github, aws_tool=aws).collect(
                SnapshotReadRequest(
                    customer_id=target.customer_id,
                    repository_id=target.repository_id,
                    commit_sha=target.commit_sha,
                    aws_account_id=target.aws_account_id,
                    aws_selectors=(
                        AwsResourceSelector(
                            operation=AwsResourceOperation.READ_RESOURCE,
                            resource_type=work.resource_type,
                            resource_id=work.resource_id,
                        ),
                    ),
                    include_iac_document=True,
                )
            )
            if bundle.iac_document is None:  # pragma: no cover - request demands the body.
                raise RuntimeError("approved IaC body is required for the IAC perspective")
            worker = AssessmentWorker(
                work_repository=_ResolvedM1WorkRepository(work),
                context_resolver=context_resolver,
                perspective_runners={
                    EvaluationPerspective.IAC: AssessmentRunner(
                        BedrockStructuredEvaluator(
                            client=bedrock,
                            perspective=EvaluationPerspective.IAC,
                            resource_document=bundle.iac_document.to_dict(),
                            evidence_references=bundle.iac_document.evidence_references,
                        )
                    ),
                    EvaluationPerspective.AWS_ACTUAL: AssessmentRunner(
                        ActualBedrockEvaluator(
                            evidence_loader=ActualEvidenceLoader(
                                tool=aws,
                                customer_id=target.customer_id,
                                aws_account_id=target.aws_account_id,
                                resource_type=work.resource_type,
                            ),
                            client=bedrock,
                        )
                    ),
                },
                derive_drift=True,
                model_profiles=InMemoryModelProfileRegistry((profile,)),
                result_store=result_store,
            )
            worker.handle(task)


def _with_complete_evaluation_plan(
    works: tuple[AssessmentResourceWork, ...], context_resolver: PolicyContextResolver
) -> tuple[AssessmentResourceWork, ...]:
    """Give every Initial resource work the same complete immutable evaluation plan."""
    if not works:
        raise ValueError("works must not be empty")
    stored_plans = {
        work.planned_coordinates for work in works if work.planned_coordinates is not None
    }
    if stored_plans:
        if len(stored_plans) != 1 or any(work.planned_coordinates is None for work in works):
            raise RuntimeError("assessment work carries inconsistent planned coordinates")
        return works
    # **Perspective는 Rule마다 다르다.** IaC 전용 Rule은 IAC만, AWS 전용은 AWS_ACTUAL만, MANUAL은
    # governance 좌표의 MANUAL만 만든다. 세 관점을 모든 Rule에 계획하면 채워질 수 없는 좌표가
    # 생겨 coverage가 100%가 되지 않고 readiness가 영원히 null로 남는다.
    #
    # 한 resource work에 적용 가능한 Rule이 하나도 없는 것("no applicable policy rules")은 오류가
    # 아니라 "이 Profile은 이 리소스 유형을 평가하지 않는다"는 답이다 (예: MANUAL-only Profile을
    # S3 리소스에 적용). 그 work는 좌표 없이 넘어가고, governance 좌표나 다른 리소스가 계획을
    # 채운다. 전체 계획이 비었을 때만 진짜 오류다 — worker가 무한 재시도로 큐를 막지 않도록,
    # 이 예외를 lambda까지 전파시키지 않는다.
    planned_list: list[PlannedEvaluation] = []
    for work in works:
        try:
            resolved = context_resolver.resolve(
                policy_profile_id=work.policy_profile_id,
                phase=work.phase,
                resource_type=work.resource_type,
                expected_profile_version=work.expected_profile_version,
            )
        except NoApplicablePolicyRulesError:
            continue
        for rule in resolved.rules:
            for perspective in _planner_for(work.resource_type).perspectives_for(rule):
                planned_list.append(
                    PlannedEvaluation(
                        resource_id=work.resource_id,
                        rule_id=rule.rule_id,
                        perspective=perspective,
                    )
                )
    planned = tuple(planned_list)
    if not planned:
        raise NoApplicablePolicyRulesError("assessment resolves no evaluable coordinates")
    if len(set(planned)) != len(planned):
        raise RuntimeError("multi-resource assessment plan contains duplicate coordinates")
    return tuple(replace(work, planned_coordinates=planned) for work in works)


def _evaluable_works(
    works: tuple[AssessmentResourceWork, ...],
) -> tuple[AssessmentResourceWork, ...]:
    """Keep only the resource works the immutable plan actually names.

    `_with_complete_evaluation_plan`은 적용 Rule이 없는 리소스를 계획에서 건너뛰지만 work 목록에는
    그대로 남긴다. 그 work를 평가 루프에 넘기면 두 가지가 잘못된다: 읽을 이유가 없는 리소스를
    AWS/GitHub에서 읽고, `AssessmentWorker.handle`이 그 resource type을 다시 resolve해
    `NoApplicablePolicyRulesError`를 올린다. SQS는 그 예외를 재시도하므로 메시지가 DLQ까지
    간다. 계획이 좌표를 갖지 않는 리소스는 "이 Profile은 이 리소스를 평가하지 않는다"는
    답이지 오류가 아니므로 조용히 빠진다. 계획은 이미 저장됐고 coverage 분모도 그 계획이므로,
    빠진 리소스가 coverage를 미완으로 만들지도 않는다.
    """
    kept: list[AssessmentResourceWork] = []
    for work in works:
        planned = work.planned_coordinates or ()
        if any(coordinate.resource_id == work.resource_id for coordinate in planned):
            kept.append(work)
    if not kept:
        raise NoApplicablePolicyRulesError("assessment plan names no evaluable resource")
    return tuple(kept)


def _with_governance_work(
    works: tuple[AssessmentResourceWork, ...],
    context_resolver: PolicyContextResolver,
    *,
    repository_id: str,
) -> tuple[AssessmentResourceWork, ...]:
    """Add the Repository-level MANUAL coordinate when the pinned Profile approves MANUAL Rules.

    좌표는 `governance:{repository_id}`로 Repository 단위 안정 값이다 (ADR-0023 §7). 검증
    Assessment는 원 계획을 그대로 재사용하므로, 계획에 governance 좌표가 있을 때만 그 work를
    만들고 새로 resolve하지 않는다. Initial은 고정된 Profile 판본을 governance 유형으로 resolve해
    적용 가능한 MANUAL Rule이 있을 때만 만든다 — 없으면 `NoApplicablePolicyRulesError`이며 그것은
    오류가 아니라 "이 Profile에는 사람이 검토할 통제가 없다"는 답이다.
    """
    if not works:
        raise ValueError("works must not be empty")
    template = works[0]
    governance_id = governance_resource_id(repository_id)
    if any(work.resource_type == GOVERNANCE_ASSESSMENT_RESOURCE_TYPE for work in works):
        return works
    if template.planned_coordinates is not None:
        planned_here = any(
            coordinate.resource_id == governance_id for coordinate in template.planned_coordinates
        )
        if not planned_here:
            return works
    else:
        try:
            context_resolver.resolve(
                policy_profile_id=template.policy_profile_id,
                phase=template.phase,
                resource_type=GOVERNANCE_ASSESSMENT_RESOURCE_TYPE,
                expected_profile_version=template.expected_profile_version,
            )
        except NoApplicablePolicyRulesError:
            return works
    governance = replace(
        template,
        resource_id=governance_id,
        resource_type=GOVERNANCE_ASSESSMENT_RESOURCE_TYPE,
        perspective=EvaluationPerspective.MANUAL,
    )
    return (*works, governance)


def _ensure_evaluation_plan(
    store: DynamoDbAssessmentReportStore, work: AssessmentResourceWork
) -> None:
    """Create the shared plan once, accepting only an identical retry or race winner."""
    if work.planned_coordinates is None:
        raise RuntimeError("M1 assessment work has no complete evaluation plan")
    plan = AssessmentEvaluationPlan(
        customer_id=work.customer_id,
        assessment_id=work.assessment_id,
        planned_coordinates=work.planned_coordinates,
    )
    try:
        existing = store.get_planned_evaluations(
            customer_id=work.customer_id, assessment_id=work.assessment_id
        )
    except AssessmentReportNotFoundError:
        try:
            store.put_plan_if_absent(plan)
            return
        except AssessmentReportStoreError as write_error:
            # A concurrent invocation may have won the conditional write. Read its
            # immutable value and accept it only when this invocation derived the same set.
            try:
                existing = store.get_planned_evaluations(
                    customer_id=work.customer_id, assessment_id=work.assessment_id
                )
            except AssessmentReportNotFoundError:
                raise write_error from None
    if existing != plan.planned_coordinates:
        raise RuntimeError("stored assessment plan differs from resolved resource scope")


def _actual_resource_tool(
    boto3: object,
    *,
    target: object,
    external_id: str,
) -> AwsResourceTool:
    """Build the read-only tool for exactly the resource types this target approves."""
    return build_actual_resource_tool(
        customer_id=target.customer_id,
        aws_account_id=target.aws_account_id,
        role_arn=target.aws_read_role_arn,
        external_id=external_id,
        resource_types=target.resource_types,
        client_factory_provider=_client_factory_provider(boto3),
        sts=boto3.client("sts"),
    )


def _client_factory_provider(boto3: object):
    """Return a provider of lazy, credential-taking clients for one AWS service each.

    The client is built when a read first needs credentials, not at wiring time, so
    configuring a resource type costs nothing until it is actually read.
    """

    def provider(service: str):
        def build(credentials: Mapping[str, str]) -> object:
            return boto3.client(
                service,
                aws_access_key_id=credentials["AccessKeyId"],
                aws_secret_access_key=credentials["SecretAccessKey"],
                aws_session_token=credentials["SessionToken"],
            )

        return build

    return provider


def _tasks(event: Mapping[str, object]) -> tuple[WorkflowTask, ...]:
    records = event.get("Records")
    if not isinstance(records, list):
        raise ValueError("SQS Records are required")
    tasks: list[WorkflowTask] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("SQS record is invalid")
        body = record.get("body")
        if not isinstance(body, str):
            raise ValueError("SQS record body is invalid")
        task_data = json.loads(body)
        if not isinstance(task_data, Mapping):
            raise ValueError("WorkflowTask body is invalid")
        tasks.append(
            WorkflowTask(
                job_id=_string(task_data.get("job_id")),
                expected_revision=task_data.get("expected_revision"),
                command=WorkflowCommand(_string(task_data.get("command"))),
            )
        )
    return tuple(tasks)


def _model_profile() -> ModelProfile:
    # The live M1 worker must not silently keep M0's one-perspective rubric.
    # M1's model/prompt/rubric is rebaselined against its IAC/Actual/Drift
    # Golden set; synthetic M0 remains explicitly fixture-backed above.
    profile_data = _m1_fixture("assessment_model_profile.json")
    return ModelProfile(
        model_profile_id=_string(profile_data.get("model_profile_id")),
        role=ModelProfileRole(_string(profile_data.get("role"))),
        region=_string(profile_data.get("region")),
        model_id=_string(profile_data.get("model_id")),
        prompt_version=_string(profile_data.get("prompt_version")),
        rubric_version=_string(profile_data.get("rubric_version")),
        golden_dataset_version=_string(profile_data.get("golden_dataset_version")),
    )


def _secret_string(client: object, secret_id: str) -> str:
    try:
        response = client.get_secret_value(SecretId=secret_id)
    except Exception:
        raise RuntimeError("M1 runtime secret read failed") from None
    if not isinstance(response, Mapping):
        raise RuntimeError("M1 runtime secret response is invalid")
    return _string(response.get("SecretString"))


def _fixture(name: str) -> dict[str, object]:
    return json.loads(_fixture_path(name).read_text())


def _m1_fixture(name: str) -> dict[str, object]:
    return json.loads((Path(__file__).parents[3] / "fixtures" / "m1" / name).read_text())


def _fixture_path(name: str) -> Path:
    return Path(__file__).parents[3] / "fixtures" / "m0" / name


def _rules_path() -> Path:
    return Path(__file__).parents[3] / "fixtures" / "rules"


def _stored_resource_selector(assessment: Mapping[str, object]) -> M1AssessmentResource | None:
    """Read the resource an Assessment record names, if it names one.

    Both coordinates are required together. A record with only one of them does not
    identify a resource, and guessing the other would silently evaluate a different
    resource than the one the Assessment was created for. The named pair is still checked
    against the approved list by `M1AssessmentTarget.resolve_resource()`; this function only
    reads it.
    """
    resource_type, resource_id = assessment.get("resource_type"), assessment.get("resource_id")
    if resource_type is None and resource_id is None:
        return None
    if not isinstance(resource_type, str) or not resource_type.strip():
        raise ValueError("stored Assessment resource_type is invalid")
    if not isinstance(resource_id, str) or not resource_id.strip():
        raise ValueError("stored Assessment resource_id is invalid")
    return M1AssessmentResource(resource_type=resource_type, resource_id=resource_id)


def _stored_assessment_scope(
    assessment: Mapping[str, object],
    *,
    assessment_id: str,
    customer_id: str,
    model_profile: ModelProfile,
    plan_reader: PlannedEvaluationReader | None,
) -> _StoredScope:
    """Restore the stored phase and, for a verification, the scope it pinned.

    A verification must be evaluated with the Model Profile, rubric, Profile
    version, and planned set of the Assessment it verifies (ADR-0020 §2·§3). The
    Worker runtime is configured with one approved Profile, so a pin that names a
    different one cannot be honoured — the Assessment is refused rather than
    re-evaluated under a Profile that would make the comparison meaningless.
    """
    phase = _stored_assessment_phase(assessment, assessment_id=assessment_id)

    # **모든 phase**가 Profile 판본을 고정한다. 없으면 최신 pointer로 조용히 대체하지 않고
    # 실패한다 — 그렇게 대체하면 실행 도중 게시된 새 Profile이 이미 계획된 평가의 Rule 집합을
    # 바꾸고, 그 사실이 어디에도 남지 않는다. 판본이 없는 기존 record는 backfill 대상이다.
    expected_profile_version = assessment.get("policy_profile_version")
    if not isinstance(expected_profile_version, str) or not expected_profile_version.strip():
        raise ValueError("stored Assessment policy_profile_version pin is missing")

    if phase is not AssessmentPhase.POST_DEPLOY_VERIFICATION:
        return _StoredScope(phase=phase, expected_profile_version=expected_profile_version)
    for name, configured in (
        ("model_profile_id", model_profile.model_profile_id),
        ("rubric_version", model_profile.rubric_version),
    ):
        pinned = assessment.get(name)
        if not isinstance(pinned, str) or not pinned.strip():
            raise ValueError(f"stored verification Assessment {name} pin is missing")
        if pinned != configured:
            raise ValueError(f"stored verification Assessment pins a different {name}")
    if plan_reader is None:
        raise ValueError("verification Assessment requires a planned evaluation reader")
    planned = plan_reader.get_planned_evaluations(
        customer_id=customer_id, assessment_id=str(assessment.get("source_assessment_id"))
    )
    if not isinstance(planned, tuple) or not planned:
        raise ValueError("source Assessment planned evaluations are unavailable")
    return _StoredScope(
        phase=phase,
        planned_coordinates=planned,
        expected_profile_version=expected_profile_version,
    )


def _stored_assessment_phase(
    assessment: Mapping[str, object], *, assessment_id: str
) -> AssessmentPhase:
    raw_phase = assessment.get("phase")
    # `policy_profile_version`은 여기 없다 — 모든 phase가 갖는 값이므로 verification 전용
    # correlation과 섞으면 Initial Assessment가 그것을 가졌다는 이유로 거부된다.
    verification_only = {
        name: assessment.get(name)
        for name in (
            "source_assessment_id",
            "deployment_id",
            "model_profile_id",
            "rubric_version",
        )
    }
    source_assessment_id = verification_only["source_assessment_id"]
    deployment_id = verification_only["deployment_id"]
    if "phase" not in assessment:
        if any(value is not None for value in verification_only.values()):
            raise ValueError("legacy Assessment cannot contain verification correlation")
        return AssessmentPhase.INITIAL
    try:
        phase = AssessmentPhase(raw_phase)
    except (TypeError, ValueError):
        raise ValueError("stored Assessment phase is invalid") from None
    for name, value in verification_only.items():
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"stored Assessment {name} is invalid")
    if phase is AssessmentPhase.POST_DEPLOY_VERIFICATION:
        if not isinstance(source_assessment_id, str) or not isinstance(deployment_id, str):
            raise ValueError("stored verification Assessment correlation is incomplete")
        if source_assessment_id == assessment_id:
            raise ValueError("stored verification Assessment cannot reference itself")
    elif any(value is not None for value in verification_only.values()):
        raise ValueError("stored non-verification Assessment has verification correlation")
    return phase


def _string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("required fixture value is invalid")
    return value
