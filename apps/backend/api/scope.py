"""Read-only view of the assessment scope configured for the caller's customer.

The scope (which repositories a customer may assess) is deployment configuration, not runtime
state (docs/DESIGN.md Policy runtime). It is injected as ASSESSMENT_SCOPE_JSON, a customer-keyed
map of selector objects. This service returns only the caller customer's entries so the console
can show "connected repositories". It never returns another customer's scope.
"""

from __future__ import annotations

import json

from apps.backend.auth import Principal


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
