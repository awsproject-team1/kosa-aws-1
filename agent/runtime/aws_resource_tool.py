"""Read-only AWS Resource Tool boundary for D (Remediation/Deployment).

This module defines the provider-neutral port that the agent runtime uses to
read Customer AWS Actual state. Per ADR-0007 and ADR-0009 the tool exposes only
two read operations (READ_RESOURCE, LIST_RESOURCES); it cannot express a write
or mutation. Access is scoped to an approved (customer_id, aws_account_id) pair
and callers must not delegate that scope to policy or AI input.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from packages.contracts import AwsResourceOperation, AwsResourceQuery


class AwsResourceToolError(RuntimeError):
    """Base failure for a read-only AWS Resource Tool operation."""


class AwsResourceScopeError(AwsResourceToolError):
    """Raised when a query targets a customer/account outside the tool scope."""


class AwsResourceNotFoundError(AwsResourceToolError):
    """Raised when a requested resource does not exist in the read snapshot."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AwsResourceView:
    """Immutable read-only view of a single AWS resource.

    The ``attributes`` mapping is descriptive read state only; the tool never
    returns a handle or token that could be used to mutate the resource.
    """

    aws_account_id: str
    resource_type: str
    resource_id: str
    attributes: Mapping[str, object]

    def __post_init__(self) -> None:
        for name in ("aws_account_id", "resource_type", "resource_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.attributes, Mapping):
            raise TypeError("attributes must be a mapping")

    def to_dict(self) -> dict[str, object]:
        return {
            "aws_account_id": self.aws_account_id,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "attributes": dict(self.attributes),
        }


class AwsResourceTool(Protocol):
    """Read-only operations required to inspect Customer AWS Actual state."""

    def read_resource(self, query: AwsResourceQuery) -> AwsResourceView:
        """Return one resource for a READ_RESOURCE query within tool scope."""
        ...

    def list_resources(self, query: AwsResourceQuery) -> Sequence[AwsResourceView]:
        """Return resources of a type for a LIST_RESOURCES query within scope."""
        ...


def require_read_operation(query: object, expected: AwsResourceOperation) -> AwsResourceQuery:
    """Validate a query object and require an exact read operation.

    Keeping this check in one place ensures every adapter enforces the same
    read-only boundary rather than trusting the caller to pass the right shape.
    """
    if not isinstance(query, AwsResourceQuery):
        raise TypeError("query must be an AwsResourceQuery")
    if query.operation is not expected:
        raise AwsResourceToolError(
            f"operation must be {expected.value}, got {query.operation.value}"
        )
    return query
