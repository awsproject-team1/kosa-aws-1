"""A-owned DynamoDB reader that assembles a remediation handoff from M1 evidence.

`POST /findings/{findingId}/remediations`는 finding_id 하나만 준다. B의 정책 판정과 C의
context 조립은 그 finding이 가리키는 **immutable** 증거(Finding, IAC/Actual 결과, 평가된
commit)를 다시 읽어야 한다. 이 reader는 그 증거를 customer 파티션 안에서만 조회해
`build_remediation_context()`와 `RemediationTarget`으로 되돌린다.

경계 원칙:

- Action 선택은 하지 않는다. 조치 유형은 오직 B의 저장된 `RemediationDecision`이 정한다
  (ADR-0018). 이 reader는 Finding·snapshot·IAC 판정이라는 사실만 되돌린다.
- provenance가 없는 legacy Finding은 remediation 자동화에 쓸 수 없다(CONTRACTS.md M1
  finding boundary). `Finding` Contract가 그 검증을 강제하므로 여기서 중복하지 않는다.
- Finding SK는 `ASSESSMENT#{assessment_id}#FINDING#{finding_id}`라 finding_id만으로는 정확한
  key를 만들 수 없다. customer 파티션을 `begins_with(SK, "ASSESSMENT#")`로 Query하고
  `finding_id` 속성으로 필터한다. Scan이 아니라 단일 파티션 Query이며, 문서의 접근 패턴
  (Assessment/Finding co-location)과 일치한다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Protocol

from apps.backend.assessment.actual import resource_type_for_evidence_reference
from apps.backend.remediation.context import build_remediation_context
from apps.backend.repositories.ports import RepositoryError, StoredDataError
from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    Finding,
    IaCSnapshot,
    RemediationTarget,
    ScoringMode,
)
from packages.contracts.remediation import RemediationContext


class DynamoTable(Protocol):
    def get_item(self, **kwargs: object) -> Mapping[str, object]: ...

    def query(self, **kwargs: object) -> Mapping[str, object]: ...


# 결과 item에는 resource type이 저장되지 않는다(M1 스키마). 대상 유형은 AWS_ACTUAL 결과의
# `aws:` read locator에서 되돌린다 — 그 locator는 `actual_evidence_reference()`가 resource type별
# 접두사로 만든 값이라 결정적으로 역산된다. locator가 없거나 어느 접두사와도 맞지 않는 옛 record는
# M1의 유일한 대상 유형이었던 S3로 본다. B의 `decide()`는 resource_type을 판정에 쓰지 않으므로
# 이 값은 표시·감사용이지만, RDS Finding에 S3라고 적힌 target을 남기지는 않는다.
_LEGACY_RESOURCE_TYPE = "AWS::S3::Bucket"


class DynamoDbRemediationContextReader:
    """Rebuild `RemediationContext` and `RemediationTarget` from immutable M1 evidence.

    두 reader 인터페이스(`get_context`, `get_target`)를 한 구현으로 만족시킨다. 둘 다 같은
    Finding·결과를 읽으므로 별개 구현이면 같은 finding에 서로 다른 사실을 되돌릴 위험이 있다.
    """

    def __init__(self, table: DynamoTable) -> None:
        if table is None:
            raise TypeError("table is required")
        self._table = table

    def get_context(self, *, customer_id: str, finding_id: str) -> RemediationContext:
        finding, assessment_id = self._load_finding(customer_id, finding_id)
        iac_result = self._result(customer_id, assessment_id, finding, EvaluationPerspective.IAC)
        actual_result = self._result(
            customer_id, assessment_id, finding, EvaluationPerspective.AWS_ACTUAL
        )
        snapshot = self._snapshot(customer_id, assessment_id, finding)
        try:
            return build_remediation_context(
                finding=finding,
                snapshot=snapshot,
                iac_result=iac_result,
                actual_result=actual_result,
            )
        except (TypeError, ValueError) as error:
            raise StoredDataError("stored remediation evidence is invalid") from error

    def get_target(self, *, customer_id: str, finding_id: str) -> RemediationTarget:
        finding, assessment_id = self._load_finding(customer_id, finding_id)
        iac_result = self._result(customer_id, assessment_id, finding, EvaluationPerspective.IAC)
        actual_result = self._result(
            customer_id, assessment_id, finding, EvaluationPerspective.AWS_ACTUAL
        )
        try:
            return RemediationTarget(
                resource_id=finding.resource_id,
                resource_type=_resource_type(actual_result),
                rule_id=finding.rule_id,
                rule_version=finding.rule_version,
                # IAC 관점이 평가됐다는 것은 이 리소스가 Terraform으로 관리된다는 뜻이다.
                # M1 live 경로는 Terraform repository의 commit에서 IaC를 읽어 평가한다.
                terraform_managed=True,
                iac_status=iac_result.status,
                iac_perspective=EvaluationPerspective.IAC,
                iac_commit_sha=iac_result.assessed_commit_sha,
            )
        except (TypeError, ValueError) as error:
            raise StoredDataError("stored remediation target is invalid") from error

    def _load_finding(self, customer_id: str, finding_id: str) -> tuple[Finding, str]:
        _non_empty(customer_id, "customer_id")
        _non_empty(finding_id, "finding_id")
        try:
            response = self._table.query(
                KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
                FilterExpression="entity_type = :finding AND finding_id = :fid",
                ExpressionAttributeValues={
                    ":pk": f"CUSTOMER#{customer_id}",
                    ":prefix": "ASSESSMENT#",
                    ":finding": "FINDING",
                    ":fid": finding_id,
                },
            )
        except Exception:
            raise RepositoryError("remediation finding read failed") from None
        items = response.get("Items")
        if not isinstance(items, list):
            raise StoredDataError("remediation finding page is invalid")
        matches = [item for item in items if isinstance(item, Mapping)]
        if not matches:
            raise StoredDataError("remediation finding not found")
        if len(matches) != 1:
            # 같은 finding_id가 여러 Assessment에 있으면 어느 증거 집합을 조치 대상으로
            # 삼을지 모호하다. deterministic ID가 충돌하지 않는 한 일어나지 않지만, 조용히
            # 아무거나 고르는 대신 fail-closed한다.
            raise StoredDataError("remediation finding is ambiguous")
        item = matches[0]
        assessment_id = _string(item.get("assessment_id"), "assessment_id")
        if item.get("customer_id") != customer_id:
            raise StoredDataError("remediation finding scope is invalid")
        return _finding_from_item(item, finding_id), assessment_id

    def _result(
        self,
        customer_id: str,
        assessment_id: str,
        finding: Finding,
        perspective: EvaluationPerspective,
    ) -> EvaluationResult:
        sk = (
            f"ASSESSMENT#{assessment_id}#RESULT#{finding.resource_id}"
            f"#RULE#{finding.rule_id}#PERSPECTIVE#{perspective.value}"
        )
        try:
            item = self._table.get_item(
                Key={"PK": f"CUSTOMER#{customer_id}", "SK": sk}, ConsistentRead=True
            ).get("Item")
        except Exception:
            raise RepositoryError("remediation evaluation result read failed") from None
        if not isinstance(item, Mapping):
            raise StoredDataError(f"remediation evidence is missing the {perspective.value} result")
        if item.get("customer_id") != customer_id or item.get("assessment_id") != assessment_id:
            raise StoredDataError("remediation evaluation result scope is invalid")
        return _result_from_item(item)

    def _snapshot(self, customer_id: str, assessment_id: str, finding: Finding) -> IaCSnapshot:
        try:
            item = self._table.get_item(
                Key={
                    "PK": f"CUSTOMER#{customer_id}",
                    "SK": f"ASSESSMENT#{assessment_id}",
                },
                ConsistentRead=True,
            ).get("Item")
        except Exception:
            raise RepositoryError("remediation assessment read failed") from None
        if not isinstance(item, Mapping) or item.get("entity_type") != "ASSESSMENT":
            raise StoredDataError("remediation assessment not found")
        if item.get("customer_id") != customer_id:
            raise StoredDataError("remediation assessment scope is invalid")
        repository_id = _string(item.get("repository_id"), "repository_id")
        # snapshot이 가리키는 commit은 Finding이 평가된 그 commit이다. Finding provenance가
        # 곧 snapshot 좌표의 근거이므로 별도 필드가 아니라 그 값을 쓴다.
        commit_sha = _string(finding.assessed_commit_sha, "assessed_commit_sha")
        # M1은 IaC snapshot 바이트를 별도 아티팩트로 저장하지 않는다. snapshot 아티팩트는
        # 그 commit의 IaC를 유일하게 규정하는 immutable 좌표로부터 결정적으로 파생한다.
        # generator/deployment 경계는 snapshot에서 commit/repository/customer만 소비하므로
        # (generator.py 참조), 이 파생 참조는 그 좌표와 정확히 일치하는 identity를 제공한다.
        digest = _snapshot_digest(
            customer_id=customer_id, repository_id=repository_id, commit_sha=commit_sha
        )
        try:
            return IaCSnapshot(
                customer_id=customer_id,
                repository_id=repository_id,
                commit_sha=commit_sha,
                artifact=ArtifactReference(
                    artifact_id=f"terraform-snapshot:{repository_id}:{commit_sha}",
                    artifact_type=ArtifactType.TERRAFORM_SNAPSHOT,
                    content_sha256=digest,
                    customer_id=customer_id,
                    repository_id=repository_id,
                ),
            )
        except (TypeError, ValueError) as error:
            raise StoredDataError("remediation snapshot is invalid") from error


def _resource_type(actual_result: EvaluationResult) -> str:
    """The resource type the Actual read observed, restored from its evidence locator."""
    for reference in actual_result.evidence_references:
        resource_type = resource_type_for_evidence_reference(reference)
        if resource_type is not None:
            return resource_type
    return _LEGACY_RESOURCE_TYPE


def _snapshot_digest(*, customer_id: str, repository_id: str, commit_sha: str) -> str:
    """Deterministic SHA-256 over the immutable coordinates of an evaluated IaC snapshot."""
    payload = json.dumps(
        {
            "customer_id": customer_id,
            "repository_id": repository_id,
            "commit_sha": commit_sha,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _finding_from_item(item: Mapping[str, object], finding_id: str) -> Finding:
    if item.get("finding_id") != finding_id:
        raise StoredDataError("remediation finding identity is invalid")
    evidence = item.get("evidence_references")
    if not isinstance(evidence, list):
        raise StoredDataError("remediation finding evidence is invalid")
    try:
        return Finding(
            finding_id=_string(item.get("finding_id"), "finding_id"),
            resource_id=_string(item.get("resource_id"), "resource_id"),
            rule_id=_string(item.get("rule_id"), "rule_id"),
            rule_version=_string(item.get("rule_version"), "rule_version"),
            perspective=EvaluationPerspective(item.get("perspective")),
            status=EvaluationStatus(item.get("status")),
            severity=_string(item.get("severity"), "severity"),
            score=_number(item.get("score"), "score"),
            rationale=_string(item.get("rationale"), "rationale"),
            evidence_references=_strings(evidence, "finding evidence_references"),
            assessed_commit_sha=item.get("assessed_commit_sha"),
            evaluated_at=item.get("evaluated_at"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise StoredDataError("remediation finding is invalid") from error


def _result_from_item(item: Mapping[str, object]) -> EvaluationResult:
    evidence = item.get("evidence_references")
    if not isinstance(evidence, list):
        raise StoredDataError("remediation result evidence is invalid")
    scoring_mode = item.get("scoring_mode")
    try:
        return EvaluationResult(
            resource_id=_string(item.get("resource_id"), "resource_id"),
            rule_id=_string(item.get("rule_id"), "rule_id"),
            perspective=EvaluationPerspective(item.get("perspective")),
            status=EvaluationStatus(item.get("status")),
            severity=_string(item.get("severity"), "severity"),
            score=_number(item.get("score"), "score"),
            rationale=_string(item.get("rationale"), "rationale"),
            evidence_references=_strings(evidence, "result evidence_references"),
            rule_version=_string(item.get("rule_version"), "rule_version"),
            rubric_version=_string(item.get("rubric_version"), "rubric_version"),
            model_profile_id=_string(item.get("model_profile_id"), "model_profile_id"),
            scoring_mode=(
                ScoringMode.CONTINUOUS if scoring_mode is None else ScoringMode(scoring_mode)
            ),
            assessed_commit_sha=item.get("assessed_commit_sha"),
            evaluated_at=item.get("evaluated_at"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise StoredDataError("remediation evaluation result is invalid") from error


def _non_empty(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StoredDataError(f"{name} must be a non-empty string")
    return value


def _strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise StoredDataError(f"{name} must be a list")
    result: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise StoredDataError(f"{name} item must be a non-empty string")
        result.append(entry)
    return tuple(result)


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise StoredDataError(f"{name} must be numeric")
    return float(value)
