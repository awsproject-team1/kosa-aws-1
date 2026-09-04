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
    DecisionSource,
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
        results = self._results(customer_id, assessment_id, finding)
        snapshot = self._snapshot(customer_id, assessment_id, finding)
        try:
            return build_remediation_context(
                finding=finding,
                snapshot=snapshot,
                results=tuple(results.values()),
            )
        except (TypeError, ValueError) as error:
            raise StoredDataError("stored remediation evidence is invalid") from error

    def get_target(self, *, customer_id: str, finding_id: str) -> RemediationTarget:
        finding, assessment_id = self._load_finding(customer_id, finding_id)
        results = self._results(customer_id, assessment_id, finding)
        iac_result = results.get(EvaluationPerspective.IAC)
        actual_result = results.get(EvaluationPerspective.AWS_ACTUAL)
        try:
            return RemediationTarget(
                resource_id=finding.resource_id,
                resource_type=_resource_type(actual_result or results[finding.perspective]),
                rule_id=finding.rule_id,
                rule_version=finding.rule_version,
                # M1 live 경로는 Terraform repository의 commit에서 IaC를 읽어 평가하므로, 이
                # 경로에 닿은 리소스는 Terraform이 관리한다.
                terraform_managed=True,
                # IaC 판정은 **있을 때만** 싣는다. authoring이 만든 Rule은 `evaluation_type`
                # 하나를 선언하므로 그 관점만 평가된다 — `AWS` Rule에는 IaC 판정이 없다.
                # Contract가 세 필드를 한 묶음으로 요구하므로 함께 비운다. 그 경우
                # `RemediationPolicy.decide()`는 Actual Finding을 `MANUAL_REVIEW`로 돌린다:
                # IaC가 이미 옳은지 모르면 Patch와 동기화를 가를 수 없다.
                iac_status=None if iac_result is None else iac_result.status,
                iac_perspective=None if iac_result is None else EvaluationPerspective.IAC,
                iac_commit_sha=None if iac_result is None else iac_result.assessed_commit_sha,
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
        item = _current_occurrence(matches, finding_id)
        assessment_id = _string(item.get("assessment_id"), "assessment_id")
        if item.get("customer_id") != customer_id:
            raise StoredDataError("remediation finding scope is invalid")
        return _finding_from_item(item, finding_id), assessment_id

    def _results(
        self, customer_id: str, assessment_id: str, finding: Finding
    ) -> dict[EvaluationPerspective, EvaluationResult]:
        """Load whichever machine perspectives this Rule was actually evaluated in.

        예전에는 `IAC`와 `AWS_ACTUAL` 결과가 **둘 다** 있어야 조치 요청이 진행됐다. legacy
        fixture Rule은 `evaluation_type`이 없어 세 관점 모두 평가되므로 그 가정이 보이지 않았지만,
        authoring이 만든 Rule은 관점 하나를 선언한다 — `AWS` Rule에는 IaC 판정이, `IAC` Rule에는
        Actual 판정이 애초에 없다. 그래서 고객이 업로드한 정책에서 나온 Finding은 모두 조치
        요청에서 503이 됐다(라이브에서 그렇게 멈췄다).

        관점의 유무는 정책이 답할 질문이지 저장소가 막을 일이 아니다. 여기서는 있는 것을 모아
        주고, Patch·동기화·사람 검토의 판단은 `RemediationPolicy.decide()`에 남긴다. 다만 Finding
        자신의 관점 결과는 반드시 있어야 한다 — 그것이 이 Finding이 나온 근거이고, 없다면 저장된
        증거가 서로 어긋난 것이다.
        """
        results: dict[EvaluationPerspective, EvaluationResult] = {}
        for perspective in (EvaluationPerspective.IAC, EvaluationPerspective.AWS_ACTUAL):
            result = self._result(customer_id, assessment_id, finding, perspective, required=False)
            if result is not None:
                results[perspective] = result
        if finding.perspective is EvaluationPerspective.DRIFT:
            # DRIFT Finding은 두 관점의 비교다. 하나라도 없으면 비교를 뒷받침할 수 없다.
            for perspective in (EvaluationPerspective.IAC, EvaluationPerspective.AWS_ACTUAL):
                if perspective not in results:
                    raise StoredDataError(
                        f"remediation evidence is missing the {perspective.value} result"
                    )
        elif finding.perspective not in results:
            required = self._result(
                customer_id, assessment_id, finding, finding.perspective, required=True
            )
            assert required is not None
            results[finding.perspective] = required
        return results

    def _result(
        self,
        customer_id: str,
        assessment_id: str,
        finding: Finding,
        perspective: EvaluationPerspective,
        *,
        required: bool = True,
    ) -> EvaluationResult | None:
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
            if not required:
                return None
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


def _resource_type(result: EvaluationResult) -> str:
    """The resource type the evaluation observed, restored from its evidence locator.

    Actual 결과가 있으면 그것을 쓴다 — AWS 조회 locator가 유형을 가장 곧게 말한다. `IAC` 전용
    Rule에는 Actual 결과가 없으므로 Finding 자신의 결과에서 읽는다.
    """
    for reference in result.evidence_references:
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


def _current_occurrence(
    matches: list[Mapping[str, object]], finding_id: str
) -> Mapping[str, object]:
    """Return the newest occurrence of one violation across the Assessments that found it.

    Finding ID는 결정적이다 — 같은 Resource × Rule × Perspective는 어느 Assessment에서 평가하든
    같은 ID를 갖는다. 그것이 의도다: ID는 **위반**을 가리키지 실행을 가리키지 않는다. 그래서 같은
    대상을 두 번 평가하면 같은 ID의 finding item이 둘 생긴다.

    예전에는 그것을 모호함으로 보고 거부했고, 그 결과 **같은 리소스를 두 번째로 평가한 순간부터
    조치 요청이 영영 503**이었다. 라이브에서 finding ID 24개 중 11개가 그 상태였다.

    조치는 지금의 상태를 고치는 일이므로 가장 최근 증거를 쓴다. 순서는 평가 시각으로 정하고,
    같은 시각이면 assessment_id로 결정적으로 가른다 — 같은 요청이 매번 같은 증거를 고른다.

    provenance(`evaluated_at`)가 없는 옛 record는 순서를 매길 수 없다. remediation은 어차피
    provenance를 요구하므로(ADR-0011), 있는 것들 중에서 고른다. 하나도 없으면 예전처럼 하나일
    때만 통과시키고 여럿이면 거부한다 — 무엇이 현재인지 말할 근거가 없다.

    모든 후보는 같은 위반이어야 한다. 좌표가 서로 다르면 ID가 진짜로 충돌한 것이므로 fail-closed다.
    """
    coordinates = {
        (item.get("resource_id"), item.get("rule_id"), item.get("perspective")) for item in matches
    }
    if len(coordinates) != 1:
        raise StoredDataError("remediation finding is ambiguous")
    dated = [item for item in matches if isinstance(item.get("evaluated_at"), str)]
    if not dated:
        if len(matches) != 1:
            raise StoredDataError("remediation finding is ambiguous")
        return matches[0]
    return max(dated, key=lambda item: (str(item["evaluated_at"]), str(item.get("assessment_id"))))


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
    decided_by = item.get("decided_by")
    observed_satisfied = item.get("observed_satisfied")
    observed_total = item.get("observed_total")
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
            decided_by=(DecisionSource.MODEL if decided_by is None else DecisionSource(decided_by)),
            observed_satisfied=(
                None
                if observed_satisfied is None
                else int(_number(observed_satisfied, "observed_satisfied"))
            ),
            observed_total=(
                None if observed_total is None else int(_number(observed_total, "observed_total"))
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
