"""The one place that decides which AWS resource types this product can read.

Two runtimes need an Actual-state read tool: the Assessment Worker (to evaluate a
resource) and the Deployment Worker (to re-read Actual after apply, ADR-0020). Building
that tool twice invites the two to support different resource types, and a type that one
side can read but the other cannot produces a verification that silently skips it.

The builder registry below is therefore both the factory and the vocabulary:
`ACTUAL_READ_RESOURCE_TYPES` is derived from it, never restated. Anything that needs to
know "which resource types are readable?" — configuration validation, evidence locators,
deployment targets — reads it from here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Protocol

from agent.runtime.assume_role_alb_resource_tool import (
    ALB_RESOURCE_TYPE,
    AssumeRoleAlbResourceTool,
)
from agent.runtime.assume_role_ec2_resource_tool import (
    EC2_INSTANCE_RESOURCE_TYPE,
    AssumeRoleEc2ResourceTool,
)
from agent.runtime.assume_role_rds_resource_tool import (
    RDS_INSTANCE_RESOURCE_TYPE,
    AssumeRoleRdsResourceTool,
)
from agent.runtime.assume_role_s3_resource_tool import (
    S3_RESOURCE_TYPE,
    AssumeRoleS3ResourceTool,
)
from agent.runtime.aws_resource_tool import AwsResourceTool, AwsResourceToolError
from agent.runtime.resource_type_routing_tool import ResourceTypeRoutingAwsResourceTool


class ClientFactoryProvider(Protocol):
    """Return a credential-taking client factory for one AWS service name."""

    def __call__(self, service: str) -> Callable[[Mapping[str, str]], object]: ...


#: resource type → (adapter class, ((AWS service name, factory keyword), ...)).
#: RDS also reads the ingress rules of its attached VPC security groups; registering both
#: clients here keeps every SDK surface needed by one adapter explicit and read-only.
_ADAPTERS: dict[str, tuple[type[AwsResourceTool], tuple[tuple[str, str], ...]]] = {
    S3_RESOURCE_TYPE: (AssumeRoleS3ResourceTool, (("s3", "s3_client_factory"),)),
    EC2_INSTANCE_RESOURCE_TYPE: (
        AssumeRoleEc2ResourceTool,
        (("ec2", "ec2_client_factory"),),
    ),
    RDS_INSTANCE_RESOURCE_TYPE: (
        AssumeRoleRdsResourceTool,
        (("rds", "rds_client_factory"), ("ec2", "ec2_client_factory")),
    ),
    ALB_RESOURCE_TYPE: (AssumeRoleAlbResourceTool, (("elbv2", "elbv2_client_factory"),)),
}

#: Every resource type an Actual read adapter exists for, in registration order.
ACTUAL_READ_RESOURCE_TYPES: tuple[str, ...] = tuple(_ADAPTERS)


def aws_service_for(resource_type: str) -> str:
    """Return the AWS service name a resource type is read through."""
    try:
        return _ADAPTERS[resource_type][1][0][0]
    except KeyError:
        raise AwsResourceToolError(
            f"no Actual read adapter exists for resource type {resource_type!r}"
        ) from None


def build_actual_resource_tool(
    *,
    customer_id: str,
    aws_account_id: str,
    role_arn: str,
    external_id: str,
    resource_types: Iterable[str],
    client_factory_provider: ClientFactoryProvider,
    sts: object,
) -> ResourceTypeRoutingAwsResourceTool:
    """Build one read-only tool covering exactly the declared resource types.

    An adapter is created only for a declared type, so a deployment approved for S3 does
    not hold an EC2 read path it never uses. An unknown type is refused here rather than at
    read time: the configuration is wrong, and a Worker should not start and then fail
    halfway through a perspective set or a post-deploy re-read.
    """
    ordered: list[str] = []
    for resource_type in resource_types:
        if resource_type not in ordered:
            ordered.append(resource_type)
    if not ordered:
        raise AwsResourceToolError("at least one resource type must be declared")
    adapters: dict[str, AwsResourceTool] = {}
    for resource_type in ordered:
        adapter, client_factories = _adapter_for(resource_type)
        adapters[resource_type] = adapter(
            customer_id=customer_id,
            aws_account_id=aws_account_id,
            role_arn=role_arn,
            external_id=external_id,
            sts=sts,
            **{
                factory_keyword: client_factory_provider(service)
                for service, factory_keyword in client_factories
            },
        )
    return ResourceTypeRoutingAwsResourceTool(adapters)


def _adapter_for(
    resource_type: str,
) -> tuple[type[AwsResourceTool], tuple[tuple[str, str], ...]]:
    try:
        return _ADAPTERS[resource_type]
    except KeyError:
        raise AwsResourceToolError(
            f"no Actual read adapter exists for resource type {resource_type!r}"
        ) from None
