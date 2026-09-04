"""The asynchronous candidate-extraction boundary: request a run, then read its results.

추출은 동기 요청으로 처리하지 않는다. 모델 호출과 문서 크기는 API Gateway의 시간 예산 밖이고,
무엇보다 **추출은 재시도 가능해야 한다.** 요청은 QUEUED manifest와 queue 메시지로 남고, worker가
그것을 처리한다. 요청이 실패해도 요청 사실 자체는 남으므로 사람이 다시 누르지 않아도 된다.

응답에는 정규화 문서의 원문이 없다. 리뷰어가 보는 문장은 모델이 쓴 재진술이며, 그 문장과 함께
locator와 **서버가 만든** `content_sha256`이 나간다 — 근거가 어느 판본의 어느 단위인지 확인할 수
있어야 승인이 의미를 갖는다.
"""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from apps.backend.auth import Action, Principal, authorize
from apps.backend.policy.authoring.serialization import (
    accepted_from_dict,
    rejected_from_dict,
    requirement_from_dict,
    unclassified_from_dict,
)
from packages.common.errors import AuthoringRunNotFound, PolicySourceNotFound
from packages.contracts import (
    AuthoringManifest,
    AuthoringRunStatus,
    CandidateReviewEntry,
    ExtractedRequirement,
    PolicyAuthoringRequest,
    RejectedRequirement,
    UnclassifiedUnits,
)

#: 한 페이지가 돌려주는 결과 수의 상한. 상한이 없으면 응답 크기가 문서 크기를 따라간다.
MAX_PAGE_SIZE = 50
DEFAULT_PAGE_SIZE = 25


class PolicyAuthoringRepository(Protocol):
    def request_extraction(
        self,
        *,
        customer_id: str,
        source_id: str,
        source_version: str,
        authoring_run_id: str,
        requested_at: str,
    ) -> PolicyAuthoringRequest: ...

    def load_authoring_manifest(
        self, *, customer_id: str, source_id: str, source_version: str
    ) -> AuthoringManifest: ...

    def has_authoring_request(
        self, *, customer_id: str, source_id: str, source_version: str
    ) -> bool: ...

    def load_authoring_results(
        self, *, customer_id: str, source_id: str, source_version: str
    ) -> tuple[AuthoringManifest, tuple[Mapping[str, object], ...]]: ...


class AuthoringQueue(Protocol):
    """Publish one extraction request to the authoring worker queue."""

    def enqueue(self, request: PolicyAuthoringRequest) -> None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyAuthoringRunAccepted:
    """The 202 body: the run exists and will be processed."""

    authoring_run_id: str
    status: AuthoringRunStatus

    def to_dict(self) -> dict[str, str]:
        return {"authoring_run_id": self.authoring_run_id, "status": self.status.value}


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyCandidatePage:
    """One page of an authoring run's outcomes, plus what produced them."""

    status: AuthoringRunStatus
    candidates: tuple[CandidateReviewEntry, ...] = ()
    unsupported: tuple[ExtractedRequirement, ...] = ()
    rejected: tuple[RejectedRequirement, ...] = ()
    #: 추출이 답하지 못한 unit. 비어 있지 않은 READY 실행은 "다 훑었다"가 아니라 "훑은
    #: 만큼이 완전하다"는 뜻이므로, 리뷰어가 승인 전에 이 목록을 본다.
    unclassified: tuple[UnclassifiedUnits, ...] = ()
    counts: dict[str, int] | None = None
    provenance: dict[str, object] | None = None
    cursor: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "counts": self.counts,
            "provenance": self.provenance,
            "candidates": [entry.to_dict() for entry in self.candidates],
            "unsupported": [entry.to_dict() for entry in self.unsupported],
            "rejected": [entry.to_dict() for entry in self.rejected],
            "unclassified": [entry.to_dict() for entry in self.unclassified],
            "cursor": self.cursor,
        }


class PolicyCandidateApiService:
    """Request an extraction run and read its reviewable results."""

    def __init__(
        self,
        *,
        repository: PolicyAuthoringRepository,
        queue: AuthoringQueue,
        now: Callable[[], datetime] | None = None,
        run_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if repository is None or queue is None:
            raise TypeError("repository and queue are required")
        self._repository = repository
        self._queue = queue
        self._now = now or (lambda: datetime.now(UTC))
        self._run_id_factory = run_id_factory or (lambda: f"authoring-{uuid4()}")

    def request_extraction(
        self, principal: Principal, *, source_id: str, source_version: str
    ) -> PolicyAuthoringRunAccepted:
        """Record the request durably, then dispatch it.

        순서가 중요하다. queue에 먼저 보내면, 저장이 실패했을 때 worker가 존재하지 않는 요청을
        처리하게 된다. 반대 순서에서는 dispatch가 실패해도 요청은 남아 재시도할 수 있다.
        """
        _require_principal(principal)
        _non_empty(source_id, "source_id")
        _non_empty(source_version, "source_version")
        authorize(principal, Action.MANAGE_POLICY_SOURCES)
        request = self._repository.request_extraction(
            customer_id=principal.customer_id,
            source_id=source_id,
            source_version=source_version,
            authoring_run_id=self._run_id_factory(),
            requested_at=self._now_iso(),
        )
        self._queue.enqueue(request)
        return PolicyAuthoringRunAccepted(
            authoring_run_id=request.authoring_run_id, status=AuthoringRunStatus.QUEUED
        )

    def list_candidates(
        self,
        principal: Principal,
        *,
        source_id: str,
        source_version: str,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> PolicyCandidatePage:
        _require_principal(principal)
        _non_empty(source_id, "source_id")
        _non_empty(source_version, "source_version")
        authorize(principal, Action.MANAGE_POLICY_SOURCES)
        page_size = _page_size(limit)

        try:
            manifest = self._repository.load_authoring_manifest(
                customer_id=principal.customer_id,
                source_id=source_id,
                source_version=source_version,
            )
        except AuthoringRunNotFound:
            # 요청은 있고 결과가 아직 없는 구간이다. 이것을 오류로 돌려주면 콘솔이 업로드
            # 직후부터 worker가 끝날 때까지 계속 실패를 표시한다. 요청 자체가 없으면 이
            # 판본에 대한 실행이 없다는 뜻이므로 404다.
            if self._repository.has_authoring_request(
                customer_id=principal.customer_id,
                source_id=source_id,
                source_version=source_version,
            ):
                return PolicyCandidatePage(status=AuthoringRunStatus.QUEUED)
            raise PolicySourceNotFound("no authoring run for this policy source version") from None
        if not manifest.is_reviewable:
            # 아직 완결되지 않은 실행은 상태만 돌려준다. 부분 결과를 보여주면 리뷰어가 그것을
            # 전체로 착각하고 승인한다.
            return PolicyCandidatePage(
                status=manifest.status, provenance=manifest.provenance.to_dict()
            )

        _manifest, items = self._repository.load_authoring_results(
            customer_id=principal.customer_id,
            source_id=source_id,
            source_version=source_version,
        )
        window, next_cursor = _page(items, cursor=cursor, page_size=page_size)
        candidates: list[CandidateReviewEntry] = []
        unsupported: list[ExtractedRequirement] = []
        rejected: list[RejectedRequirement] = []
        unclassified: list[UnclassifiedUnits] = []
        for item in window:
            entity = str(item.get("entity_type", ""))
            if entity.endswith("_CANDIDATE"):
                candidates.append(CandidateReviewEntry.from_accepted(accepted_from_dict(item)))
            elif entity.endswith("_UNSUPPORTED"):
                unsupported.append(requirement_from_dict(item.get("requirement")))
            elif entity.endswith("_REJECTED"):
                rejected.append(rejected_from_dict(item))
            elif entity.endswith("_UNCLASSIFIED"):
                unclassified.append(unclassified_from_dict(item))
        return PolicyCandidatePage(
            status=manifest.status,
            candidates=tuple(candidates),
            unsupported=tuple(unsupported),
            rejected=tuple(rejected),
            unclassified=tuple(unclassified),
            counts=dict(manifest.counts),
            provenance=manifest.provenance.to_dict(),
            cursor=next_cursor,
        )

    def _now_iso(self) -> str:
        return self._now().astimezone(UTC).isoformat().replace("+00:00", "+00:00")


def _page(
    items: Sequence[Mapping[str, object]], *, cursor: str | None, page_size: int
) -> tuple[tuple[Mapping[str, object], ...], str | None]:
    """Slice the stored items by their sort key, which is stable across requests.

    offset이 아니라 마지막으로 돌려준 key를 cursor로 쓴다. 실행이 끝난 뒤 item 집합은 변하지
    않지만, key 기반 cursor는 그 가정이 깨져도 같은 항목을 두 번 보여주거나 건너뛰지 않는다.
    """
    start = 0
    if cursor is not None:
        last = _decode_cursor(cursor)
        start = next(
            (index + 1 for index, item in enumerate(items) if str(item.get("SK")) == last),
            None,  # type: ignore[arg-type]
        )
        if start is None:
            raise ValueError("cursor does not refer to this authoring run")
    window = tuple(items[start : start + page_size])
    if start + page_size >= len(items) or not window:
        return window, None
    return window, _encode_cursor(str(window[-1].get("SK")))


def _encode_cursor(sort_key: str) -> str:
    return urlsafe_b64encode(sort_key.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> str:
    if not isinstance(cursor, str) or not cursor.strip():
        raise ValueError("cursor must be a non-empty string")
    try:
        return urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except Exception as error:
        raise ValueError("cursor is invalid") from error


def _page_size(limit: object) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if limit <= 0 or limit > MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    return limit


def _require_principal(principal: object) -> None:
    if not isinstance(principal, Principal):
        raise TypeError("principal must be a Principal")


def _non_empty(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
