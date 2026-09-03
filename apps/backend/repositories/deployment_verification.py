"""A-owned DynamoDB adapters for starting a Post-Deploy Verification (ADR-0020 §1·§3·§7).

두 경계를 담는다.

- `DynamoDbVerificationSourceReader`: 원 Assessment의 durable 사실을 `VerificationSource`로
  모은다. Repository·Profile·Profile 판본은 `ASSESSMENT#` item에서, planned 집합은 `#PLAN` item에서,
  Model Profile과 rubric은 **결과에서 파생**한다 — Initial Assessment item에는 그 pin이 없고
  (ADR-0020 §3), 결과의 값이 "실제로 이 값으로 평가했다"는 사실이다(`comparison_input`과 같은
  규칙).
- `DynamoDbPostDeployVerificationStore`: 검증 Assessment item, 다음 revision의 Deployment Job,
  `ASSESS_RESOURCE` outbox, Deployment record의 `verification_assessment_id`를 **하나의 조건부
  transaction**으로 쓴다. 넷 중 하나만 써지면 검증이 시작됐다고 보이는데 평가되지 않거나, 평가는
  되는데 화면이 그 Assessment를 찾지 못한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from apps.backend.assessment.models import Assessment
from apps.backend.assessment.reporting import AssessmentReport
from apps.backend.assessment.verification import VerificationSource
from apps.backend.jobs.models import Job
from apps.backend.jobs.outbox import WorkflowOutboxEntry
from apps.backend.repositories.comparison_input import _observed_scope
from apps.backend.repositories.deployment import (
    DynamoTransactionClient,
    _error_code,
    _job_item,
    _outbox_item,
)
from apps.backend.repositories.dynamodb import DynamoTable, _item_from_assessment
from apps.backend.repositories.dynamodb_values import marshal_item
from apps.backend.repositories.errors import DuplicateJobError, RepositoryError, StoredDataError
from packages.contracts import AssessmentPhase, PlannedEvaluation, WorkflowCommand


class AssessmentReportReader(Protocol):
    def get_report(self, *, customer_id: str, assessment_id: str) -> AssessmentReport: ...

    def get_planned_evaluations(
        self, *, customer_id: str, assessment_id: str
    ) -> tuple[PlannedEvaluation, ...]: ...


class DynamoDbVerificationSourceReader:
    """Assemble the source Assessment's pinned scope from its stored items."""

    def __init__(self, table: DynamoTable, *, reports: AssessmentReportReader) -> None:
        if table is None:
            raise TypeError("table is required")
        if reports is None:
            raise TypeError("reports reader is required")
        self._table = table
        self._reports = reports

    def get_verification_source(
        self, *, customer_id: str, assessment_id: str
    ) -> VerificationSource:
        for value, name in ((customer_id, "customer_id"), (assessment_id, "assessment_id")):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        try:
            item = self._table.get_item(
                Key={"PK": f"CUSTOMER#{customer_id}", "SK": f"ASSESSMENT#{assessment_id}"},
                ConsistentRead=True,
            ).get("Item")
        except Exception:
            raise RepositoryError("source assessment read failed") from None
        if not isinstance(item, Mapping) or item.get("entity_type") != "ASSESSMENT":
            raise StoredDataError("source assessment not found")
        if item.get("customer_id") != customer_id or item.get("assessment_id") != assessment_id:
            raise StoredDataError("source assessment scope is invalid")
        repository_id = _text(item, "repository_id")
        policy_profile_id = _text(item, "policy_profile_id")
        # 판본이 없는 record는 backfill 대상이다. 최신 pointer로 조용히 대체하면 검증이 원
        # 평가와 다른 allow-list로 돌아간다 (ADR-0020 amendment).
        policy_profile_version = _text(item, "policy_profile_version")
        raw_phase = item.get("phase")
        try:
            phase = AssessmentPhase.INITIAL if raw_phase is None else AssessmentPhase(raw_phase)
        except ValueError:
            raise StoredDataError("source assessment phase is invalid") from None
        planned = self._reports.get_planned_evaluations(
            customer_id=customer_id, assessment_id=assessment_id
        )
        report = self._reports.get_report(customer_id=customer_id, assessment_id=assessment_id)
        model_profile_id, rubric_version = _observed_scope(report, assessment_id)
        try:
            return VerificationSource(
                assessment_id=assessment_id,
                customer_id=customer_id,
                repository_id=repository_id,
                policy_profile_id=policy_profile_id,
                policy_profile_version=policy_profile_version,
                model_profile_id=model_profile_id,
                rubric_version=rubric_version,
                phase=phase,
                planned_coordinates=planned,
            )
        except (TypeError, ValueError) as error:
            raise StoredDataError("source assessment is not a complete verification source") from (
                error
            )


class DynamoDbPostDeployVerificationStore:
    """Write the verification Assessment, resumed Job, task, and record link atomically."""

    def __init__(self, *, table_name: str, transaction_client: DynamoTransactionClient) -> None:
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("table_name must be a non-empty string")
        if transaction_client is None:
            raise TypeError("transaction_client is required")
        self._table_name = table_name
        self._transaction_client = transaction_client

    def create_verification_assessment(
        self,
        *,
        assessment: Assessment,
        job: Job,
        expected_revision: int,
        outbox: WorkflowOutboxEntry,
    ) -> None:
        if not isinstance(assessment, Assessment):
            raise TypeError("assessment must be an Assessment")
        if not isinstance(job, Job):
            raise TypeError("job must be a Job")
        if not isinstance(outbox, WorkflowOutboxEntry):
            raise TypeError("outbox must be a WorkflowOutboxEntry")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise TypeError("expected_revision must be an integer")
        if assessment.phase is not AssessmentPhase.POST_DEPLOY_VERIFICATION:
            raise ValueError("only a POST_DEPLOY_VERIFICATION assessment is written here")
        if (
            assessment.customer_id != job.customer_id
            or assessment.job_id != job.job_id
            or job.assessment_id != assessment.assessment_id
            or job.deployment_id != assessment.deployment_id
        ):
            raise ValueError("assessment, job, and deployment identities are inconsistent")
        if job.revision != expected_revision + 1:
            raise ValueError("resumed job must be exactly one revision ahead")
        if (
            outbox.customer_id != job.customer_id
            or outbox.job_id != job.job_id
            or outbox.task.expected_revision != job.revision
            or outbox.task.command is not WorkflowCommand.ASSESS_RESOURCE
        ):
            raise ValueError("verification outbox scope, revision, or command is inconsistent")
        deployment_id = assessment.deployment_id
        assert deployment_id is not None  # the Assessment contract requires it for this phase
        try:
            self._transaction_client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": marshal_item(_item_from_assessment(assessment)),
                            # 검증 Assessment는 새 ID다. 원 Assessment를 덮어쓰지 않는다 (§1).
                            "ConditionExpression": "attribute_not_exists(SK)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": marshal_item(_job_item(job)),
                            # 같은 revision에서 시작한 실행만 다음 revision을 쓴다.
                            "ConditionExpression": "#revision = :expected",
                            "ExpressionAttributeNames": {"#revision": "revision"},
                            "ExpressionAttributeValues": marshal_item(
                                {":expected": expected_revision}
                            ),
                        }
                    },
                    # Outbox는 Job 하나당 한 칸이고 단계마다 새 task로 다시 채운다(overwrite) —
                    # apply 완료 예약과 같은 규칙이다. Job revision 조건이 중복 재개를 막는다.
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": marshal_item(_outbox_item(outbox)),
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": marshal_item(
                                {
                                    "PK": f"CUSTOMER#{assessment.customer_id}",
                                    "SK": f"DEPLOYMENT#{deployment_id}",
                                }
                            ),
                            "UpdateExpression": "SET verification_assessment_id = :assessment_id",
                            # 한 Deployment는 한 번만 검증 Assessment를 얻는다. 두 번째 write는
                            # 조용히 덮어쓰지 않고 충돌로 드러난다.
                            "ConditionExpression": (
                                "deployment_id = :deployment_id "
                                "AND attribute_not_exists(verification_assessment_id)"
                            ),
                            "ExpressionAttributeValues": marshal_item(
                                {
                                    ":assessment_id": assessment.assessment_id,
                                    ":deployment_id": deployment_id,
                                }
                            ),
                        }
                    },
                ]
            )
        except Exception as error:
            if _error_code(error) in {
                "ConditionalCheckFailedException",
                "TransactionCanceledException",
            }:
                raise DuplicateJobError("verification assessment is already started") from None
            raise RepositoryError("verification assessment write failed") from None


def _text(item: Mapping[str, object], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise StoredDataError(f"source assessment {key} is missing")
    return value
