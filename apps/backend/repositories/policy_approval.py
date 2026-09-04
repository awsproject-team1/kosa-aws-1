"""승인된 정책 Source·게시 Profile·후보 추출의 원자적 DynamoDB write와 승인 read 어댑터."""

import logging
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from apps.backend.policy.ingestion import ProfileBaseline
from apps.backend.policy.serialization import profile_from_dict, rule_from_dict
from apps.backend.repositories.dynamodb_values import marshal_item
from apps.backend.repositories.errors import RepositoryError
from packages.common.errors import (
    ApprovalConflictError,
    AuthoringRunNotFound,
    PolicyProfileNotFound,
)
from packages.contracts import (
    ApprovalRejectionCode,
    ArtifactReadFailureCode,
    AuditEventType,
    AuthoringManifest,
    AuthoringProvenance,
    AuthoringRunStatus,
    NormalizedPolicyDocument,
    PolicyAuthoringRequest,
    PolicyAuthoringResult,
    PolicyCandidateExtraction,
    PolicyProfile,
    PolicyRuleReference,
    PolicySource,
    PolicySourceApproval,
    PolicySourceKind,
    RuleCandidate,
    RuleLifecycle,
)

_LOGGER = logging.getLogger("governance.approval")

#: 한 authoring 실행이 만들 수 있는 결과 item 수의 상한. 상한이 없으면 문서 하나가 고객
#: partition을 채우고, 그 상태에서 write가 실패하면 어디까지 저장됐는지 말할 수 없다.
MAX_AUTHORING_RESULTS_PER_RUN = 200

#: 한 승인 transaction이 기록할 수 있는 Rule 수. DynamoDB transaction item 상한(100)에서
#: 조건 검사·승인 record·audit event 자리를 남긴 값이다. 넘으면 승인이 원자적이지 않게 된다.
MAX_RULES_PER_APPROVAL = 90

#: 결과 item의 SK segment. Review는 CANDIDATE만 승인 대상으로 읽고, 나머지 둘은 보존용이다.
AUTHORING_CANDIDATE_SEGMENT = "CANDIDATE"
AUTHORING_RESULT_SEGMENTS = (AUTHORING_CANDIDATE_SEGMENT, "UNSUPPORTED", "REJECTED")

#: 재시도마다 서버가 새로 만드는 write 시각. 멱등 판정에서 제외한다 — 포함하면 모든 재시도가
#: "다른 내용"으로 보인다. 승인자가 정한 `approved_at`은 여기 없고 그대로 비교된다.
_WRITE_TIME_FIELDS = frozenset({"occurred_at", "recorded_at"})

#: query 페이지 수 상한. 끝나지 않는 페이지네이션을 무한 루프 대신 실패로 만든다.
_MAX_QUERY_PAGES = 50


class ProfileConcurrentlyUpdatedError(RepositoryError):
    """Raised when another publication moved the current Profile pointer first.

    조용히 덮어쓰지 않는다. 덮어쓰면 두 게시자가 각자 자기 Profile이 현재 판본이라고 믿는다.
    """


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
        """Write the approval, the audit event, **and the approved Rule items**, atomically.

        승인은 Rule Registry에 기록되지 않으면 Runtime에서 존재하지 않는 것과 같다. 승인 record만
        쓰고 Rule item을 나중에 쓰면, 그 사이에 게시된 Profile이 참조하는 Rule을 Catalog가 찾지
        못한다. 그래서 같은 transaction에 넣는다.

        `candidates`는 사람이 고른 부분집합이며 전부 APPROVED여야 한다. 미승인 후보가 섞이면
        검토 게이트를 통과하지 않은 Rule이 Registry에 들어간다.

        **승인은 더해진다.** 같은 판본에 이미 승인 record가 있으면 새 승인은 그 record에 Rule을
        보태고, 이미 승인된 Rule은 그대로 남는다. 예전에는 두 번째 승인이 첫 번째와 다르면
        조건부 write가 실패해 503으로 새어 나갔다 — 한 문서에서 두 번째 Profile을 만드는 순간
        게시가 막혔다. 빼는 것은 허용하지 않는다: 이미 승인된 Rule은 게시된 Profile이 인용한다.
        원본 binding이 다르면 같은 판본이 아니므로 거부한다.
        """
        _non_empty(customer_id, "customer_id")
        if not isinstance(approval, PolicySourceApproval) or not all(
            isinstance(candidate, RuleCandidate) for candidate in candidates
        ):
            raise TypeError("approval and candidates are required")
        if len(candidates) > MAX_RULES_PER_APPROVAL:
            # DynamoDB transaction의 item 상한 안에 머문다. 넘으면 승인이 원자적이지 않게 된다.
            raise RepositoryError(
                f"one approval must not record more than {MAX_RULES_PER_APPROVAL} rules"
            )
        stored = self._stored_approval(customer_id, approval)
        if stored is not None:
            already = {(r.rule_id, r.version) for r in stored.approved_rules}
            added = tuple(
                reference
                for reference in approval.approved_rules
                if (reference.rule_id, reference.version) not in already
            )
            if not added:
                # 보탤 것이 없다. 같은 승인의 재시도이거나 이미 승인된 부분집합이다. 그래도 저장된
                # Rule item이 지금 승인하려는 내용과 같은지는 확인한다 — 같은 key에 다른 내용이
                # 있다면 승인된 Rule이 바뀐 것이고, 그것을 "성공"으로 답하면 사실이 사라진다.
                if self._table is not None and not all(
                    self._stored_matches(
                        customer_id,
                        _rule_item(customer_id, candidate, "unused"),
                        ignoring=_WRITE_TIME_FIELDS | {"recorded_at"},
                    )
                    for candidate in candidates
                ):
                    raise RepositoryError("an approved rule differs from the stored registry item")
                return
            approval = replace(approval, approved_rules=(*stored.approved_rules, *added))
            candidates = tuple(
                candidate
                for candidate in candidates
                if (candidate.rule.rule_id, candidate.rule.version) not in already
            )
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
        rule_items = [_rule_item(customer_id, candidate, occurred_at) for candidate in candidates]
        approval_entry = (
            self._put(approval_item)
            if stored is None
            # 이미 있는 record를 보탠 record로 바꾼다. 그 사이에 누가 먼저 보탰으면 조건이
            # 실패하고 호출자가 다시 읽는다 — 두 승인이 서로를 덮어쓰지 않게 한다.
            else self._replace(approval_item, "approved_at = :seen", {":seen": stored.approved_at})
        )
        entries = [
            condition,
            *self._authoring_condition(customer_id, approval),
            *(self._put(item) for item in rule_items),
            approval_entry,
            self._put(audit_item),
        ]
        try:
            self._write(entries, "policy approval")
        except RepositoryError:
            # 승인 API는 at-least-once로 재시도될 수 있다. 이미 저장된 항목이 지금 쓰려는 것과
            # 같은 내용이면 재시도로 흡수하고, 같은 Rule key에 **다른 내용**이 있으면 승인된
            # Rule이 조용히 바뀌는 것이므로 fail-closed한다.
            #
            # 비교에서 write 시각은 제외한다. `occurred_at`/`approved_at`은 시도마다 서버가 새로
            # 만드는 값이라, 포함하면 **모든** 재시도가 "다른 내용"으로 보인다 — 재시도를
            # 흡수하는 경로가 사실상 존재하지 않게 된다. 승인의 정체성은 승인자가 정한
            # `approved_at`과 승인된 Rule 집합이며 그 둘은 그대로 비교된다.
            if self._table is None or not all(
                self._stored_matches(customer_id, item, ignoring=_WRITE_TIME_FIELDS)
                for item in (*rule_items, approval_item)
            ):
                raise

    def _stored_approval(
        self, customer_id: str, approval: PolicySourceApproval
    ) -> PolicySourceApproval | None:
        """The approval already recorded for this exact source version, if any.

        같은 판본인지는 원본 binding으로 판정한다. binding이 다른 승인은 다른 문서에 대한
        승인이므로 보탤 수 없다.
        """
        if self._table is None:
            return None
        item = self._find_item(
            customer_id,
            f"POLICY_SOURCE#{approval.source_id}#VERSION#{approval.source_version}#APPROVAL",
        )
        if item is None:
            return None
        stored = _approval_from_item(item)
        same_binding = (
            stored.original_binding == approval.original_binding
            and stored.normalized_artifact_id == approval.normalized_artifact_id
            and stored.normalized_sha256 == approval.normalized_sha256
        )
        if not same_binding:
            raise ApprovalConflictError(
                "this source version already carries an approval bound to a different original"
            )
        return stored

    def retire_profile(self, *, customer_id: str, policy_profile_id: str, retired_by: str) -> None:
        """Remove a Profile's current pointer so nothing new can select it.

        판본 item(`#VERSION#`)은 남긴다. Assessment가 그 판본을 고정해 두었고 보고서가 그것을
        읽어 준비도를 원본별로 나누므로, 지우면 이미 만들어진 보고서가 깨진다. 사라지는 것은
        "현재 Profile"이라는 자리뿐이다 — 목록에서 빠지고, 사용자에게 지정할 수 없게 되고,
        문서 삭제를 막던 참조가 풀린다.
        """
        _non_empty(customer_id, "customer_id")
        _non_empty(policy_profile_id, "policy_profile_id")
        _non_empty(retired_by, "retired_by")
        pk = f"CUSTOMER#{customer_id}"
        pointer_sk = f"POLICY_PROFILE#{policy_profile_id}"
        pointer = self._find_item(customer_id, pointer_sk)
        if pointer is None:
            raise PolicyProfileNotFound("policy profile not found")
        occurred_at, event_id = self._now_iso(), self._new_id("audit")
        audit_item = {
            "PK": pk,
            "SK": f"AUDIT#{occurred_at}#{event_id}",
            "entity_type": "AUDIT_EVENT",
            "customer_id": customer_id,
            "event_id": event_id,
            "occurred_at": occurred_at,
            "version": 1,
            "event_type": AuditEventType.POLICY_PROFILE_RETIRED.value,
            "policy_profile_id": policy_profile_id,
            "policy_profile_version": pointer.get("current_version"),
            "retired_by": retired_by,
        }
        self._write(
            [
                {
                    "Delete": {
                        "TableName": self._table_name,
                        "Key": marshal_item({"PK": pk, "SK": pointer_sk}),
                        "ConditionExpression": "attribute_exists(SK) AND customer_id = :customer",
                        "ExpressionAttributeValues": marshal_item({":customer": customer_id}),
                    }
                },
                self._put(audit_item),
            ],
            "policy profile retirement",
        )

    def _replace(
        self, item: dict[str, object], condition: str, values: dict[str, object]
    ) -> dict[str, object]:
        """Overwrite one item only while the stored one still looks as the caller last read it."""
        return {
            "Put": {
                "TableName": self._table_name,
                "Item": marshal_item(item),
                "ConditionExpression": f"attribute_exists(SK) AND {condition}",
                "ExpressionAttributeValues": marshal_item(values),
            }
        }

    def _authoring_condition(
        self, customer_id: str, approval: PolicySourceApproval
    ) -> list[dict[str, object]]:
        """Require a READY authoring manifest when one exists for this source version.

        authoring worker가 만든 후보는 manifest가 완결을 선언한 뒤에만 승인될 수 있다. 승인
        transaction 안에서 다시 확인해, 읽은 시점과 쓰는 시점 사이에 manifest가 바뀌는 경우를
        막는다. manifest가 없는 판본(이전 경로로 저장된 추출)에는 이 조건을 걸 것이 없다.
        """
        if self._table is None:
            return []
        sort_key = f"POLICY_SOURCE#{approval.source_id}#VERSION#{approval.source_version}#AUTHORING"
        try:
            self._read_item(customer_id, sort_key)
        except RepositoryError:
            return []
        return [
            {
                "ConditionCheck": {
                    "TableName": self._table_name,
                    "Key": marshal_item({"PK": f"CUSTOMER#{customer_id}", "SK": sort_key}),
                    "ConditionExpression": "customer_id = :customer AND #status = :ready",
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": marshal_item(
                        {
                            ":customer": customer_id,
                            ":ready": AuthoringRunStatus.READY.value,
                        }
                    ),
                }
            }
        ]

    def record_profile(
        self,
        *,
        customer_id: str,
        profile: PolicyProfile,
        published_by: str,
        published_at: str,
        expected_current_version: str | None = None,
    ) -> None:
        """Publish one Profile version and move the current pointer to it, atomically.

        두 item을 쓴다.

        - `POLICY_PROFILE#{id}#VERSION#{version}` — immutable 판본 이력. Assessment가 고정한
          version을 나중에 직접 읽을 수 있어야 하므로 절대 덮어쓰지 않는다.
        - `POLICY_PROFILE#{id}` — current pointer. 새 Assessment가 어떤 판본을 고를지 정한다.

        pointer 교체는 **낙관적 동시성**으로 보호한다. `expected_current_version`이 `None`이면
        최초 게시이므로 pointer가 없어야 하고, 값이 있으면 그 version과 일치해야 한다. 이 조건이
        없으면 동시에 게시된 두 Profile 중 나중에 도착한 것이 앞의 것을 조용히 덮어쓰고, 게시자는
        자기 Profile이 현재 판본이라고 믿는다.
        """
        _non_empty(customer_id, "customer_id")
        _non_empty(published_by, "published_by")
        _non_empty(published_at, "published_at")
        if not isinstance(profile, PolicyProfile):
            raise TypeError("profile must be a PolicyProfile")
        if expected_current_version is not None:
            _non_empty(expected_current_version, "expected_current_version")
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
        pointer_item = {
            **profile_item,
            "SK": f"POLICY_PROFILE#{profile.policy_profile_id}",
            "current_version": profile.version,
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
        entries = [
            self._put(profile_item),
            self._pointer_put(pointer_item, expected_current_version),
            self._put(audit_item),
        ]
        try:
            self._write(entries, "policy profile")
        except RepositoryError as error:
            # 조건 실패는 두 가지다. 같은 내용의 재시도이거나, 경쟁 게시다. 저장된 pointer가
            # 지금 게시하려는 판본을 이미 가리키면 재시도로 흡수하고, 아니면 경쟁 게시로 보고
            # 열거된 사유로 알린다 — 게시자가 자기 Profile이 현재 판본이라고 잘못 믿지 않게 한다.
            if self._table is not None and self._stored_matches(customer_id, profile_item):
                stored_pointer = self._stored_pointer(customer_id, profile.policy_profile_id)
                if stored_pointer == profile.version:
                    return
            raise ProfileConcurrentlyUpdatedError(
                ApprovalRejectionCode.PROFILE_CONCURRENTLY_UPDATED.value
            ) from error

    def _pointer_put(
        self, item: dict[str, object], expected_current_version: str | None
    ) -> dict[str, object]:
        if expected_current_version is None:
            return self._put(item)
        return {
            "Put": {
                "TableName": self._table_name,
                "Item": marshal_item(item),
                "ConditionExpression": "current_version = :expected",
                "ExpressionAttributeValues": marshal_item({":expected": expected_current_version}),
            }
        }

    def _stored_pointer(self, customer_id: str, policy_profile_id: str) -> str | None:
        try:
            item = self._read_item(customer_id, f"POLICY_PROFILE#{policy_profile_id}")
        except RepositoryError:
            return None
        value = item.get("current_version")
        return value if isinstance(value, str) else None

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
        source_item = _source_item(customer_id, document, version_sk)
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
        try:
            self._write(
                [condition, self._put(candidates_item), self._put(source_item)],
                "policy candidate extraction",
            )
        except RepositoryError:
            # C의 추출 Worker는 at-least-once로 같은 결과를 재전송할 수 있다. 이미 저장된 두 item이
            # 지금 쓰려는 것과 같은 내용이면 재시도로 보고 흡수하고, 다르면 immutability를 지켜
            # fail-closed한다(`DynamoDbPolicyCatalogBootstrap`과 같은 관용구). read table이 없으면
            # 같은 내용인지 확인할 수 없으므로 원래 오류를 그대로 올린다.
            if self._table is None or not (
                self._stored_matches(customer_id, candidates_item)
                and self._stored_matches(customer_id, source_item)
            ):
                raise
        return None

    def record_authoring_result(
        self, *, customer_id: str, result: PolicyAuthoringResult
    ) -> AuthoringManifest:
        """Persist one authoring run as a manifest plus one item per outcome.

        후보 전체를 한 item에 담지 않는다. 한 문서가 만드는 후보 수는 문서에 달려 있고, 단일
        item은 DynamoDB item 크기 상한에 걸리는 순간 **저장 자체가 실패**한다 — 그때 실패하는
        것은 후보 하나가 아니라 그 문서의 추출 전부다.

        나눠 쓰면 "일부만 써진 상태"가 생기므로 manifest가 그 경계를 담당한다.

            PROCESSING manifest
            → child item 멱등 write
            → count/digest 검증
            → manifest READY 전환

        Review와 Approval은 READY manifest만 읽는다. 같은 source version을 다른
        extractor·prompt·Catalog로 재추출하면 identity가 달라지므로 재시도가 아니라 다른
        추출로 보아 fail-closed한다.
        """
        _non_empty(customer_id, "customer_id")
        if not isinstance(result, PolicyAuthoringResult):
            raise TypeError("result must be a PolicyAuthoringResult")
        total = sum(result.counts.values())
        if total > MAX_AUTHORING_RESULTS_PER_RUN:
            raise RepositoryError(
                f"an authoring run must not produce more than "
                f"{MAX_AUTHORING_RESULTS_PER_RUN} results"
            )

        document = result.document
        prefix = f"POLICY_SOURCE#{document.source_id}#VERSION#{document.source_version}"
        processing = _manifest_for(result, AuthoringRunStatus.PROCESSING)
        self._put_manifest(customer_id, processing, prefix)

        children = _authoring_child_items(customer_id, result, prefix)
        for item in children:
            self._put_idempotent(customer_id, item, "policy authoring result")

        stored = self._read_children(customer_id, prefix)
        if stored != {str(item["SK"]): item for item in children}:
            # 개수만 세면 "다른 후보가 같은 개수만큼 써진" 경우를 통과시킨다.
            raise RepositoryError("stored authoring results do not match the run that wrote them")

        ready = _manifest_for(result, AuthoringRunStatus.READY)
        self._put_manifest(customer_id, ready, prefix)
        self._put_idempotent(
            customer_id, _source_item(customer_id, document, prefix), "policy source"
        )
        return ready

    def request_extraction(
        self,
        *,
        customer_id: str,
        source_id: str,
        source_version: str,
        authoring_run_id: str,
        requested_at: str,
    ) -> PolicyAuthoringRequest:
        """Record one extraction request durably before anything is queued.

        요청을 먼저 남기는 이유는 재시도다. queue publish가 실패해도 요청은 남으므로 사람이
        다시 누르지 않아도 sweeper나 재요청이 같은 실행을 이어받을 수 있다.

        **이미 요청된 판본을 다시 요청하면 원래 요청을 그대로 돌려준다.** 새 `authoring_run_id`와
        새 `requested_at`을 발급하면 같은 문서에 대한 실행이 둘이 되고, 그 둘의 provenance가
        달라 저장 계층이 서로를 다른 추출로 본다.
        """
        for name, value in (
            ("customer_id", customer_id),
            ("source_id", source_id),
            ("source_version", source_version),
            ("authoring_run_id", authoring_run_id),
            ("requested_at", requested_at),
        ):
            _non_empty(value, name)

        prefix = f"POLICY_SOURCE#{source_id}#VERSION#{source_version}"
        try:
            existing = self._read_item(customer_id, f"{prefix}#REQUEST")
        except RepositoryError:
            existing = None
        if existing is not None:
            return _request_from_item(existing)

        request = PolicyAuthoringRequest(
            customer_id=customer_id,
            source_id=source_id,
            source_version=source_version,
            authoring_run_id=authoring_run_id,
            requested_at=requested_at,
        )
        item = {
            "PK": f"CUSTOMER#{customer_id}",
            "SK": f"{prefix}#REQUEST",
            "entity_type": "POLICY_AUTHORING_REQUEST",
            "customer_id": customer_id,
            "version": 1,
            **request.to_dict(),
        }
        try:
            self._write([self._put(item)], "policy authoring request")
        except RepositoryError:
            # 동시에 두 요청이 들어오면 하나만 이긴다. 진 쪽은 이긴 요청을 그대로 쓴다.
            stored = self._read_item(customer_id, f"{prefix}#REQUEST")
            return _request_from_item(stored)
        return request

    def load_authoring_manifest(
        self, *, customer_id: str, source_id: str, source_version: str
    ) -> AuthoringManifest:
        """Load the run manifest, or say there is none.

        없는 manifest를 `RepositoryError`로 올리면 API가 503으로 옮긴다. 업로드 직후부터
        worker가 결과를 쓸 때까지 manifest는 존재하지 않으므로, 그 구간 내내 후보 조회가
        "서비스 장애"로 보였다. 없음은 오류가 아니라 상태다.
        """
        _non_empty(customer_id, "customer_id")
        item = self._find_item(
            customer_id, f"POLICY_SOURCE#{source_id}#VERSION#{source_version}#AUTHORING"
        )
        if item is None:
            raise AuthoringRunNotFound("no authoring run for this policy source version")
        return _manifest_from_item(item)

    def has_authoring_request(
        self, *, customer_id: str, source_id: str, source_version: str
    ) -> bool:
        """Whether an extraction was requested for this version (queued or in flight)."""
        _non_empty(customer_id, "customer_id")
        return (
            self._find_item(
                customer_id, f"POLICY_SOURCE#{source_id}#VERSION#{source_version}#REQUEST"
            )
            is not None
        )

    def load_authoring_results(
        self, *, customer_id: str, source_id: str, source_version: str
    ) -> tuple[AuthoringManifest, tuple[Mapping[str, object], ...]]:
        """Return the READY manifest and every stored outcome item, in stored order.

        API의 페이지네이션과 승인 read가 같은 read 경로를 쓰게 한다. 두 경로가 다른 방식으로
        읽으면 하나가 READY 검사를 빠뜨려도 다른 하나의 테스트가 그것을 잡지 못한다.
        """
        manifest = self.load_authoring_manifest(
            customer_id=customer_id, source_id=source_id, source_version=source_version
        )
        if not manifest.is_reviewable:
            raise RepositoryError("the authoring run is not ready for review")
        prefix = f"POLICY_SOURCE#{source_id}#VERSION#{source_version}"
        stored = self._read_children(customer_id, prefix)
        return manifest, tuple(stored[key] for key in sorted(stored))

    def _put_manifest(self, customer_id: str, manifest: AuthoringManifest, prefix: str) -> None:
        """Write the manifest, absorbing a retry of the same run and refusing a different one."""
        item = {
            "PK": f"CUSTOMER#{customer_id}",
            "SK": f"{prefix}#AUTHORING",
            "entity_type": "POLICY_AUTHORING_RUN",
            "customer_id": customer_id,
            "version": 1,
            **manifest.to_dict(),
        }
        try:
            existing = self._read_item(customer_id, str(item["SK"]))
        except RepositoryError:
            self._write([self._put(item)], "policy authoring manifest")
            return
        stored = _manifest_from_item(existing)
        if stored.extraction_identity != manifest.extraction_identity:
            raise RepositoryError(
                "a different extraction already exists for this policy source version"
            )
        if dict(existing) == item:
            return
        if stored.status is AuthoringRunStatus.READY and manifest.status is not (
            AuthoringRunStatus.READY
        ):
            # 이미 완결된 실행을 PROCESSING으로 되돌리지 않는다. 리뷰 중인 후보 집합이
            # 진행 중 상태로 보이면 승인 경로가 그것을 읽지 못한다.
            return
        self._write([self._overwrite(item)], "policy authoring manifest")

    def _put_idempotent(self, customer_id: str, item: dict[str, object], label: str) -> None:
        try:
            self._write([self._put(item)], label)
        except RepositoryError:
            # worker는 at-least-once다. 같은 내용이면 재시도로 흡수하고, 다르면 fail-closed한다.
            if self._table is None or not self._stored_matches(customer_id, item):
                raise

    def _read_children(self, customer_id: str, prefix: str) -> dict[str, Mapping[str, object]]:
        """Read every outcome item written under one authoring run, following pagination."""
        if self._table is None:
            raise RepositoryError("a read table is required to load policy authoring state")
        items: dict[str, Mapping[str, object]] = {}
        for segment in AUTHORING_RESULT_SEGMENTS:
            start_key: object | None = None
            for _ in range(_MAX_QUERY_PAGES):
                request: dict[str, object] = {
                    "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
                    "ExpressionAttributeValues": {
                        ":pk": f"CUSTOMER#{customer_id}",
                        ":prefix": f"{prefix}#{segment}#",
                    },
                    "ConsistentRead": True,
                }
                if start_key is not None:
                    request["ExclusiveStartKey"] = start_key
                try:
                    response = self._table.query(**request)
                except Exception:
                    raise RepositoryError("policy authoring state read failed") from None
                for item in response.get("Items", []):  # type: ignore[union-attr]
                    if not isinstance(item, Mapping) or item.get("customer_id") != customer_id:
                        raise RepositoryError("policy authoring state is invalid")
                    items[str(item["SK"])] = item
                start_key = response.get("LastEvaluatedKey")
                if not start_key:
                    break
            else:
                raise RepositoryError("policy authoring state read did not terminate")
        return items

    def _stored_matches(
        self,
        customer_id: str,
        expected: dict[str, object],
        *,
        ignoring: frozenset[str] = frozenset(),
    ) -> bool:
        """이미 저장된 item이 기대 item과 같은 내용인지 확인한다.

        `ignoring`은 시도마다 서버가 새로 만드는 bookkeeping 필드다. 그 값까지 비교하면 재시도가
        언제나 "다른 내용"으로 보여, 멱등 write 경로가 실질적으로 사라진다.
        """
        try:
            existing = self._read_item(customer_id, str(expected["SK"]))
        except RepositoryError:
            return False
        return {name: value for name, value in existing.items() if name not in ignoring} == {
            name: value for name, value in expected.items() if name not in ignoring
        }

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
        candidates = self._load_candidates(
            customer_id=customer_id, source_id=source_id, source_version=source_version
        )
        return document, candidates

    def _load_candidates(
        self, *, customer_id: str, source_id: str, source_version: str
    ) -> tuple[RuleCandidate, ...]:
        """Read this source version's undecided candidates from whichever store holds them.

        authoring worker가 쓴 실행은 READY manifest와 `#CANDIDATE#` item으로 존재하고, 그 이전
        경로(`record_candidate_extraction`)는 단일 `#CANDIDATES` item으로 존재한다. 승인은 두
        경우 모두 같은 값을 받아야 하므로 read를 여기 하나로 모은다. **manifest가 있으면 그것이
        정본이다** — manifest가 READY가 아니면 일부만 쓰인 후보 집합을 완전한 것으로 읽지 않도록
        실패한다.
        """
        try:
            manifest = self.load_authoring_manifest(
                customer_id=customer_id, source_id=source_id, source_version=source_version
            )
        except (AuthoringRunNotFound, RepositoryError):
            legacy = self._read_item(
                customer_id, f"POLICY_SOURCE#{source_id}#VERSION#{source_version}#CANDIDATES"
            )
            return _candidates_from_item(legacy)

        if not manifest.is_reviewable:
            raise RepositoryError("the authoring run is not ready for review")
        prefix = f"POLICY_SOURCE#{source_id}#VERSION#{source_version}"
        stored = self._read_children(customer_id, prefix)
        candidates = tuple(
            _candidate_from_dict(stored[key]["candidate"])
            for key in sorted(stored)
            if f"#{AUTHORING_CANDIDATE_SEGMENT}#" in key
        )
        if len(candidates) != manifest.counts.get("accepted", 0) + manifest.counts.get("manual", 0):
            raise RepositoryError("stored authoring candidates do not match the manifest counts")
        return candidates

    def load_publication(
        self, *, customer_id: str, source_id: str, source_version: str
    ) -> tuple[
        tuple[RuleCandidate, ...], tuple[PolicySourceApproval, ...], tuple[PolicySource, ...]
    ]:
        """게시 판정 입력 — 승인된 후보, 승인 record, 게시된 Source.

        후보 규칙 전체는 `#CANDIDATES` item에 있고, 어떤 Rule version이 승인됐는지는 승인 record의
        `approved_rules`에 있다. 둘을 조합해 **승인 record에 든 후보만** APPROVED로 표시해 돌려준다.
        게시 입력 집합을 승인 record로 정의하는 것이다 — `publish_profile()`은 넘어온 후보를 전부
        Profile에 넣으므로, 미승인 후보를 APPROVED로 섞으면 게이트가 게시를 거부해 부분 승인 Source가
        영영 게시되지 못한다. `publish_profile()`의 승인 검사는 여기 표시한 lifecycle이 승인 record와
        어긋나지 않는지 재확인하는 이중 방어다.
        """
        _non_empty(customer_id, "customer_id")
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
        # 게시 입력은 "승인된 Rule"이다(`publish_profile`은 넘어온 후보를 전부 Profile에 넣고
        # 미승인이면 거부한다). 그래서 승인 record의 `approved_rules`에 든 후보만 APPROVED로 표시해
        # 돌려준다. 이 필터는 `publish_profile`의 `RULE_NOT_APPROVED` 게이트를 대신하는 게 아니라,
        # 게시할 후보 집합 자체를 승인 record로 정의하는 것이다 — 미승인 후보를 APPROVED로 섞어
        # 게이트에 맡기면, 그 게이트는 (설계상) 게시를 거부하므로 부분 승인 Source는 영영 게시할 수
        # 없게 된다. `publish_profile`의 승인 검사(`is_approved`·`approval.approves`)는 여기서
        # 표시한 lifecycle과 승인 record가 어긋나지 않는지 재확인하는 이중 방어로 남는다.
        approved = tuple(
            candidate.approved()
            for candidate in self._load_candidates(
                customer_id=customer_id, source_id=source_id, source_version=source_version
            )
            if (candidate.rule.rule_id, candidate.rule.version) in approved_keys
        )
        return approved, (approval,), (source,)

    def load_baseline(
        self, *, customer_id: str, policy_profile_id: str, version: str
    ) -> ProfileBaseline:
        """Load an already-published Profile so its Rules can join a new one.

        기준선은 **이 고객 파티션에 이미 게시된 Profile**이어야 한다. 임의의 Rule 목록을 받으면
        승인 게이트를 우회하는 입구가 되므로, 입력은 Profile 식별자 하나뿐이고 Rule은 저장된
        것에서만 나온다.

        Rule item은 Catalog가 Assessment 시점에 거는 것과 같은 검사를 통과해야 한다 —
        `entity_type`이 `POLICY_RULE`이고 lifecycle이 `APPROVED`. 두 곳의 검사가 다르면 게시는
        통과하는데 평가는 실패하는 Profile이 만들어진다.
        """
        _non_empty(customer_id, "customer_id")
        _non_empty(policy_profile_id, "policy_profile_id")
        _non_empty(version, "version")
        item = self._find_item(customer_id, f"POLICY_PROFILE#{policy_profile_id}#VERSION#{version}")
        if item is None:
            raise PolicyProfileNotFound("baseline policy profile version not found")
        try:
            profile = profile_from_dict(dict(item))
        except (KeyError, TypeError, ValueError) as error:
            raise RepositoryError("stored policy profile is invalid") from error

        rules = []
        sources: dict[tuple[str, str], PolicySource] = {}
        for reference in profile.rule_references:
            rule_item = self._find_item(
                customer_id, f"RULE#{reference.rule_id}#VERSION#{reference.version}"
            )
            if rule_item is None:
                raise RepositoryError("baseline profile references a rule that is not stored")
            if rule_item.get("entity_type") != "POLICY_RULE":
                raise RepositoryError("stored policy rule entity type is invalid")
            if rule_item.get("lifecycle") != RuleLifecycle.APPROVED.value:
                raise RepositoryError("baseline profile references a rule that is not approved")
            try:
                rule = rule_from_dict(dict(rule_item))
            except (KeyError, TypeError, ValueError) as error:
                raise RepositoryError("stored policy rule is invalid") from error
            rules.append(rule)
            for cited in rule.source_references:
                key = (cited.source_id, cited.source_version)
                if key in sources:
                    continue
                source_item = self._find_item(
                    customer_id, f"POLICY_SOURCE#{cited.source_id}#VERSION#{cited.source_version}"
                )
                if source_item is None:
                    raise RepositoryError("baseline rule cites a policy source that is not stored")
                sources[key] = _source_from_item(source_item)
        return ProfileBaseline(
            policy_profile_id=profile.policy_profile_id,
            version=profile.version,
            rules=tuple(rules),
            sources=tuple(sources.values()),
        )

    def list_profiles(self, *, customer_id: str) -> tuple[dict[str, object], ...]:
        """Summarise the Profiles published in this customer's partition.

        게시 화면이 기준선을 고르려면 어떤 Profile이 있는지 알아야 한다. current pointer item만
        읽는다 — 판본 이력까지 돌려주면 목록이 이력으로 가득 차고, 고를 수 있는 것은 각 Profile의
        현재 판본이다. 정책 원문은 이 응답에 없다.
        """
        _non_empty(customer_id, "customer_id")
        if self._table is None:
            raise RepositoryError("a read table is required to list policy profiles")
        summaries: list[dict[str, object]] = []
        start_key: object | None = None
        for _ in range(_MAX_QUERY_PAGES):
            request: dict[str, object] = {
                "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
                "ExpressionAttributeValues": {
                    ":pk": f"CUSTOMER#{customer_id}",
                    ":sk": "POLICY_PROFILE#",
                },
            }
            if start_key is not None:
                request["ExclusiveStartKey"] = start_key
            try:
                response = self._table.query(**request)
            except Exception:
                raise RepositoryError("policy profile listing failed") from None
            for item in response.get("Items", []):  # type: ignore[union-attr]
                if not isinstance(item, Mapping) or item.get("customer_id") != customer_id:
                    raise RepositoryError("stored policy profile customer scope is invalid")
                if "#VERSION#" in str(item.get("SK", "")):
                    continue
                try:
                    profile = profile_from_dict(dict(item))
                except (KeyError, TypeError, ValueError) as error:
                    raise RepositoryError("stored policy profile is invalid") from error
                summaries.append(
                    {
                        "policy_profile_id": profile.policy_profile_id,
                        "version": profile.version,
                        "rule_count": len(profile.rule_references),
                        "source_kinds": [kind.value for kind in profile.source_kinds],
                        "published_at": item.get("published_at"),
                    }
                )
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                break
        else:
            raise RepositoryError("policy profile listing did not terminate")
        return tuple(summaries)

    def _find_item(self, customer_id: str, sk: str) -> Mapping[str, object] | None:
        """Read one item in the caller's partition, returning None when it does not exist.

        `_read_item`은 없음과 읽기 실패를 같은 `RepositoryError`로 합친다. 호출자가 둘을
        구별해야 하는 곳에서는 이쪽을 쓴다.
        """
        if self._table is None:
            raise RepositoryError("a read table is required to load policy approval state")
        try:
            item = self._table.get_item(
                Key={"PK": f"CUSTOMER#{customer_id}", "SK": sk}, ConsistentRead=True
            ).get("Item")
        except Exception:
            raise RepositoryError("policy approval state read failed") from None
        if not isinstance(item, Mapping) or item.get("customer_id") != customer_id:
            return None
        return item

    def _read_item(self, customer_id: str, sk: str) -> Mapping[str, object]:
        item = self._find_item(customer_id, sk)
        if item is None:
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

    def _overwrite(self, item: dict[str, object]) -> dict[str, object]:
        """Replace an item that is allowed to advance — the authoring manifest only.

        후보·승인·Profile item은 immutable이므로 이 경로를 쓰지 않는다. manifest만 PROCESSING
        에서 READY로 전진한다.
        """
        return {"Put": {"TableName": self._table_name, "Item": marshal_item(item)}}

    def _write(self, items: list[dict[str, object]], label: str) -> None:
        try:
            self._transaction_client.transact_write_items(TransactItems=items)
        except Exception as error:
            if _error_code(error) in {
                "ConditionalCheckFailedException",
                "TransactionCanceledException",
            }:
                _LOGGER.warning(
                    "%s stale/conditional: code=%s reasons=%s",
                    label,
                    _error_code(error),
                    _cancellation_codes(error),
                )
                raise RepositoryError(f"{label} already exists or binding is stale") from None
            _LOGGER.error(
                "%s: transaction failed code=%s type=%s",
                label,
                _error_code(error),
                type(error).__name__,
            )
            raise RepositoryError(f"{label} write failed") from None

    def _now_iso(self) -> str:
        return self._now().astimezone(UTC).isoformat().replace("+00:00", "Z")

    def _new_id(self, prefix: str) -> str:
        value = self._id_factory()
        _non_empty(value, "generated identifier")
        return f"{prefix}-{value}"


def _manifest_for(result: PolicyAuthoringResult, status: AuthoringRunStatus) -> AuthoringManifest:
    """The manifest for one run in one state. READY carries the counts and digest."""
    document = result.document
    normalized_sha256 = document.normalized_sha256
    if not normalized_sha256:
        raise RepositoryError("a READY document must carry a normalized digest")
    ready = status is AuthoringRunStatus.READY
    return AuthoringManifest(
        source_id=document.source_id,
        source_version=document.source_version,
        normalized_sha256=normalized_sha256,
        status=status,
        provenance=result.provenance,
        counts=result.counts if ready else {},
        result_digest=result.result_digest if ready else None,
    )


def _authoring_child_items(
    customer_id: str, result: PolicyAuthoringResult, prefix: str
) -> list[dict[str, object]]:
    """One item per outcome, keyed by the Requirement's deterministic digest.

    key에 실행 시각이나 순번을 쓰지 않는다. worker 재시도가 같은 후보를 새 key로 다시 쓰면
    승인 화면에 같은 내용의 후보가 둘 생기고 둘 다 승인될 수 있다.
    """
    pk = f"CUSTOMER#{customer_id}"

    def base(segment: str, digest: str) -> dict[str, object]:
        return {
            "PK": pk,
            "SK": f"{prefix}#{segment}#{digest}",
            "entity_type": f"POLICY_AUTHORING_{segment}",
            "customer_id": customer_id,
            "source_id": result.document.source_id,
            "source_version": result.document.source_version,
            "authoring_run_id": result.provenance.authoring_run_id,
            "version": 1,
        }

    items: list[dict[str, object]] = []
    for entry in result.approvable:
        items.append(
            {
                **base("CANDIDATE", entry.requirement.digest),
                "classification": entry.requirement.classification.value,
                **entry.to_dict(),
            }
        )
    for requirement in result.unsupported:
        items.append(
            {
                **base("UNSUPPORTED", requirement.digest),
                "requirement": requirement.to_dict(),
            }
        )
    for rejection in result.rejected:
        items.append(
            {
                **base("REJECTED", rejection.requirement.digest),
                **rejection.to_dict(),
            }
        )
    return items


def _source_item(
    customer_id: str, document: NormalizedPolicyDocument, prefix: str
) -> dict[str, object]:
    """The `PolicySource` item publication reads and re-checks the artifact binding against."""
    return {
        "PK": f"CUSTOMER#{customer_id}",
        "SK": prefix,
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


def _rule_item(customer_id: str, candidate: RuleCandidate, recorded_at: str) -> dict[str, object]:
    """One approved Rule as the Runtime Catalog reads it.

    `lifecycle`을 item에 명시적으로 쓴다. Catalog는 그 값이 `APPROVED`인 item만 Rule로 인정한다 —
    승인 경계를 통과하지 않은 Rule이 어떤 경로로 partition에 들어오더라도 Runtime이 그것을
    평가에 쓰지 못하게 하는 마지막 방어다.

    `recorded_at`은 이 item을 쓴 서버 시각이지 사람이 승인한 시각이 아니다. 후자는 승인 record의
    `approved_at`이며 그것만이 승인의 정체성에 들어간다. 두 값을 같은 이름으로 두면 재시도마다
    달라지는 값이 승인 내용의 일부처럼 보인다.
    """
    if candidate.lifecycle is not RuleLifecycle.APPROVED:
        raise RepositoryError("only an approved candidate may enter the rule registry")
    rule = candidate.rule
    return {
        "PK": f"CUSTOMER#{customer_id}",
        "SK": f"RULE#{rule.rule_id}#VERSION#{rule.version}",
        "entity_type": "POLICY_RULE",
        "customer_id": customer_id,
        "lifecycle": RuleLifecycle.APPROVED.value,
        "recorded_at": recorded_at,
        "schema_version": 1,
        **rule.to_dict(),
    }


def _request_from_item(item: Mapping[str, object]) -> PolicyAuthoringRequest:
    try:
        return PolicyAuthoringRequest(
            customer_id=_require_str(item, "customer_id"),
            source_id=_require_str(item, "source_id"),
            source_version=_require_str(item, "source_version"),
            authoring_run_id=_require_str(item, "authoring_run_id"),
            requested_at=_require_str(item, "requested_at"),
        )
    except (TypeError, ValueError, KeyError):
        raise RepositoryError("policy authoring request is invalid") from None


def _manifest_from_item(item: Mapping[str, object]) -> AuthoringManifest:
    provenance_raw = item.get("provenance")
    counts_raw = item.get("counts")
    if not isinstance(provenance_raw, Mapping):
        raise RepositoryError("policy authoring manifest is invalid")
    failure_raw = item.get("failure_code")
    try:
        return AuthoringManifest(
            source_id=_require_str(item, "source_id"),
            source_version=_require_str(item, "source_version"),
            normalized_sha256=_require_str(item, "normalized_sha256"),
            status=AuthoringRunStatus(_require_str(item, "status")),
            provenance=AuthoringProvenance(
                extractor_id=_require_str(provenance_raw, "extractor_id"),
                extractor_version=_require_str(provenance_raw, "extractor_version"),
                model_id=_require_str(provenance_raw, "model_id"),
                model_version=_require_str(provenance_raw, "model_version"),
                prompt_version=_require_str(provenance_raw, "prompt_version"),
                candidate_schema_version=_require_str(provenance_raw, "candidate_schema_version"),
                control_catalog_version=_require_str(provenance_raw, "control_catalog_version"),
                authoring_run_id=_require_str(provenance_raw, "authoring_run_id"),
                requested_at=_require_str(provenance_raw, "requested_at"),
            ),
            counts={
                str(name): int(value)  # type: ignore[arg-type]
                for name, value in (counts_raw or {}).items()  # type: ignore[union-attr]
            },
            result_digest=(
                None if item.get("result_digest") is None else str(item["result_digest"])
            ),
            failure_code=(
                None if failure_raw is None else ArtifactReadFailureCode(str(failure_raw))
            ),
        )
    except (TypeError, ValueError, KeyError, AttributeError):
        raise RepositoryError("policy authoring manifest is invalid") from None


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
    """Restore one stored candidate through the shared Rule restore path.

    Rule 복원을 여기서 다시 쓰지 않는다. `PolicyRule`에 필드가 늘어날 때 이 함수만 갱신되지
    않으면, 승인된 고객 Rule이 실행 의미를 잃은 채 legacy Rule로 복원되고 Runtime은 그것을
    조용히 3 Perspective로 평가한다.
    """
    if not isinstance(entry, Mapping):
        raise TypeError("candidate entry must be a mapping")
    lifecycle = RuleLifecycle(_require_str(entry, "lifecycle"))
    return RuleCandidate(rule=rule_from_dict(entry.get("rule")), lifecycle=lifecycle)


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

    판본 필드가 두 이름으로 존재한다. 승인 경로가 쓴 item은 `policy_source_version`을 쓴다 —
    item에 이미 스키마 `version`(정수)이 있어 이름이 겹치기 때문이다. 반면 운영자 bootstrap이
    Registry에서 쓴 item은 `PolicySource.to_dict()`를 그대로 펼치므로 `version`이 곧 판본이다.
    기준선(ISMS-P) Rule이 인용하는 Source는 후자에서 오므로 두 모양을 모두 읽어야 한다.
    """
    version = item.get("policy_source_version", item.get("version"))
    try:
        return PolicySource(
            source_id=_require_str(item, "source_id"),
            kind=PolicySourceKind(_require_str(item, "kind")),
            title=_require_str(item, "title"),
            version=version if isinstance(version, str) else "",
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


def _cancellation_codes(error: BaseException) -> list[str | None] | None:
    """Per-item reason codes for a cancelled transaction, or None when absent.

    A cancelled transaction names which item's condition failed; without it a stale-binding
    report says only that *something* was stale. Codes only — the reasons carry item data.
    """
    response = getattr(error, "response", None)
    reasons = response.get("CancellationReasons") if isinstance(response, dict) else None
    if not isinstance(reasons, list):
        return None
    return [reason.get("Code") if isinstance(reason, dict) else None for reason in reasons]
