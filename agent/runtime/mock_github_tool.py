"""Deterministic in-memory GitHub Integration Tool for Fixture/Mock development.

This adapter lets D and its consumers develop against the read-only GitHub
boundary before the real GitHub App + OIDC path exists. It holds a fixed set of
IaC snapshots for exactly one approved (customer_id, repository_id) scope and
refuses any request outside that scope. It has no write path by construction.
"""

from collections.abc import Iterable

from agent.runtime.github_tool import (
    GitHubSnapshotNotFoundError,
    GitHubToolScopeError,
    IaCSnapshotRequest,
    require_snapshot_request,
)
from packages.contracts import IaCSnapshot


class MockGitHubTool:
    """Serve read-only IaC snapshots from a scoped, immutable snapshot set."""

    def __init__(
        self,
        *,
        customer_id: str,
        repository_id: str,
        snapshots: Iterable[IaCSnapshot],
    ) -> None:
        _require_non_empty_string(customer_id, "customer_id")
        _require_non_empty_string(repository_id, "repository_id")
        self._customer_id = customer_id
        self._repository_id = repository_id
        self._by_commit: dict[str, IaCSnapshot] = {}
        for snapshot in snapshots:
            if not isinstance(snapshot, IaCSnapshot):
                raise TypeError("snapshots must contain IaCSnapshot items")
            if snapshot.customer_id != customer_id:
                raise ValueError("snapshot customer_id must match tool scope")
            if snapshot.repository_id != repository_id:
                raise ValueError("snapshot repository_id must match tool scope")
            self._by_commit[snapshot.commit_sha] = snapshot

    def read_iac_snapshot(self, request: IaCSnapshotRequest) -> IaCSnapshot:
        """Return the IaC snapshot for a request within tool scope."""
        request = require_snapshot_request(request)
        self._require_scope(request)
        snapshot = self._by_commit.get(request.commit_sha)
        if snapshot is None:
            raise GitHubSnapshotNotFoundError(
                f"no IaC snapshot for commit {request.commit_sha!r} in scope"
            )
        return snapshot

    def _require_scope(self, request: IaCSnapshotRequest) -> None:
        if request.customer_id != self._customer_id or request.repository_id != self._repository_id:
            raise GitHubToolScopeError(
                "request customer_id/repository_id is outside the tool scope"
            )


def _require_non_empty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
