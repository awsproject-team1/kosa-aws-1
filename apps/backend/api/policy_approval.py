"""A service wiring B's immutable policy approval/publication decisions to storage."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from apps.backend.auth import Action, Principal, authorize
from apps.backend.policy.ingestion import ProfileBaseline, approve_source, publish_profile
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

    def load_baseline(
        self, *, customer_id: str, policy_profile_id: str, version: str
    ) -> ProfileBaseline: ...

    def list_profiles(self, *, customer_id: str) -> tuple[dict[str, object], ...]: ...

    def record_profile(
        self,
        *,
        customer_id: str,
        profile: PolicyProfile,
        published_by: str,
        published_at: str,
        expected_current_version: str | None = None,
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

    def list_profiles(self, principal: Principal) -> tuple[dict[str, object], ...]:
        """List the Profiles already published for this customer.

        게시 화면이 기준선을 고르려면 무엇이 있는지 보여줘야 한다. 이름을 손으로 적게 하면
        오타 하나가 "기준선 없음"으로 조용히 게시된다.
        """
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        authorize(principal, Action.PUBLISH_POLICY_PROFILE)
        return self._repository.list_profiles(customer_id=principal.customer_id)

    def publish(
        self,
        principal: Principal,
        *,
        sources: tuple[tuple[str, str], ...],
        policy_profile_id: str,
        version: str,
        baseline: tuple[str, str] | None = None,
        expected_current_version: str | None = None,
    ) -> PolicyProfile:
        """Publish one Profile from every selected policy source, plus an optional baseline.

        `sources`는 `(source_id, source_version)` 목록이다. **문서 하나로 제한하지 않는다** —
        `publish_profile()`은 Rule마다 그 Rule이 인용한 Source의 승인 record로 판정하므로, 사내
        문서를 여러 개 올린 고객이 그 승인 Rule들을 한 Profile로 묶는 데 아무 장애가 없었다.
        제한은 이 API 경계에만 있었고, 그 결과 콘솔은 문서가 섞인 장바구니를 거부해야 했다.

        `baseline`은 ISMS-P 같은 운영자 게시 기준선의 `(policy_profile_id, version)`이다. 그
        Rule에는 고객 승인 record가 없다 — 고객이 올린 문서가 아니기 때문이다. 그래서 이미 이
        고객 파티션에 게시된 Profile에서만 가져온다(`ProfileBaseline` 참조).

        `expected_current_version`이 없으면 최초 게시로 본다. 값이 있으면 그 판본을 가리키고
        있을 때만 pointer가 움직인다 — 동시에 게시된 두 Profile 중 나중 것이 앞의 것을 조용히
        덮어쓰지 않게 한다.
        """
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        for name, value in (
            ("policy_profile_id", policy_profile_id),
            ("version", version),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        selected = _require_source_selection(sources)
        if baseline is not None:
            baseline = _require_pair(baseline, "baseline")
        if not selected and baseline is None:
            # 아무것도 고르지 않은 게시는 빈 Profile이다. `publish_profile`도 거부하지만, 그
            # 거부는 "Rule이 없다"로만 말한다. 무엇을 고르지 않았는지는 여기서만 알 수 있다.
            raise ValueError("a profile must be published from at least one source or a baseline")
        authorize(principal, Action.PUBLISH_POLICY_PROFILE)
        candidates: list[RuleCandidate] = []
        approvals: list[PolicySourceApproval] = []
        published_sources: list[PolicySource] = []
        for source_id, source_version in selected:
            loaded_candidates, loaded_approvals, loaded_sources = self._repository.load_publication(
                customer_id=principal.customer_id,
                source_id=source_id,
                source_version=source_version,
            )
            candidates.extend(loaded_candidates)
            approvals.extend(loaded_approvals)
            published_sources.extend(loaded_sources)
        resolved_baseline = (
            None
            if baseline is None
            else self._repository.load_baseline(
                customer_id=principal.customer_id,
                policy_profile_id=baseline[0],
                version=baseline[1],
            )
        )
        profile = publish_profile(
            policy_profile_id=policy_profile_id,
            version=version,
            candidates=tuple(candidates),
            approvals=tuple(approvals),
            sources=tuple(published_sources),
            baseline=resolved_baseline,
        )
        self._repository.record_profile(
            customer_id=principal.customer_id,
            profile=profile,
            published_by=principal.subject,
            published_at=self._now_iso(),
            expected_current_version=expected_current_version,
        )
        return profile

    def _now_iso(self) -> str:
        return self._now().astimezone(UTC).isoformat().replace("+00:00", "Z")


def _require_source_selection(value: object) -> tuple[tuple[str, str], ...]:
    """Validate the selected `(source_id, source_version)` pairs, refusing repeats.

    같은 판본을 두 번 고르면 그 승인 record가 두 번 들어가고 `publish_profile`은 그것을
    `ORIGINAL_BINDING_MISMATCH`로 거부한다 — 사용자가 한 일은 중복 선택인데 사유는 binding
    불일치로 나온다. 여기서 사실대로 거부한다.
    """
    if value is None:
        return ()
    if not isinstance(value, (tuple, list)):
        raise TypeError("sources must be a sequence of (source_id, source_version) pairs")
    selected: list[tuple[str, str]] = []
    for entry in value:
        pair = _require_pair(entry, "source")
        if pair in selected:
            raise ValueError(f"policy source {pair[0]}@{pair[1]} is selected more than once")
        selected.append(pair)
    return tuple(selected)


def _require_pair(value: object, field_name: str) -> tuple[str, str]:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise TypeError(f"{field_name} must be a pair of non-empty strings")
    first, second = value
    for item in (first, second):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_name} must be a pair of non-empty strings")
    return (first, second)
