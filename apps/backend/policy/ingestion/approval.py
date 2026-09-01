"""Approve reviewed Rule candidates and publish them as a Policy Profile.

`docs/POLICY_INGESTION.md`의 "Required public/API boundary" 4번과 5번이다. **승인과 게시는 서로
다른 operation이다.** 승인은 Source/Control/Rule version을 확정하고, 게시는 그 Rule들을 평가
경계로 만든다. 하나로 합치더라도 거부 조건은 같으므로 판정을 두 함수로 나눠 둔다.

이 모듈은 아무것도 영속화하지 않는다. A의 승인 API가 조건부 write 전에 호출하는 순수 판정이다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from packages.contracts import (
    NormalizedPolicyDocument,
    PolicyProfile,
    PolicyRule,
    PolicyRuleReference,
    PolicySource,
)
from packages.contracts.policy_approval import (
    ApprovalRejectionCode,
    PolicySourceApproval,
    RuleCandidate,
)


class ApprovalRejectedError(ValueError):
    """Raised when an approval or a publication is refused.

    사유는 `rejection_code`로만 표현한다. 메시지에 원문 내용이나 추출 텍스트를 담지 않는다.
    """

    def __init__(self, rejection_code: ApprovalRejectionCode, message: str) -> None:
        super().__init__(message)
        self.rejection_code = rejection_code


def approve_source(
    document: NormalizedPolicyDocument,
    candidates: Iterable[RuleCandidate],
    *,
    approved_by: str,
    approved_at: str,
) -> tuple[PolicySourceApproval, tuple[RuleCandidate, ...]]:
    """Approve reviewed candidates against the exact normalized document they cite.

    승인은 검증된 그 판본에만 붙는다. 후보가 인용한 locator와 hash를 정규화 결과와 대조해,
    사람이 본 문장과 Rule이 가리키는 문장이 같음을 승인 시점에 고정한다. 나중에 원문이 개정되면
    새 Source version이 되므로 이 승인은 옛 판본에 그대로 남는다.

    Returns the immutable approval record and the candidates marked `APPROVED`.
    """
    if not isinstance(document, NormalizedPolicyDocument):
        raise TypeError("document must be a NormalizedPolicyDocument")
    if not document.is_approvable:
        raise ApprovalRejectedError(
            ApprovalRejectionCode.SOURCE_NOT_APPROVABLE,
            f"a {document.status.value} document may not be approved",
        )

    reviewed = tuple(candidates)
    if not reviewed:
        raise ApprovalRejectedError(
            ApprovalRejectionCode.EMPTY_PROFILE, "an approval must cover at least one rule"
        )
    for candidate in reviewed:
        if not isinstance(candidate, RuleCandidate):
            raise TypeError("candidates must contain RuleCandidate values")
        _require_rule_matches_document(candidate.rule, document)

    approved = tuple(candidate.approved() for candidate in reviewed)
    # `normalized_artifact_id`/`normalized_sha256`은 `is_approvable`이 이미 `FAILED`를 배제해
    # 반드시 존재한다. Contract의 `_require_consistent_outcome()`가 그 불변식을 강제한다.
    assert document.normalized_artifact_id is not None
    assert document.normalized_sha256 is not None
    approval = PolicySourceApproval(
        source_id=document.source_id,
        source_version=document.source_version,
        artifact_id=document.artifact_id,
        s3_version_id=document.s3_version_id,
        content_sha256=document.content_sha256,
        normalized_artifact_id=document.normalized_artifact_id,
        normalized_sha256=document.normalized_sha256,
        approved_rules=tuple(candidate.reference for candidate in approved),
        approved_by=approved_by,
        approved_at=approved_at,
    )
    return approval, approved


def publish_profile(
    *,
    policy_profile_id: str,
    version: str,
    candidates: Iterable[RuleCandidate],
    approvals: Iterable[PolicySourceApproval],
    sources: Iterable[PolicySource] = (),
) -> PolicyProfile:
    """Build a Policy Profile from approved rules, refusing anything unapproved.

    `docs/POLICY_INGESTION.md`가 명시한 세 거부 조건을 그대로 구현한다.

    1. 승인되지 않은 Source 또는 Rule을 참조하는 Profile
    2. 승인된 것과 다른 Source version을 가리키는 `SourceReference`
    3. 승인 record가 인용한 `(artifact_id, s3_version_id, content_sha256)`과 어긋나는 Rule

    `sources`는 이미 게시된 `PolicySource` 목록이다. 3번을 검사하려면 승인 record의 binding과
    대조할 Source 쪽 값이 필요하다. 비워 두면 1·2번만 검사한다.
    """
    approval_index = _approval_index(approvals)
    source_index = {(source.source_id, source.version): source for source in sources}

    references: list[PolicyRuleReference] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        if not isinstance(candidate, RuleCandidate):
            raise TypeError("candidates must contain RuleCandidate values")
        rule = candidate.rule
        if not candidate.is_approved:
            raise ApprovalRejectedError(
                ApprovalRejectionCode.RULE_NOT_APPROVED,
                f"rule {rule.rule_id}@{rule.version} is {candidate.lifecycle.value}, not APPROVED",
            )
        _require_approved_provenance(rule, approval_index, source_index)

        key = (rule.rule_id, rule.version)
        if key in seen:
            raise ApprovalRejectedError(
                ApprovalRejectionCode.DUPLICATE_RULE_REFERENCE,
                f"rule {rule.rule_id}@{rule.version} is listed more than once",
            )
        seen.add(key)
        references.append(candidate.reference)

    if not references:
        raise ApprovalRejectedError(
            ApprovalRejectionCode.EMPTY_PROFILE, "a profile must reference at least one rule"
        )
    return PolicyProfile(
        policy_profile_id=policy_profile_id,
        version=version,
        rule_references=tuple(references),
    )


def _require_rule_matches_document(rule: PolicyRule, document: NormalizedPolicyDocument) -> None:
    """Every reference must cite a unit of this exact normalized document version."""
    for reference in rule.source_references:
        if (reference.source_id, reference.source_version) != (
            document.source_id,
            document.source_version,
        ):
            raise ApprovalRejectedError(
                ApprovalRejectionCode.SOURCE_VERSION_MISMATCH,
                f"rule {rule.rule_id}@{rule.version} cites "
                f"{reference.source_id}@{reference.source_version}, not the approved version",
            )
        unit = document.unit(reference.locator)
        if unit is None:
            raise ApprovalRejectedError(
                ApprovalRejectionCode.UNKNOWN_LOCATOR,
                f"rule {rule.rule_id}@{rule.version} cites a locator "
                "the normalized document does not contain",
            )
        if unit.text_sha256 != reference.content_sha256:
            # 사람이 검토한 문장과 Rule이 고정한 hash가 다르면, 승인이 다른 내용에 붙는다.
            raise ApprovalRejectedError(
                ApprovalRejectionCode.CONTENT_DIGEST_MISMATCH,
                f"rule {rule.rule_id}@{rule.version} pins a digest that does not match "
                "the normalized unit",
            )


def _require_approved_provenance(
    rule: PolicyRule,
    approvals: Mapping[tuple[str, str], PolicySourceApproval],
    sources: Mapping[tuple[str, str], PolicySource],
) -> None:
    for reference in rule.source_references:
        key = (reference.source_id, reference.source_version)
        approval = approvals.get(key)
        if approval is None:
            # 승인된 Source가 없거나, 승인된 것과 **다른 version**을 가리킨다. 둘 다 거부다.
            code = (
                ApprovalRejectionCode.SOURCE_VERSION_MISMATCH
                if any(source_id == reference.source_id for source_id, _ in approvals)
                else ApprovalRejectionCode.SOURCE_NOT_APPROVED
            )
            raise ApprovalRejectedError(
                code,
                f"rule {rule.rule_id}@{rule.version} cites "
                f"{reference.source_id}@{reference.source_version}, which is not approved",
            )
        if not approval.approves(PolicyRuleReference(rule_id=rule.rule_id, version=rule.version)):
            raise ApprovalRejectedError(
                ApprovalRejectionCode.RULE_NOT_APPROVED,
                f"rule {rule.rule_id}@{rule.version} is not covered by the source approval",
            )
        source = sources.get(key)
        if source is None:
            continue
        if (source.artifact_id, source.content_sha256) != (
            approval.artifact_id,
            approval.content_sha256,
        ):
            # 승인은 통과했지만 게시된 Source가 다른 원본을 가리킨다. 승인을 판본 사이에서
            # 옮겨 붙이려는 시도가 여기서 걸린다.
            raise ApprovalRejectedError(
                ApprovalRejectionCode.ORIGINAL_BINDING_MISMATCH,
                f"rule {rule.rule_id}@{rule.version} cites a source version whose artifact "
                "binding differs from the approval record",
            )


def _approval_index(
    approvals: Iterable[PolicySourceApproval],
) -> dict[tuple[str, str], PolicySourceApproval]:
    index: dict[tuple[str, str], PolicySourceApproval] = {}
    for approval in approvals:
        if not isinstance(approval, PolicySourceApproval):
            raise TypeError("approvals must contain PolicySourceApproval values")
        key = (approval.source_id, approval.source_version)
        if key in index:
            raise ApprovalRejectedError(
                ApprovalRejectionCode.ORIGINAL_BINDING_MISMATCH,
                f"policy source {approval.source_id}@{approval.source_version} "
                "has more than one approval record",
            )
        index[key] = approval
    return index
