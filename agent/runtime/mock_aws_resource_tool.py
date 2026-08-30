"""Deterministic in-memory AWS Resource Tool for Fixture/Mock development.

This adapter lets D and its consumers develop against the read-only AWS
boundary before the real IAM AssumeRole + SDK path exists. It holds a fixed set
of resource views for exactly one approved (customer_id, aws_account_id) scope
and refuses any query outside that scope. It has no write path by construction.
"""

from collections.abc import Iterable, Sequence

from agent.runtime.aws_resource_tool import (
    AwsResourceNotFoundError,
    AwsResourceScopeError,
    AwsResourceView,
    require_read_operation,
)
from packages.contracts import AwsResourceOperation, AwsResourceQuery


class MockAwsResourceTool:
    """Serve read-only resource views from a scoped, immutable snapshot."""

    def __init__(
        self,
        *,
        customer_id: str,
        aws_account_id: str,
        resources: Iterable[AwsResourceView],
    ) -> None:
        _require_non_empty_string(customer_id, "customer_id")
        _require_non_empty_string(aws_account_id, "aws_account_id")
        self._customer_id = customer_id
        self._aws_account_id = aws_account_id
        self._by_key: dict[tuple[str, str], AwsResourceView] = {}
        for view in resources:
            if not isinstance(view, AwsResourceView):
                raise TypeError("resources must contain AwsResourceView items")
            if view.aws_account_id != aws_account_id:
                raise ValueError("resource aws_account_id must match tool scope")
            self._by_key[(view.resource_type, view.resource_id)] = view

    def read_resource(self, query: AwsResourceQuery) -> AwsResourceView:
        """Return one resource for a READ_RESOURCE query within tool scope."""
        query = require_read_operation(query, AwsResourceOperation.READ_RESOURCE)
        self._require_scope(query)
        # The Contract guarantees resource_id is present for READ_RESOURCE.
        view = self._by_key.get((query.resource_type, query.resource_id or ""))
        if view is None:
            raise AwsResourceNotFoundError(
                f"no {query.resource_type} resource {query.resource_id!r} in scope"
            )
        return view

    def list_resources(self, query: AwsResourceQuery) -> Sequence[AwsResourceView]:
        """Return resources of a type for a LIST_RESOURCES query within scope."""
        query = require_read_operation(query, AwsResourceOperation.LIST_RESOURCES)
        self._require_scope(query)
        return tuple(
            view
            for (resource_type, _), view in self._by_key.items()
            if resource_type == query.resource_type
        )

    def _require_scope(self, query: AwsResourceQuery) -> None:
        if query.customer_id != self._customer_id or query.aws_account_id != self._aws_account_id:
            raise AwsResourceScopeError(
                "query customer_id/aws_account_id is outside the tool scope"
            )


def _require_non_empty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
