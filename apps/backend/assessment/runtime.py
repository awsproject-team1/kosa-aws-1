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
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agent.context import AssessmentInputCollector, AwsResourceSelector, SnapshotReadRequest
from agent.runtime import AwsResourceTool, GitHubRestSnapshotTool, build_actual_resource_tool
from apps.backend.assessment import (
    ActualBedrockEvaluator,
    ActualEvidenceLoader,
    AssessmentResourceWork,
    AssessmentRunner,
    AssessmentWorker,
    BedrockConverseClientFactory,
    BedrockStructuredEvaluator,
    DynamoDbAssessmentReportStore,
    DynamoDbEvaluationResultStore,
    InMemoryModelProfileRegistry,
    M1RuntimeConfiguration,
)
from apps.backend.assessment.runtime_config import M1AssessmentResource
from apps.backend.policy import PolicyContext, PolicyContextResolver, load_rule_registry
from packages.contracts import (
    AssessmentPhase,
    AwsResourceOperation,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    ModelProfile,
    ModelProfileRole,
    PlannedEvaluation,
    PolicyRule,
    WorkflowCommand,
    WorkflowTask,
)


class PlannedEvaluationReader(Protocol):
    """Read the immutable planned set a verification Assessment has to reuse."""

    def get_planned_evaluations(
        self, *, customer_id: str, assessment_id: str
    ) -> tuple[PlannedEvaluation, ...]: ...


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
        self._work: dict[tuple[str, int], AssessmentResourceWork] = {}

    def get_resource_work(
        self, *, job_id: str, expected_revision: int
    ) -> AssessmentResourceWork | None:
        """Reload once per task; the resolved work is authoritative for that revision.

        The handler resolves the runtime target before building the Worker, so the
        Worker's own reload must not repeat the Job query and Assessment read.
        """
        cached = self._work.get((job_id, expected_revision))
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
        repository_id, profile_id = (
            assessment.get("repository_id"),
            assessment.get("policy_profile_id"),
        )
        if not isinstance(repository_id, str) or not isinstance(profile_id, str):
            return None
        scope = _stored_assessment_scope(
            assessment,
            assessment_id=assessment_id,
            customer_id=customer_id,
            model_profile=self._model_profile,
            plan_reader=self._plan_reader,
        )
        target = self._configuration.resolve(
            customer_id=customer_id,
            repository_id=repository_id,
            policy_profile_id=profile_id,
        )
        resource = target.resolve_resource(_stored_resource_selector(assessment))
        self._targets[(job_id, expected_revision)] = target
        work = AssessmentResourceWork(
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
        self._work[(job_id, expected_revision)] = work
        return work

    def target_for(self, *, job_id: str, expected_revision: int) -> object:
        work = self.get_resource_work(job_id=job_id, expected_revision=expected_revision)
        if work is None:
            raise LookupError("M1 assessment work is missing or stale")
        return self._targets[(job_id, expected_revision)]


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
        return EvaluationResult(
            resource_id=resource_id,
            rule_id=rule.rule_id,
            perspective=EvaluationPerspective.IAC,
            status=EvaluationStatus.PASS if compliant else EvaluationStatus.FAIL,
            severity=rule.severity.value,
            score=100 if compliant else 20,
            rationale="M0 synthetic S3 public-access-block evaluation",
            evidence_references=tuple(evidence),
            rule_version=rule.version,
            rubric_version=model_profile.rubric_version,
            model_profile_id=model_profile.model_profile_id,
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
        work = work_repository.get_resource_work(
            job_id=task.job_id, expected_revision=task.expected_revision
        )
        if work is None:  # pragma: no cover - target_for already refused a stale Job.
            raise RuntimeError("M1 assessment work is missing or stale")
        secrets = boto3.client("secretsmanager")
        github = GitHubRestSnapshotTool(
            customer_id=target.customer_id,
            repository_id=target.repository_id,
            repository_full_name=target.github_repository,
            token_provider=lambda secrets=secrets, secret_id=target.github_token_secret_id: (
                _secret_string(secrets, secret_id)
            ),
        )
        aws = _actual_resource_tool(
            boto3,
            target=target,
            external_id=_secret_string(secrets, target.aws_external_id_secret_id),
        )
        # Read both approved inputs before evaluation.  The collector exposes no mutation path.
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
        if bundle.iac_document is None:  # pragma: no cover - the request demands the body.
            raise RuntimeError("approved IaC body is required for the IAC perspective")
        bedrock = BedrockConverseClientFactory(boto3).for_assessment(profile)
        worker = AssessmentWorker(
            work_repository=work_repository,
            context_resolver=PolicyContextResolver(load_rule_registry(_rules_path()).catalog),
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
            result_store=DynamoDbEvaluationResultStore(
                table, table_name=table_name, transaction_client=boto3.client("dynamodb")
            ),
            plan_store=report_store,
        )
        worker.handle(task)


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
    if phase is not AssessmentPhase.POST_DEPLOY_VERIFICATION:
        return _StoredScope(phase=phase)
    for name, configured in (
        ("model_profile_id", model_profile.model_profile_id),
        ("rubric_version", model_profile.rubric_version),
    ):
        pinned = assessment.get(name)
        if not isinstance(pinned, str) or not pinned.strip():
            raise ValueError(f"stored verification Assessment {name} pin is missing")
        if pinned != configured:
            raise ValueError(f"stored verification Assessment pins a different {name}")
    expected_profile_version = assessment.get("policy_profile_version")
    if not isinstance(expected_profile_version, str) or not expected_profile_version.strip():
        raise ValueError("stored verification Assessment policy_profile_version pin is missing")
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
    verification_only = {
        name: assessment.get(name)
        for name in (
            "source_assessment_id",
            "deployment_id",
            "model_profile_id",
            "rubric_version",
            "policy_profile_version",
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
