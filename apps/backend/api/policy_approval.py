"""A service wiring B's immutable policy approval/publication decisions to storage."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from apps.backend.auth import Action, Principal, authorize
from apps.backend.policy.ingestion import approve_source, publish_profile
from packages.contracts import (
    NormalizedPolicyDocument,
    PolicyProfile,
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
        self, principal: Principal, *, source_id: str, source_version: str
    ) -> PolicySourceApproval:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        authorize(principal, Action.MANAGE_POLICY_SOURCES)
        document, candidates = self._repository.load_review(
            customer_id=principal.customer_id, source_id=source_id, source_version=source_version
        )
        approval, approved = approve_source(
            document, candidates, approved_by=principal.subject, approved_at=self._now_iso()
        )
        self._repository.record_approval(
            customer_id=principal.customer_id, approval=approval, candidates=approved
        )
        return approval

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
