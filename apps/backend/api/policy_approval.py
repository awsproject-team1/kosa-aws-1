"""A service wiring B's immutable policy approval/publication decisions to storage."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from apps.backend.auth import Action, Principal, authorize
from apps.backend.policy.ingestion import approve_source, publish_profile
from packages.contracts import (
    NormalizedPolicyDocument,
    PolicyProfile,
    PolicyRuleReference,
    PolicySource,
    PolicySourceApproval,
    RuleCandidate,
)


class PolicyApprovalRepository(Protocol):
    def load_review(
        self, *, customer_id: str, source_id: str, source_version: str
    ) -> tuple[NormalizedPolicyDocument, tuple[RuleCandidate, ...]]: ...
    def record_approval(
        self,
        *,
        customer_id: str,
        approval: PolicySourceApproval,
        candidates: tuple[RuleCandidate, ...],
    ) -> None: ...

    def load_publication(
        self, *, customer_id: str, source_id: str, source_version: str
    ) -> tuple[
        tuple[RuleCandidate, ...], tuple[PolicySourceApproval, ...], tuple[PolicySource, ...]
    ]: ...

    def record_profile(
        self, *, customer_id: str, profile: PolicyProfile, published_by: str, published_at: str
    ) -> None: ...


class PolicyApprovalApiService:
    def __init__(
        self, repository: PolicyApprovalRepository, *, now: Callable[[], datetime] | None = None
    ) -> None:
        if repository is None:
            raise TypeError("repository is required")
        self._repository = repository
        self._now = now or (lambda: datetime.now(UTC))

    def approve(
        self,
        principal: Principal,
        *,
        source_id: str,
        source_version: str,
        approved_rules: tuple[PolicyRuleReference, ...],
    ) -> PolicySourceApproval:
        """리뷰어가 고른 Rule 후보만 승인한다.

        `approved_rules`는 사람이 검토해 승인하기로 고른 `(rule_id, version)` 목록이다. AI 추출
        후보 전량이 아니라 이 부분집합만 `approve_source()`에 넘겨야, 후보 저장(C)과 사람 승인(A)
        사이의 검토 게이트가 형식으로 남지 않는다(`docs/POLICY_INGESTION.md` 인수 조건 4, 150행).
        목록에 없는 후보는 CANDIDATE로 남고 Profile·Assessment에 들어가지 않는다.
        """
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        if not isinstance(approved_rules, tuple) or not approved_rules:
            raise ValueError("approved_rules must be a non-empty tuple")
        for reference in approved_rules:
            if not isinstance(reference, PolicyRuleReference):
                raise TypeError("approved_rules items must be PolicyRuleReference values")
        authorize(principal, Action.MANAGE_POLICY_SOURCES)
        document, candidates = self._repository.load_review(
            customer_id=principal.customer_id, source_id=source_id, source_version=source_version
        )
        selected = self._select_reviewed(candidates, approved_rules)
        approval, approved = approve_source(
            document, selected, approved_by=principal.subject, approved_at=self._now_iso()
        )
        self._repository.record_approval(
            customer_id=principal.customer_id, approval=approval, candidates=approved
        )
        return approval

    @staticmethod
    def _select_reviewed(
        candidates: tuple[RuleCandidate, ...], approved_rules: tuple[PolicyRuleReference, ...]
    ) -> tuple[RuleCandidate, ...]:
        """승인 목록에 든 Rule 후보만 골라 순서를 보존해 돌려준다.

        승인 대상이 후보에 없으면 거부한다 — 존재하지 않는 Rule을 승인 record에 넣지 않는다.
        """
        by_reference = {
            (candidate.rule.rule_id, candidate.rule.version): candidate for candidate in candidates
        }
        selected: list[RuleCandidate] = []
        seen: set[tuple[str, str]] = set()
        for reference in approved_rules:
            key = (reference.rule_id, reference.version)
            if key in seen:
                raise ValueError(f"duplicate approved rule {reference.rule_id}@{reference.version}")
            seen.add(key)
            candidate = by_reference.get(key)
            if candidate is None:
                raise ValueError(
                    f"approved rule {reference.rule_id}@{reference.version} is not a candidate"
                )
            selected.append(candidate)
        return tuple(selected)

    def publish(
        self,
        principal: Principal,
        *,
        source_id: str,
        source_version: str,
        policy_profile_id: str,
        version: str,
    ) -> PolicyProfile:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        for name, value in (
            ("source_id", source_id),
            ("source_version", source_version),
            ("policy_profile_id", policy_profile_id),
            ("version", version),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        authorize(principal, Action.PUBLISH_POLICY_PROFILE)
        candidates, approvals, sources = self._repository.load_publication(
            customer_id=principal.customer_id,
            source_id=source_id,
            source_version=source_version,
        )
        profile = publish_profile(
            policy_profile_id=policy_profile_id,
            version=version,
            candidates=candidates,
            approvals=approvals,
            sources=sources,
        )
        self._repository.record_profile(
            customer_id=principal.customer_id,
            profile=profile,
            published_by=principal.subject,
            published_at=self._now_iso(),
        )
        return profile

    def _now_iso(self) -> str:
        return self._now().astimezone(UTC).isoformat().replace("+00:00", "Z")
