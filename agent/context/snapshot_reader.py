"""Assessment input collection for D (Remediation/Deployment).

This module consumes the two read-only D tool boundaries -- the GitHub
Integration Tool and the AWS Resource Tool -- and combines their reads into a
single immutable Assessment input bundle that the AI evaluation boundary (C)
consumes as-is. Per ADR-0007 both underlying tools are read-only and scoped to
approved resources; this collector adds no write surface. A request names one
approved ``customer_id`` that both halves (IaC repository and AWS account)
share by construction, so the collector cannot read across customers.

The collector does not evaluate anything. It only gathers the IaC snapshot
(IAC perspective) and the AWS Actual resource views (AWS_ACTUAL perspective)
that C needs. Drift judgement (IAC vs AWS_ACTUAL) is a separate boundary.
"""

from dataclasses import dataclass

from agent.runtime.aws_resource_tool import AwsResourceTool, AwsResourceView
from agent.runtime.github_tool import GitHubTool, IaCSnapshotRequest
from packages.contracts import (
    AwsResourceOperation,
    AwsResourceQuery,
    IaCSnapshot,
)


class AssessmentInputError(RuntimeError):
    """Base failure for collecting an Assessment input bundle."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AwsResourceSelector:
    """One read against the AWS Resource Tool within a single account scope.

    A selector names an operation and target the same way ``AwsResourceQuery``
    does, minus the scope fields (customer_id/aws_account_id) which the request
    supplies once so every AWS read shares one account.
    """

    operation: AwsResourceOperation
    resource_type: str
    resource_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.operation, AwsResourceOperation):
            raise TypeError("operation must be an AwsResourceOperation")
        if not isinstance(self.resource_type, str) or not self.resource_type.strip():
            raise ValueError("resource_type must be a non-empty string")
        if self.resource_id is not None and (
            not isinstance(self.resource_id, str) or not self.resource_id.strip()
        ):
            raise ValueError("resource_id must be a non-empty string when provided")

    def to_query(self, *, customer_id: str, aws_account_id: str) -> AwsResourceQuery:
        """Build a scoped ``AwsResourceQuery`` for this selector.

        The Contract's own validation (e.g. READ_RESOURCE requires resource_id)
        applies here, so an ill-formed selector fails at query construction.
        """
        return AwsResourceQuery(
            customer_id=customer_id,
            aws_account_id=aws_account_id,
            operation=self.operation,
            resource_type=self.resource_type,
            resource_id=self.resource_id,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SnapshotReadRequest:
    """Immutable request to collect one Assessment input bundle.

    Names the approved IaC coordinate (customer_id, repository_id, commit_sha)
    and the approved AWS account plus the resource selectors to read. Both
    halves must name the same ``customer_id``; this is validated at
    construction so an out-of-scope request never reaches the tools.
    """

    customer_id: str
    repository_id: str
    commit_sha: str
    aws_account_id: str
    aws_selectors: tuple[AwsResourceSelector, ...]

    def __post_init__(self) -> None:
        for name in ("customer_id", "repository_id", "commit_sha", "aws_account_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.aws_selectors, tuple):
            raise TypeError("aws_selectors must be a tuple")
        if not self.aws_selectors:
            raise ValueError("aws_selectors must not be empty")
        for selector in self.aws_selectors:
            if not isinstance(selector, AwsResourceSelector):
                raise TypeError("aws_selectors must contain AwsResourceSelector items")

    def iac_request(self) -> IaCSnapshotRequest:
        return IaCSnapshotRequest(
            customer_id=self.customer_id,
            repository_id=self.repository_id,
            commit_sha=self.commit_sha,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AssessmentInputBundle:
    """Immutable, read-only Assessment input for the AI evaluation boundary.

    Carries the IaC snapshot (IAC perspective) and the AWS Actual resource
    views (AWS_ACTUAL perspective) for one approved customer scope. The bundle
    exposes no mutation surface; both the snapshot and the views are already
    frozen Contract/runtime values.
    """

    customer_id: str
    iac_snapshot: IaCSnapshot
    aws_resources: tuple[AwsResourceView, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.customer_id, str) or not self.customer_id.strip():
            raise ValueError("customer_id must be a non-empty string")
        if not isinstance(self.iac_snapshot, IaCSnapshot):
            raise TypeError("iac_snapshot must be an IaCSnapshot")
        if not isinstance(self.aws_resources, tuple):
            raise TypeError("aws_resources must be a tuple")
        for view in self.aws_resources:
            if not isinstance(view, AwsResourceView):
                raise TypeError("aws_resources must contain AwsResourceView items")

    def to_dict(self) -> dict[str, object]:
        return {
            "customer_id": self.customer_id,
            "iac_snapshot": self.iac_snapshot.to_dict(),
            "aws_resources": [view.to_dict() for view in self.aws_resources],
        }


class AssessmentInputCollector:
    """Combine read-only IaC and AWS reads into one Assessment input bundle."""

    def __init__(self, *, github_tool: GitHubTool, aws_tool: AwsResourceTool) -> None:
        self._github_tool = github_tool
        self._aws_tool = aws_tool

    def collect(self, request: SnapshotReadRequest) -> AssessmentInputBundle:
        """Read the IaC snapshot and AWS Actual views for one customer scope.

        Scope errors and not-found errors from either tool propagate unchanged
        so the caller sees the same read-only boundary failures the tools
        raise. The two reads are gathered into a single immutable bundle.
        """
        if not isinstance(request, SnapshotReadRequest):
            raise TypeError("request must be a SnapshotReadRequest")

        iac_snapshot = self._github_tool.read_iac_snapshot(request.iac_request())

        views: list[AwsResourceView] = []
        for selector in request.aws_selectors:
            query = selector.to_query(
                customer_id=request.customer_id,
                aws_account_id=request.aws_account_id,
            )
            if selector.operation is AwsResourceOperation.READ_RESOURCE:
                views.append(self._aws_tool.read_resource(query))
            else:
                views.extend(self._aws_tool.list_resources(query))

        return AssessmentInputBundle(
            customer_id=request.customer_id,
            iac_snapshot=iac_snapshot,
            aws_resources=tuple(views),
        )
