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
from pathlib import Path

from agent.context import AssessmentInputCollector, AwsResourceSelector, SnapshotReadRequest
from agent.runtime import AssumeRoleS3ResourceTool, GitHubRestSnapshotTool
from apps.backend.assessment import (
    AssessmentResourceWork,
    AssessmentRunner,
    AssessmentWorker,
    BedrockConverseClientFactory,
    DynamoDbAssessmentReportStore,
    DynamoDbEvaluationResultStore,
    InMemoryModelProfileRegistry,
    M1RuntimeConfiguration,
    S3ActualBedrockEvaluator,
    S3ActualEvidenceLoader,
)
from apps.backend.policy import PolicyContext, PolicyContextResolver, load_rule_registry
from packages.contracts import (
    AssessmentPhase,
    AwsResourceOperation,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    ModelProfile,
    ModelProfileRole,
    PolicyRule,
    WorkflowCommand,
    WorkflowTask,
)


class DynamoFixtureWorkRepository:
    """Reload authoritative selector IDs, then bind them to the M0 synthetic S3 input."""

    def __init__(self, table: object, snapshot: Mapping[str, object]) -> None:
        self._table = table
        self._snapshot = snapshot

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
        return AssessmentResourceWork(
            customer_id=customer_id,
            assessment_id=assessment_id,
            job_id=job_id,
            revision=expected_revision,
            policy_profile_id=profile_id,
            phase=AssessmentPhase.INITIAL,
            resource_id=_string(self._snapshot.get("resource_id")),
            resource_type=_string(self._snapshot.get("resource_type")),
            perspective=EvaluationPerspective(_string(self._snapshot.get("perspective"))),
            model_profile_id="assessment-nova-lite-m0-v1",
        )


class DynamoM1WorkRepository:
    """Reload a Job and resolve its live target only from protected Worker config."""

    def __init__(self, table: object, configuration: M1RuntimeConfiguration) -> None:
        self._table = table
        self._configuration = configuration
        self._targets: dict[tuple[str, int], object] = {}

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
        repository_id, profile_id = (
            assessment.get("repository_id"),
            assessment.get("policy_profile_id"),
        )
        if not isinstance(repository_id, str) or not isinstance(profile_id, str):
            return None
        target = self._configuration.resolve(
            customer_id=customer_id,
            repository_id=repository_id,
            policy_profile_id=profile_id,
        )
        self._targets[(job_id, expected_revision)] = target
        return AssessmentResourceWork(
            customer_id=customer_id,
            assessment_id=assessment_id,
            job_id=job_id,
            revision=expected_revision,
            policy_profile_id=profile_id,
            phase=AssessmentPhase.INITIAL,
            resource_id=target.s3_bucket_id,
            resource_type="AWS::S3::Bucket",
            perspective=EvaluationPerspective.AWS_ACTUAL,
            model_profile_id="assessment-nova-lite-m0-v1",
        )

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
    table = boto3.resource("dynamodb").Table(_string(os.environ.get("METADATA_TABLE_NAME")))
    registry = load_rule_registry(_rules_path())
    worker = AssessmentWorker(
        work_repository=DynamoFixtureWorkRepository(table, snapshot),
        context_resolver=PolicyContextResolver(registry.catalog),
        runner=AssessmentRunner(SyntheticS3Evaluator(snapshot)),
        model_profiles=InMemoryModelProfileRegistry((profile,)),
        result_store=DynamoDbEvaluationResultStore(table),
        plan_store=DynamoDbAssessmentReportStore(table),
    )
    for task in _tasks(event):
        worker.handle(task)


def _m1_handler(event: Mapping[str, object], raw_configuration: str) -> None:
    try:
        import boto3
    except ImportError as error:  # pragma: no cover
        raise RuntimeError("AWS Lambda boto3 runtime is required") from error
    configuration = M1RuntimeConfiguration.from_json(raw_configuration)
    table = boto3.resource("dynamodb").Table(_string(os.environ.get("METADATA_TABLE_NAME")))
    profile = _model_profile()
    for task in _tasks(event):
        work_repository = DynamoM1WorkRepository(table, configuration)
        target = work_repository.target_for(
            job_id=task.job_id, expected_revision=task.expected_revision
        )
        secrets = boto3.client("secretsmanager")
        github = GitHubRestSnapshotTool(
            customer_id=target.customer_id,
            repository_id=target.repository_id,
            repository_full_name=target.github_repository,
            token_provider=lambda secrets=secrets, secret_id=target.github_token_secret_id: (
                _secret_string(secrets, secret_id)
            ),
        )
        aws = AssumeRoleS3ResourceTool(
            customer_id=target.customer_id,
            aws_account_id=target.aws_account_id,
            role_arn=target.aws_read_role_arn,
            external_id=_secret_string(secrets, target.aws_external_id_secret_id),
            sts=boto3.client("sts"),
            s3_client_factory=lambda credentials: boto3.client(
                "s3",
                aws_access_key_id=credentials["AccessKeyId"],
                aws_secret_access_key=credentials["SecretAccessKey"],
                aws_session_token=credentials["SessionToken"],
            ),
        )
        # Read both approved inputs before evaluation.  The collector exposes no mutation path.
        AssessmentInputCollector(github_tool=github, aws_tool=aws).collect(
            SnapshotReadRequest(
                customer_id=target.customer_id,
                repository_id=target.repository_id,
                commit_sha=target.commit_sha,
                aws_account_id=target.aws_account_id,
                aws_selectors=(
                    AwsResourceSelector(
                        operation=AwsResourceOperation.READ_RESOURCE,
                        resource_type="AWS::S3::Bucket",
                        resource_id=target.s3_bucket_id,
                    ),
                ),
            )
        )
        worker = AssessmentWorker(
            work_repository=work_repository,
            context_resolver=PolicyContextResolver(load_rule_registry(_rules_path()).catalog),
            runner=AssessmentRunner(
                S3ActualBedrockEvaluator(
                    evidence_loader=S3ActualEvidenceLoader(
                        tool=aws,
                        customer_id=target.customer_id,
                        aws_account_id=target.aws_account_id,
                    ),
                    client=BedrockConverseClientFactory(boto3).for_assessment(profile),
                )
            ),
            model_profiles=InMemoryModelProfileRegistry((profile,)),
            result_store=DynamoDbEvaluationResultStore(table),
            plan_store=DynamoDbAssessmentReportStore(table),
        )
        worker.handle(task)


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
    profile_data = _fixture("assessment_model_profile.json")
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


def _fixture_path(name: str) -> Path:
    return Path(__file__).parents[3] / "fixtures" / "m0" / name


def _rules_path() -> Path:
    return Path(__file__).parents[3] / "fixtures" / "rules"


def _string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("required fixture value is invalid")
    return value
