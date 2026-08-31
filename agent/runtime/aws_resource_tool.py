"""Read-only AWS Resource Tool boundary for D (Remediation/Deployment).

This module defines the provider-neutral port that the agent runtime uses to
read Customer AWS Actual state. Per ADR-0007 and ADR-0009 the tool exposes only
two read operations (READ_RESOURCE, LIST_RESOURCES); it cannot express a write
or mutation. Access is scoped to an approved (customer_id, aws_account_id) pair
and callers must not delegate that scope to policy or AI input.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, runtime_checkable

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
        # Recursively freeze so the promised immutability holds at every depth:
        # a top-level MappingProxyType still lets nested dict/list values mutate,
        # which would leak back into this view and later query results.
        object.__setattr__(self, "attributes", _deep_freeze(self.attributes))

    def to_dict(self) -> dict[str, object]:
        return {
            "aws_account_id": self.aws_account_id,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "attributes": _thaw(self.attributes),
        }


@runtime_checkable
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
    # LIST_RESOURCES targets a resource_type, not a single resource. Reject a
    # stray resource_id rather than silently ignoring it.
    if expected is AwsResourceOperation.LIST_RESOURCES and query.resource_id is not None:
        raise AwsResourceToolError("LIST_RESOURCES must not carry a resource_id")
    return query


def require_scope(
    query: AwsResourceQuery, *, customer_id: str, aws_account_id: str
) -> AwsResourceQuery:
    """Require a query to stay within one approved (customer, account) scope.

    ADR-0007 defines the boundary along two axes, read-only and scope. This
    shared guard enforces the scope axis so every adapter (mock or real SDK)
    applies the same check instead of relying on per-adapter convention.
    """
    if not isinstance(query, AwsResourceQuery):
        raise TypeError("query must be an AwsResourceQuery")
    if query.customer_id != customer_id or query.aws_account_id != aws_account_id:
        raise AwsResourceScopeError("query customer_id/aws_account_id is outside the tool scope")
    return query


def _deep_freeze(value: object) -> object:
    """Return a recursively read-only copy of a mapping/sequence value tree."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    # Treat non-string sequences as tuples; str/bytes are already immutable.
    if isinstance(value, (str, bytes)):
        return value
    if isinstance(value, Sequence):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    """Return a plain, mutable copy of a frozen value tree for serialization."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (str, bytes)):
        return value
    if isinstance(value, Sequence):
        return [_thaw(item) for item in value]
    return value
