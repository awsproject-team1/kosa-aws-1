"""Read-only view of the assessment scope configured for the caller's customer.

The scope (which repositories a customer may assess) is deployment configuration, not runtime
state (docs/DESIGN.md Policy runtime). It is injected as ASSESSMENT_SCOPE_JSON, a customer-keyed
map of selector objects. This service returns only the caller customer's entries so the console
can show "connected repositories". It never returns another customer's scope.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from agent.runtime.github_tool import require_github_repository_full_name
from apps.backend.auth import Principal

_AWS_ACCOUNT_ID = re.compile(r"[0-9]{12}")


def _is_repository_full_name(value: str) -> bool:
    try:
        require_github_repository_full_name(value)
    except ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class ConnectionFieldRule:
    """How one console-visible connection fact is recognized as well-formed."""

    is_valid: Callable[[str], bool]
    #: 배포 gate의 거부 메시지에서 "must be " 뒤에 붙는 요구사항 문구.
    requirement: str


#: 평가 경계를 정하는 필드. 이것만이 "이 배포가 무엇을 평가해도 되는가"에 답한다.
SCOPE_SELECTOR_FIELDS = frozenset({"repository_id"})

#: 콘솔이 표시하는 비밀 아닌 연결 정보와 그 모양. 경계를 넓히지 않고, 운영자가 플랫폼이 실제
#: 고객 repository/계정에 연결됐는지 눈으로 확인하게 해준다.
#:
#: 필드와 검사를 한 곳에 묶는 이유: 이 값을 다루는 곳이 셋이다 — 배포 gate(거부), 이 서비스의
#: 읽기 경로(표시), runtime의 scope 파서(허용). 각자 목록을 들고 있으면 필드가 하나 늘 때
#: 셋 중 둘만 따라오고, 그 어긋남은 라이브 배포에서만 드러난다. 실제로 gate가 runtime보다
#: 좁아서 재배포가 라이브 표시값을 지울 뻔했다.
SCOPE_CONNECTION_RULES: Mapping[str, ConnectionFieldRule] = MappingProxyType(
    {
        "github_repository": ConnectionFieldRule(
            is_valid=_is_repository_full_name,
            requirement="a canonical owner/repository name",
        ),
        "aws_account_id": ConnectionFieldRule(
            is_valid=lambda value: _AWS_ACCOUNT_ID.fullmatch(value) is not None,
            requirement="12 digits",
        ),
    }
)

#: 규칙에서 파생한다 — 검사 없는 연결 정보가 생길 수 없다.
SCOPE_CONNECTION_FIELDS = frozenset(SCOPE_CONNECTION_RULES)
#: 한 scope 항목이 declare할 수 있는 전부.
SCOPE_ENTRY_FIELDS = SCOPE_SELECTOR_FIELDS | SCOPE_CONNECTION_FIELDS


class ScopeApiService:
    def __init__(self, *, scope_json: str | None) -> None:
        self._scope: dict[str, list[dict]] = {}
        if scope_json and scope_json.strip():
            try:
                parsed = json.loads(scope_json)
                if isinstance(parsed, dict):
                    self._scope = parsed
            except json.JSONDecodeError:
                # Malformed config is treated as empty scope rather than crashing the read path.
                self._scope = {}

    def get_scope(self, principal: Principal) -> dict[str, object]:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        entries = self._scope.get(principal.customer_id, [])
        repositories = [
            self._repository_view(e)
            for e in entries
            if isinstance(e, dict) and e.get("repository_id")
        ]
        return {"customer_id": principal.customer_id, "repositories": repositories}

    @staticmethod
    def _repository_view(entry: dict) -> dict[str, object]:
        """Public, non-secret connection facts for one assessment target.

        The console shows these so an operator can confirm the platform is wired to the
        real customer GitHub repository and AWS account before running a live assessment.
        Only non-secret identifiers are surfaced; secret *references* (role ARNs, secret
        IDs) are never returned by the read API.

        A value that does not match its rule is dropped rather than shown. The deploy gate
        already refuses one, but `ASSESSMENT_SCOPE_JSON` is an ordinary Lambda variable that
        can be edited outside the workflow — and was, back when the gate could not carry
        these fields at all. This screen exists to be trusted, so it serves only what it can
        still recognize.
        """
        view: dict[str, object] = {"repository_id": entry.get("repository_id")}
        for field_name, rule in SCOPE_CONNECTION_RULES.items():
            value = entry.get(field_name)
            if isinstance(value, str) and rule.is_valid(value):
                view[field_name] = value
        return view
