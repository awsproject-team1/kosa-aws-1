"""Read-only view of the assessment scope configured for the caller's customer.

The scope (which repositories a customer may assess) is deployment configuration, not runtime
state (docs/DESIGN.md Policy runtime). It is injected as ASSESSMENT_SCOPE_JSON, a customer-keyed
map of selector objects. This service returns only the caller customer's entries so the console
can show "connected repositories". It never returns another customer's scope.
"""

from __future__ import annotations

import json

from apps.backend.auth import Principal

#: 평가 경계를 정하는 필드. 이것만이 "이 배포가 무엇을 평가해도 되는가"에 답한다.
SCOPE_SELECTOR_FIELDS = frozenset({"repository_id"})
#: 콘솔이 표시하는 비밀 아닌 연결 정보. 경계를 넓히지 않고, 운영자가 플랫폼이 실제 고객
#: repository/계정에 연결됐는지 눈으로 확인하게 해준다.
SCOPE_CONNECTION_FIELDS = frozenset({"github_repository", "aws_account_id"})
#: 한 scope 항목이 declare할 수 있는 전부. 이 집합이 정본이다 — runtime 파서와 배포 gate가
#: 각자 목록을 손으로 들고 있으면 한쪽만 바뀌었을 때, 배포는 통과했는데 Lambda가 콜드 스타트에
#: 실패하거나(gate가 더 넓음) 화면이 읽을 값을 넣을 방법이 없어진다(gate가 더 좁음). 실제로
#: 후자가 일어나 재배포가 라이브 표시값을 지울 뻔했다.
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
        """
        view: dict[str, object] = {"repository_id": entry.get("repository_id")}
        github_repository = entry.get("github_repository")
        if isinstance(github_repository, str) and github_repository.strip():
            view["github_repository"] = github_repository
        aws_account_id = entry.get("aws_account_id")
        if isinstance(aws_account_id, str) and aws_account_id.strip():
            view["aws_account_id"] = aws_account_id
        return view
