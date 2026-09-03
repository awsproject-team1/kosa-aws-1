"""Route a read-only AWS Resource query to the adapter that owns its resource type.

The Assessment runtime now evaluates more than one resource type, but the read boundary
stays one port. This composite holds an explicit resource type → adapter map and refuses
anything outside it.

Refusing is the whole point. If an unmapped type fell through to some default adapter, or
returned an empty view, a Rule added for a type nobody wired would produce "no violations"
— indistinguishable from a compliant resource. The registered map is the single place that
answers "which types can this deployment actually read?".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from agent.runtime.aws_resource_tool import (
    AwsResourceTool,
    AwsResourceToolError,
    AwsResourceView,
)
from packages.contracts import AwsResourceQuery


class ResourceTypeRoutingAwsResourceTool(AwsResourceTool):
    """Dispatch READ_RESOURCE/LIST_RESOURCES by `resource_type` over an allow-list."""

    def __init__(self, adapters: Mapping[str, AwsResourceTool]) -> None:
        if not isinstance(adapters, Mapping) or not adapters:
            raise ValueError("adapters must be a non-empty mapping of resource type to tool")
        for resource_type, adapter in adapters.items():
            if not isinstance(resource_type, str) or not resource_type.strip():
                raise ValueError("adapter keys must be non-empty resource type strings")
            if not isinstance(adapter, AwsResourceTool):
                raise TypeError(f"adapter for {resource_type!r} must implement AwsResourceTool")
        self._adapters = dict(adapters)

    @property
    def resource_types(self) -> tuple[str, ...]:
        """The resource types this deployment can actually read, in registration order."""
        return tuple(self._adapters)

    def supports(self, resource_type: object) -> bool:
        return isinstance(resource_type, str) and resource_type in self._adapters

    def read_resource(self, query: AwsResourceQuery) -> AwsResourceView:
        return self._adapter(query).read_resource(query)

    def list_resources(self, query: AwsResourceQuery) -> Sequence[AwsResourceView]:
        return self._adapter(query).list_resources(query)

    def _adapter(self, query: AwsResourceQuery) -> AwsResourceTool:
        if not isinstance(query, AwsResourceQuery):
            raise TypeError("query must be an AwsResourceQuery")
        adapter = self._adapters.get(query.resource_type)
        if adapter is None:
            # The unsupported type is named because it comes from server-side
            # configuration and the Policy Registry, never from customer input.
            raise AwsResourceToolError(
                f"no read adapter is configured for resource type {query.resource_type!r}"
            )
        return adapter
