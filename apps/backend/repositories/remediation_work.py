"""A-owned authoritative DynamoDB reader for C Remediation Worker work."""

from collections.abc import Mapping
from decimal import Decimal

from apps.backend.remediation.worker import RemediationWork
from apps.backend.repositories.dynamodb import DynamoTable
from apps.backend.repositories.ports import RepositoryError, StoredDataError
from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    EvaluationPerspective,
    EvaluationStatus,
    Finding,
    IaCSnapshot,
    ManualReviewCode,
    RemediationAction,
    RemediationContext,
    RemediationDecision,
)


class DynamoDbRemediationWorkRepository:
    """Resolve one Job globally, then load its tenant-scoped remediation record."""

    def __init__(self, table: DynamoTable) -> None:
        if table is None:
            raise TypeError("table is required")
        self._table = table

    def get_work(self, *, job_id: str, expected_revision: int) -> RemediationWork | None:
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("job_id must be a non-empty string")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ValueError("expected_revision must be a non-negative integer")
        try:
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
                job.get("job_type") != "REMEDIATION"
                or _revision(job.get("revision")) != expected_revision
                or job.get("job_id") != job_id
            ):
                return None
            customer_id = _string(job.get("customer_id"), "customer_id")
            remediation_id = _string(job.get("remediation_id"), "remediation_id")
            item = self._table.get_item(
                Key={
                    "PK": f"CUSTOMER#{customer_id}",
                    "SK": f"REMEDIATION#{remediation_id}",
                },
                ConsistentRead=True,
            ).get("Item")
            if item is None:
                return None
            remediation = _mapping(item)
            if (
                remediation.get("customer_id") != customer_id
                or remediation.get("job_id") != job_id
                or remediation.get("remediation_id") != remediation_id
            ):
                raise StoredDataError("stored remediation scope is invalid")
            return RemediationWork(
                customer_id=customer_id,
                remediation_id=remediation_id,
                job_id=job_id,
                revision=expected_revision,
                context=remediation_context_from_item(_mapping(remediation.get("context"))),
                decision=_decision(_mapping(remediation.get("decision"))),
            )
        except StoredDataError:
            raise
        except (KeyError, TypeError, ValueError):
            raise StoredDataError("stored remediation work is invalid") from None
        except Exception:
            raise RepositoryError("remediation work read failed") from None


def remediation_context_from_item(value: Mapping[str, object]) -> RemediationContext:
    """Rebuild the stored C context (Finding + snapshot + evidence).

    Shared with the deployment approval path: readiness is judged against the same
    context the Worker used, so both must read it the same way. A second parser would
    let the two drift and judge a plan against a Finding it was not generated from.
    """
    finding_value = _mapping(value.get("finding"))
    snapshot_value = _mapping(value.get("snapshot"))
    artifact_value = _mapping(snapshot_value.get("artifact"))
    finding = Finding(
        finding_id=_string(finding_value.get("finding_id"), "finding_id"),
        resource_id=_string(finding_value.get("resource_id"), "resource_id"),
        rule_id=_string(finding_value.get("rule_id"), "rule_id"),
        rule_version=_string(finding_value.get("rule_version"), "rule_version"),
        perspective=EvaluationPerspective(finding_value.get("perspective")),
        status=EvaluationStatus(finding_value.get("status")),
        severity=_string(finding_value.get("severity"), "severity"),
        score=_number(finding_value.get("score"), "score"),
        rationale=_string(finding_value.get("rationale"), "rationale"),
        evidence_references=_strings(
            finding_value.get("evidence_references"), "finding evidence_references"
        ),
        assessed_commit_sha=finding_value.get("assessed_commit_sha"),
        evaluated_at=finding_value.get("evaluated_at"),
    )
    snapshot = IaCSnapshot(
        customer_id=_string(snapshot_value.get("customer_id"), "snapshot customer_id"),
        repository_id=_string(snapshot_value.get("repository_id"), "repository_id"),
        commit_sha=_string(snapshot_value.get("commit_sha"), "commit_sha"),
        artifact=ArtifactReference(
            artifact_id=_string(artifact_value.get("artifact_id"), "artifact_id"),
            artifact_type=ArtifactType(artifact_value.get("artifact_type")),
            content_sha256=_string(artifact_value.get("content_sha256"), "content_sha256"),
            customer_id=_string(artifact_value.get("customer_id"), "artifact customer_id"),
            repository_id=artifact_value.get("repository_id"),
        ),
    )
    return RemediationContext(
        finding=finding,
        snapshot=snapshot,
        evidence_references=_strings(
            value.get("evidence_references"), "context evidence_references"
        ),
        source_assessment_id=(
            None
            if value.get("source_assessment_id") is None
            else _string(value.get("source_assessment_id"), "source_assessment_id")
        ),
    )


def _decision(value: Mapping[str, object]) -> RemediationDecision:
    manual = value.get("manual_review_code")
    return RemediationDecision(
        finding_id=_string(value.get("finding_id"), "decision finding_id"),
        resource_id=_string(value.get("resource_id"), "decision resource_id"),
        rule_id=_string(value.get("rule_id"), "decision rule_id"),
        rule_version=_string(value.get("rule_version"), "decision rule_version"),
        perspective=EvaluationPerspective(value.get("perspective")),
        action=RemediationAction(value.get("action")),
        manual_review_code=None if manual is None else ManualReviewCode(manual),
        exception_id=value.get("exception_id"),
    )


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("stored value must be a mapping")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be a list")
    return tuple(_string(item, name) for item in value)


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TypeError(f"{name} must be numeric")
    return float(value)


def _revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise TypeError("revision must be numeric")
    return int(value)
