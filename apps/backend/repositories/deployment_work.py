"""A/D 공용 DynamoDB reader: D Deployment Worker의 authoritative work 조립 (ADR-0019).

`DeploymentWorker`는 queue payload가 아니라 `(job_id, revision)`으로 durable work를 다시 읽는다
(ADR-0013). 이 reader가 그 work를 만든다. 여러 item을 합성한다:

- `JOB#{job_id}` (GSI1으로 전역 조회): `job_type`/`revision`/`customer_id`/`deployment_id` 확정.
- `DEPLOYMENT#{deployment_id}`: 대상 commit·repository, PLAN_COMPLETED 이후 채워진 plan facts
  (`plan_hash`/plan·binary artifact/state)와 `plan_run` 좌표.
- `DEPLOYMENT#{deployment_id}#APPROVAL#approval-{deployment_id}`: 승인 사실(있을 때).
- `DEPLOYMENT#{deployment_id}#EVENT#{run_id}`는 이 reader가 만들지 않는다 — apply 완료 run
  좌표(`run_reference`)는 A가 완료 Event를 durable하게 기록한 뒤에야 채워지며(7-B), 그전에는
  None으로 남아 Worker가 APPLY_COMPLETED에서 fail-closed한다.

`aws_account_id`와 재조회 대상은 DeploymentRecord/Job에 없으므로 보호된 runtime configuration이
`(customer_id, repository_id)`로 resolve해 주입한다(assessment M1 runtime과 같은 원리).
"""

from collections.abc import Callable, Mapping
from decimal import Decimal

from apps.backend.deployment.worker import DeploymentWork
from apps.backend.repositories.deployment import (
    _approval_from_item,
    _artifact_from,
    _state_version_from,
)
from apps.backend.repositories.dynamodb import DynamoTable
from apps.backend.repositories.ports import RepositoryError, StoredDataError
from packages.contracts import (
    ArtifactType,
    DeploymentApproval,
    TerraformPlan,
    WorkflowRunReference,
)
from packages.contracts.remediation import RemediationSyncTarget


class DynamoDbDeploymentWorkRepository:
    """Resolve one deployment Job globally, then assemble its authoritative work."""

    def __init__(
        self,
        table: DynamoTable,
        *,
        aws_account_id_for: Callable[[str, str], str],
    ) -> None:
        """`aws_account_id_for(customer_id, repository_id)`는 승인된 sandbox 계정을 돌려준다.

        DeploymentRecord/Job에 없는 값이므로 보호된 runtime configuration이 주입한다. resolve할 수
        없으면 fail-closed로 예외를 던져야 한다(미승인 대상은 work가 만들어지지 않는다).
        """
        if table is None:
            raise TypeError("table is required")
        if not callable(aws_account_id_for):
            raise TypeError("aws_account_id_for must be callable")
        self._table = table
        self._aws_account_id_for = aws_account_id_for

    def get_work(self, *, job_id: str, expected_revision: int) -> DeploymentWork | None:
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("job_id must be a non-empty string")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ValueError("expected_revision must be a non-negative integer")
        try:
            job = self._resolve_job(job_id, expected_revision)
            if job is None:
                return None
            customer_id = _string(job.get("customer_id"), "customer_id")
            deployment_id = _string(job.get("deployment_id"), "deployment_id")
            record = self._deployment_item(customer_id, deployment_id)
            if record is None:
                return None
            repository_id = _string(record.get("repository_id"), "repository_id")
            commit_sha = _string(record.get("commit_sha"), "commit_sha")
            aws_account_id = self._require_aws_account_id(customer_id, repository_id)
            plan = _plan_from_record(record, deployment_id, commit_sha)
            state_version = (
                None
                if record.get("state_version") is None
                else _state_version_from(record.get("state_version"))
            )
            plan_run = _plan_run_from_record(record, deployment_id, repository_id)
            approval = self._approval(customer_id, deployment_id)
            sync_target = (
                None
                if plan is None
                else self._sync_target(record, customer_id, repository_id, commit_sha)
            )
            return DeploymentWork(
                customer_id=customer_id,
                deployment_id=deployment_id,
                repository_id=repository_id,
                aws_account_id=aws_account_id,
                job_id=job_id,
                revision=expected_revision,
                commit_sha=commit_sha,
                plan=plan,
                state_version=state_version,
                plan_run=plan_run,
                approval=approval,
                run_reference=None,
                sync_target=sync_target,
            )
        except StoredDataError:
            raise
        except (KeyError, TypeError, ValueError):
            raise StoredDataError("stored deployment work is invalid") from None
        except RepositoryError:
            raise
        except Exception:
            raise RepositoryError("deployment work read failed") from None

    def _resolve_job(self, job_id: str, expected_revision: int) -> Mapping[str, object] | None:
        response = self._table.query(
            IndexName="GSI1",
            KeyConditionExpression="GSI1PK = :job_id",
            ExpressionAttributeValues={":job_id": f"JOB#{job_id}"},
            Limit=2,
        )
        jobs = response.get("Items", [])
        if not isinstance(jobs, list) or len(jobs) != 1:
            return None
        job = _mapping(jobs[0])
        if (
            job.get("job_type") != "DEPLOYMENT"
            or _revision(job.get("revision")) != expected_revision
            or job.get("job_id") != job_id
        ):
            return None
        return job

    def _deployment_item(self, customer_id: str, deployment_id: str) -> Mapping[str, object] | None:
        item = self._table.get_item(
            Key={"PK": f"CUSTOMER#{customer_id}", "SK": f"DEPLOYMENT#{deployment_id}"},
            ConsistentRead=True,
        ).get("Item")
        if item is None:
            return None
        record = _mapping(item)
        if (
            record.get("entity_type") != "DEPLOYMENT"
            or record.get("customer_id") != customer_id
            or record.get("deployment_id") != deployment_id
        ):
            raise StoredDataError("stored deployment scope is invalid")
        return record

    def _approval(self, customer_id: str, deployment_id: str) -> DeploymentApproval | None:
        item = self._table.get_item(
            Key={
                "PK": f"CUSTOMER#{customer_id}",
                "SK": f"DEPLOYMENT#{deployment_id}#APPROVAL#approval-{deployment_id}",
            },
            ConsistentRead=True,
        ).get("Item")
        if item is None:
            return None
        return _approval_from_item(_mapping(item))

    def _sync_target(
        self,
        record: Mapping[str, object],
        customer_id: str,
        repository_id: str,
        commit_sha: str,
    ) -> RemediationSyncTarget:
        # finding_id는 DEPLOYMENT# item에 없으므로 remediation record에서 읽는다(같은
        # REMEDIATION# item을 C Remediation Worker reader도 쓴다).
        remediation_id = _string(record.get("remediation_id"), "remediation_id")
        item = self._table.get_item(
            Key={"PK": f"CUSTOMER#{customer_id}", "SK": f"REMEDIATION#{remediation_id}"},
            ConsistentRead=True,
        ).get("Item")
        if item is None:
            raise StoredDataError("deployment references a missing remediation")
        finding = _mapping(_mapping(_mapping(item).get("context")).get("finding"))
        return RemediationSyncTarget(
            finding_id=_string(finding.get("finding_id"), "finding_id"),
            customer_id=customer_id,
            repository_id=repository_id,
            commit_sha=commit_sha,
        )

    def _require_aws_account_id(self, customer_id: str, repository_id: str) -> str:
        aws_account_id = self._aws_account_id_for(customer_id, repository_id)
        if not isinstance(aws_account_id, str) or not aws_account_id.strip():
            raise RepositoryError("aws_account_id could not be resolved for the deployment")
        return aws_account_id


def _plan_from_record(
    record: Mapping[str, object], deployment_id: str, commit_sha: str
) -> TerraformPlan | None:
    plan_hash = record.get("plan_hash")
    plan_artifact = record.get("plan_artifact")
    if plan_hash is None or plan_artifact is None:
        return None  # plan facts는 PLAN_COMPLETED 이후에만 존재한다.
    artifact = _artifact_from(plan_artifact, ArtifactType.TERRAFORM_PLAN)
    return TerraformPlan(
        deployment_id=deployment_id,
        commit_sha=commit_sha,
        plan_hash=_string(plan_hash, "plan_hash"),
        artifact=artifact,
    )


def _plan_run_from_record(
    record: Mapping[str, object], deployment_id: str, repository_id: str
) -> WorkflowRunReference | None:
    value = record.get("plan_run")
    if value is None:
        return None
    data = _mapping(value)
    reference = WorkflowRunReference(
        deployment_id=_string(data.get("deployment_id"), "plan_run deployment_id"),
        repository_id=_string(data.get("repository_id"), "plan_run repository_id"),
        run_id=_string(data.get("run_id"), "plan_run run_id"),
    )
    if reference.deployment_id != deployment_id or reference.repository_id != repository_id:
        raise StoredDataError("stored plan_run scope is invalid")
    return reference


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("stored value must be a mapping")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise TypeError("revision must be numeric")
    return int(value)
