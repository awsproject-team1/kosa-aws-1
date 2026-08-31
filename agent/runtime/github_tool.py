"""Read-only GitHub Integration Tool boundary for D (Remediation/Deployment).

This module defines the provider-neutral port that the agent runtime uses to
read Customer IaC state from an approved GitHub repository. Per ADR-0007 the
GitHub App holds least-privilege access to approved Customer IaC repositories,
and the Remediation write path (Branch/Commit/PR) is intentionally out of scope
until M2. This boundary therefore exposes only IaC snapshot reads; it cannot
express a write or mutation. Access is scoped to an approved
(customer_id, repository_id) pair and callers must not delegate that scope to
policy or AI input.
"""

from dataclasses import dataclass
from typing import Protocol

from packages.contracts import IaCSnapshot


class GitHubToolError(RuntimeError):
    """Base failure for a read-only GitHub Integration Tool operation."""


class GitHubToolScopeError(GitHubToolError):
    """Raised when a request targets a customer/repository outside tool scope."""


class GitHubSnapshotNotFoundError(GitHubToolError):
    """Raised when a requested IaC snapshot does not exist in the read state."""


@dataclass(frozen=True, slots=True, kw_only=True)
class IaCSnapshotRequest:
    """Immutable read request for one repository IaC snapshot.

    A request names an approved (customer_id, repository_id) scope and an exact
    ``commit_sha``. It carries no field that could mutate the repository; the
    tool only ever returns a descriptive snapshot for this coordinate.
    """

    customer_id: str
    repository_id: str
    commit_sha: str

    def __post_init__(self) -> None:
        for name in ("customer_id", "repository_id", "commit_sha"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")

    def to_dict(self) -> dict[str, str]:
        return {
            "customer_id": self.customer_id,
            "repository_id": self.repository_id,
            "commit_sha": self.commit_sha,
        }


class GitHubTool(Protocol):
    """Read-only operations required to inspect Customer IaC state."""

    def read_iac_snapshot(self, request: IaCSnapshotRequest) -> IaCSnapshot:
        """Return the IaC snapshot for a request within tool scope."""
        ...


def require_snapshot_request(request: object) -> IaCSnapshotRequest:
    """Validate a request object for a read-only IaC snapshot lookup.

    Keeping this check in one place ensures every adapter enforces the same
    read-only boundary rather than trusting the caller to pass the right shape.
    """
    if not isinstance(request, IaCSnapshotRequest):
        raise TypeError("request must be an IaCSnapshotRequest")
    return request
