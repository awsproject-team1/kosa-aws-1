"""승인된 정책 Source·게시 Profile·후보 추출의 원자적 DynamoDB write와 승인 read 어댑터."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from apps.backend.repositories.dynamodb_values import marshal_item
from apps.backend.repositories.errors import RepositoryError
from packages.contracts import (
    AuditEventType,
    NormalizedPolicyDocument,
    PolicyCandidateExtraction,
    PolicyProfile,
    PolicyRule,
    PolicyRuleReference,
    PolicySource,
    PolicySourceApproval,
    PolicySourceKind,
    RuleCandidate,
    RuleLifecycle,
    RuleSeverity,
    SourceReference,
)
from packages.contracts.assessments import AssessmentPhase


class DynamoTransactionClient(Protocol):
    def transact_write_items(self, **kwargs: object) -> object: ...


class DynamoReadTable(Protocol):
    def get_item(self, **kwargs: object) -> Mapping[str, object]: ...
    def query(self, **kwargs: object) -> Mapping[str, object]: ...


class DynamoDbPolicyApprovalRepository:
    """정책 승인·게시의 write와 read를 한 DynamoDB partition에서 담당한다.

    write(승인 record·Profile·후보 추출)는 low-level `transaction_client`로 조건부
    transaction을 쓰고, read(`load_review`/`load_publication`)는 자동 un/marshal되는 resource
    `table`로 읽는다. 후보 추출 자체는 C가 만들고(`PolicyCandidateExtraction`), 이 리포지토리는
    그것을 저장·조회만 한다. 공개 API service는 정책 원문·S3 key·호출자 customer id를 넘기지 않는다.
    """

    def __init__(
        self,
        *,
        table_name: str,
        transaction_client: DynamoTransactionClient,
        table: DynamoReadTable | None = None,
        now: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("table_name must be a non-empty string")
        if transaction_client is None:
            raise TypeError("transaction_client is required")
        self._table_name = table_name
        self._transaction_client = transaction_client
        self._table = table
        self._now = now or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: str(uuid4()))

    def record_approval(
        self,
        *,
        customer_id: str,
        approval: PolicySourceApproval,
        candidates: tuple[RuleCandidate, ...],
    ) -> None:
        _non_empty(customer_id, "customer_id")
        if not isinstance(approval, PolicySourceApproval) or not all(
            isinstance(candidate, RuleCandidate) for candidate in candidates
        ):
            raise TypeError("approval and candidates are required")
        occurred_at, event_id = self._now_iso(), self._new_id("audit")
        pk = f"CUSTOMER#{customer_id}"
        approval_item = {
            "PK": pk,
            "SK": f"POLICY_SOURCE#{approval.source_id}#VERSION#{approval.source_version}#APPROVAL",
            "entity_type": "POLICY_SOURCE_APPROVAL",
            "customer_id": customer_id,
            "occurred_at": occurred_at,
            "version": 1,
            **approval.to_dict(),
        }
        audit_item = {
            "PK": pk,
            "SK": f"AUDIT#{occurred_at}#{event_id}",
            "entity_type": "AUDIT_EVENT",
            "customer_id": customer_id,
            "event_id": event_id,
            "occurred_at": occurred_at,
            "version": 1,
            "event_type": AuditEventType.POLICY_SOURCE_APPROVED.value,
            "source_id": approval.source_id,
            "source_version": approval.source_version,
            "approved_by": approval.approved_by,
        }
        condition = {
            "ConditionCheck": {
                "TableName": self._table_name,
                "Key": marshal_item(
                    {
                        "PK": pk,
                        "SK": f"POLICY_INGESTION#{approval.source_id}#VERSION#{approval.source_version}",
                    }
                ),
                "ConditionExpression": (
                    "customer_id = :customer AND #status = :ready AND artifact_id = :artifact "
                    "AND s3_version_id = :s3_version AND content_sha256 = :digest"
                ),
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": marshal_item(
                    {
                        ":customer": customer_id,
                        ":ready": "READY",
                        ":artifact": approval.artifact_id,
                        ":s3_version": approval.s3_version_id,
                        ":digest": approval.content_sha256,
                    }
                ),
            }
        }
        self._write([condition, self._put(approval_item), self._put(audit_item)], "policy approval")

    def record_profile(
        self,
        *,
        customer_id: str,
        profile: PolicyProfile,
        published_by: str,
        published_at: str,
    ) -> None:
        _non_empty(customer_id, "customer_id")
        _non_empty(published_by, "published_by")
        _non_empty(published_at, "published_at")
        if not isinstance(profile, PolicyProfile):
            raise TypeError("profile must be a PolicyProfile")
        occurred_at, event_id = self._now_iso(), self._new_id("audit")
        pk = f"CUSTOMER#{customer_id}"
        profile_item = {
            "PK": pk,
            "SK": f"POLICY_PROFILE#{profile.policy_profile_id}#VERSION#{profile.version}",
            "entity_type": "POLICY_PROFILE",
            "customer_id": customer_id,
            "published_by": published_by,
            "published_at": published_at,
            "version": 1,
            **profile.to_dict(),
        }
        audit_item = {
            "PK": pk,
            "SK": f"AUDIT#{occurred_at}#{event_id}",
            "entity_type": "AUDIT_EVENT",
            "customer_id": customer_id,
            "event_id": event_id,
            "occurred_at": occurred_at,
            "version": 1,
            "event_type": AuditEventType.POLICY_PROFILE_PUBLISHED.value,
            "policy_profile_id": profile.policy_profile_id,
            "policy_profile_version": profile.version,
            "published_by": published_by,
        }
        self._write([self._put(profile_item), self._put(audit_item)], "policy profile")

    def record_candidate_extraction(
        self, *, customer_id: str, extraction: PolicyCandidateExtraction
    ) -> None:
        """C의 후보 추출 결과를 승인·게시 read 경로가 읽을 형태로 저장한다.

        추출은 `READY` 정규화 문서에만 붙는다(추측 저장 방지). 두 item을 조건부로 함께 쓴다.
        - `POLICY_SOURCE#{sid}#VERSION#{ver}#CANDIDATES`: 후보 규칙 전체(`load_review`가 읽음).
        - `POLICY_SOURCE#{sid}#VERSION#{ver}`: `PolicySource`(`load_publication`이 반환·대조).
        `PolicySource`의 artifact 바인딩은 문서에서 그대로 유도하므로 승인 record의 바인딩과
        어긋날 수 없다. `title`은 업로드 파일명을, `kind`는 고객 업로드 정책의 `INTERNAL_POLICY`를
        쓴다 — 게시 시점의 `ORIGINAL_BINDING_MISMATCH` 검사는 artifact 바인딩만 대조한다.
        """
        _non_empty(customer_id, "customer_id")
        if not isinstance(extraction, PolicyCandidateExtraction):
            raise TypeError("extraction must be a PolicyCandidateExtraction")
        document = extraction.document
        pk = f"CUSTOMER#{customer_id}"
        version_sk = f"POLICY_SOURCE#{document.source_id}#VERSION#{document.source_version}"
        candidates_item = {
            "PK": pk,
            "SK": f"{version_sk}#CANDIDATES",
            "entity_type": "POLICY_CANDIDATE_EXTRACTION",
            "customer_id": customer_id,
            "source_id": document.source_id,
            "source_version": document.source_version,
            "version": 1,
            **extraction.to_dict(),
        }
        source_item = {
            "PK": pk,
            "SK": version_sk,
            "entity_type": "POLICY_SOURCE",
            "customer_id": customer_id,
            "version": 1,
            "source_id": document.source_id,
            "kind": PolicySourceKind.INTERNAL_POLICY.value,
            "title": document.filename,
            "policy_source_version": document.source_version,
            "artifact_id": document.artifact_id,
            "content_sha256": document.content_sha256,
        }
        condition = {
            "ConditionCheck": {
                "TableName": self._table_name,
                "Key": marshal_item(
                    {
                        "PK": pk,
                        "SK": (
                            f"POLICY_INGESTION#{document.source_id}"
                            f"#VERSION#{document.source_version}"
                        ),
                    }
                ),
                "ConditionExpression": (
                    "customer_id = :customer AND #status = :ready AND artifact_id = :artifact "
                    "AND s3_version_id = :s3_version AND content_sha256 = :digest"
                ),
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": marshal_item(
                    {
                        ":customer": customer_id,
                        ":ready": "READY",
                        ":artifact": document.artifact_id,
                        ":s3_version": document.s3_version_id,
                        ":digest": document.content_sha256,
                    }
                ),
            }
        }
        self._write(
            [condition, self._put(candidates_item), self._put(source_item)],
            "policy candidate extraction",
        )

    def load_review(
        self, *, customer_id: str, source_id: str, source_version: str
    ) -> tuple[NormalizedPolicyDocument, tuple[RuleCandidate, ...]]:
        """승인 판정 입력 — READY 정규화 문서와 그 판본을 인용하는 미결정 후보들.

        문서는 `POLICY_INGESTION` item에서, 후보는 `#CANDIDATES` item에서 읽어 `approve_source()`
        에 넘긴다. 후보는 CANDIDATE 상태 그대로 반환하며 승인 판정은 순수 함수가 한다.
        """
        # 지연 import: policy_ingestion은 api 계층을 참조하므로 모듈 로드 시점에 끌어오면
        # repositories 패키지 초기화와 순환한다. 런타임 호출 시점엔 모든 모듈이 초기화돼 있다.
        from apps.backend.repositories.policy_ingestion import document_from_item

        _non_empty(customer_id, "customer_id")
        ingestion_item = self._read_item(
            customer_id, f"POLICY_INGESTION#{source_id}#VERSION#{source_version}"
        )
        try:
            document = document_from_item(ingestion_item)
        except RuntimeError:
            raise RepositoryError("policy ingestion record is invalid") from None
        candidates_item = self._read_item(
            customer_id, f"POLICY_SOURCE#{source_id}#VERSION#{source_version}#CANDIDATES"
        )
        candidates = _candidates_from_item(candidates_item)
        return document, candidates

    def load_publication(
        self, *, customer_id: str, source_id: str, source_version: str
    ) -> tuple[
        tuple[RuleCandidate, ...], tuple[PolicySourceApproval, ...], tuple[PolicySource, ...]
    ]:
        """게시 판정 입력 — 승인된 후보, 승인 record, 게시된 Source.

        후보 규칙 전체는 `#CANDIDATES` item에 있고, 어떤 Rule version이 승인됐는지는 승인 record의
        `approved_rules`에 있다. 둘을 조합해 승인된 후보만 APPROVED 상태로 반환하므로,
        `publish_profile()`의 `RULE_NOT_APPROVED` 게이트가 미승인 후보를 그대로 거른다.
        """
        _non_empty(customer_id, "customer_id")
        candidates_item = self._read_item(
            customer_id, f"POLICY_SOURCE#{source_id}#VERSION#{source_version}#CANDIDATES"
        )
        approval_item = self._read_item(
            customer_id, f"POLICY_SOURCE#{source_id}#VERSION#{source_version}#APPROVAL"
        )
        source_item = self._read_item(
            customer_id, f"POLICY_SOURCE#{source_id}#VERSION#{source_version}"
        )
        approval = _approval_from_item(approval_item)
        source = _source_from_item(source_item)
        approved_keys = {
            (reference.rule_id, reference.version) for reference in approval.approved_rules
        }
        approved = tuple(
            candidate.approved()
            for candidate in _candidates_from_item(candidates_item)
            if (candidate.rule.rule_id, candidate.rule.version) in approved_keys
        )
        return approved, (approval,), (source,)

    def _read_item(self, customer_id: str, sk: str) -> Mapping[str, object]:
        if self._table is None:
            raise RepositoryError("a read table is required to load policy approval state")
        try:
            item = self._table.get_item(
                Key={"PK": f"CUSTOMER#{customer_id}", "SK": sk}, ConsistentRead=True
            ).get("Item")
        except Exception:
            raise RepositoryError("policy approval state read failed") from None
        if not isinstance(item, Mapping) or item.get("customer_id") != customer_id:
            raise RepositoryError("policy approval state not found")
        return item

    def _put(self, item: dict[str, object]) -> dict[str, object]:
        return {
            "Put": {
                "TableName": self._table_name,
                "Item": marshal_item(item),
                "ConditionExpression": "attribute_not_exists(PK) AND attribute_not_exists(SK)",
            }
        }

    def _write(self, items: list[dict[str, object]], label: str) -> None:
        try:
            self._transaction_client.transact_write_items(TransactItems=items)
        except Exception as error:
            if _error_code(error) in {
                "ConditionalCheckFailedException",
                "TransactionCanceledException",
            }:
                raise RepositoryError(f"{label} already exists or binding is stale") from None
            raise RepositoryError(f"{label} write failed") from None

    def _now_iso(self) -> str:
        return self._now().astimezone(UTC).isoformat().replace("+00:00", "Z")

    def _new_id(self, prefix: str) -> str:
        value = self._id_factory()
        _non_empty(value, "generated identifier")
        return f"{prefix}-{value}"


def _candidates_from_item(item: Mapping[str, object]) -> tuple[RuleCandidate, ...]:
    """`#CANDIDATES` item의 `candidates`를 `RuleCandidate` 튜플로 되돌린다.

    저장 형식은 `PolicyCandidateExtraction.to_dict()`이므로 후보는 CANDIDATE lifecycle로
    복원된다. 승인 여부는 `load_publication`에서 승인 record와 조합해 결정한다.
    """
    raw = item.get("candidates")
    if not isinstance(raw, list):
        raise RepositoryError("policy candidate item is invalid")
    try:
        return tuple(_candidate_from_dict(entry) for entry in raw)
    except (TypeError, ValueError, KeyError):
        raise RepositoryError("policy candidate item is invalid") from None


def _candidate_from_dict(entry: object) -> RuleCandidate:
    if not isinstance(entry, Mapping):
        raise TypeError("candidate entry must be a mapping")
    lifecycle = RuleLifecycle(_require_str(entry, "lifecycle"))
    rule_raw = entry.get("rule")
    if not isinstance(rule_raw, Mapping):
        raise TypeError("candidate rule must be a mapping")
    references = rule_raw.get("source_references")
    if not isinstance(references, list):
        raise TypeError("rule source_references must be a list")
    rule = PolicyRule(
        rule_id=_require_str(rule_raw, "rule_id"),
        version=_require_str(rule_raw, "version"),
        title=_require_str(rule_raw, "title"),
        severity=RuleSeverity(_require_str(rule_raw, "severity")),
        applicable_phases=tuple(
            AssessmentPhase(value) for value in _require_str_list(rule_raw, "applicable_phases")
        ),
        resource_types=tuple(_require_str_list(rule_raw, "resource_types")),
        source_references=tuple(_source_reference_from_dict(ref) for ref in references),
    )
    return RuleCandidate(rule=rule, lifecycle=lifecycle)


def _source_reference_from_dict(entry: object) -> SourceReference:
    if not isinstance(entry, Mapping):
        raise TypeError("source reference must be a mapping")
    return SourceReference(
        source_id=_require_str(entry, "source_id"),
        source_version=_require_str(entry, "source_version"),
        locator=_require_str(entry, "locator"),
        content_sha256=_require_str(entry, "content_sha256"),
    )


def _approval_from_item(item: Mapping[str, object]) -> PolicySourceApproval:
    """`#APPROVAL` item(`PolicySourceApproval.to_dict()` 형식)을 되돌린다."""
    references = item.get("approved_rules")
    if not isinstance(references, list):
        raise RepositoryError("policy approval item is invalid")
    try:
        return PolicySourceApproval(
            source_id=_require_str(item, "source_id"),
            source_version=_require_str(item, "source_version"),
            artifact_id=_require_str(item, "artifact_id"),
            s3_version_id=_require_str(item, "s3_version_id"),
            content_sha256=_require_str(item, "content_sha256"),
            normalized_artifact_id=_require_str(item, "normalized_artifact_id"),
            normalized_sha256=_require_str(item, "normalized_sha256"),
            approved_rules=tuple(
                PolicyRuleReference(
                    rule_id=_require_str(ref, "rule_id"), version=_require_str(ref, "version")
                )
                for ref in references
                if isinstance(ref, Mapping)
            ),
            approved_by=_require_str(item, "approved_by"),
            approved_at=_require_str(item, "approved_at"),
        )
    except (TypeError, ValueError, KeyError):
        raise RepositoryError("policy approval item is invalid") from None


def _source_from_item(item: Mapping[str, object]) -> PolicySource:
    """`POLICY_SOURCE` item을 `PolicySource`로 되돌린다.

    `version`은 write 시 `policy_source_version`으로 저장한다 — item에는 이미 스키마 `version`
    (schema revision 정수)이 있어 이름이 겹치기 때문이다.
    """
    try:
        return PolicySource(
            source_id=_require_str(item, "source_id"),
            kind=PolicySourceKind(_require_str(item, "kind")),
            title=_require_str(item, "title"),
            version=_require_str(item, "policy_source_version"),
            artifact_id=_require_str(item, "artifact_id"),
            content_sha256=_require_str(item, "content_sha256"),
        )
    except (TypeError, ValueError, KeyError):
        raise RepositoryError("policy source item is invalid") from None


def _require_str(item: Mapping[str, object], name: str) -> str:
    value = item.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is invalid")
    return value


def _require_str_list(item: Mapping[str, object], name: str) -> list[str]:
    value = item.get(name)
    if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
        raise ValueError(f"{name} is invalid")
    return value


def _non_empty(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _error_code(error: BaseException) -> str | None:
    response = getattr(error, "response", None)
    details = response.get("Error") if isinstance(response, dict) else None
    code = details.get("Code") if isinstance(details, dict) else None
    return code if isinstance(code, str) else None
