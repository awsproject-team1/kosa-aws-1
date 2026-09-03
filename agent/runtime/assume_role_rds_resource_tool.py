"""Code-only, read-only RDS implementation of the AWS Resource Tool port.

`describe_db_instances` alone carries the state all four RDS Rules cite (public access,
network/auth restriction, storage encryption, exported log types), so this adapter makes
one call per resource. The declared DB instance identifier is the resource id, which is
also what the Terraform plan projection reads from `aws_db_instance.identifier`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from time import time
from typing import Protocol

from agent.runtime.assume_role_session import (
    AssumeRoleReadSession,
    error_code,
    paginate,
    projected,
)
from agent.runtime.aws_resource_tool import (
    AwsResourceNotFoundError,
    AwsResourceTool,
    AwsResourceToolError,
    AwsResourceView,
    require_read_operation,
    require_scope,
)
from packages.contracts import AwsResourceOperation, AwsResourceQuery

RDS_INSTANCE_RESOURCE_TYPE = "AWS::RDS::DBInstance"

_NOT_FOUND_CODES = frozenset({"DBInstanceNotFound", "DBInstanceNotFoundFault"})

# The fields the four RDS Rules cite, and only those. `MasterUsername`, `Endpoint`, and tag
# values are absent because they are not evidence and would move customer connection detail
# into stored evidence; backup/MultiAZ/deletion-protection state is absent because no Rule
# here asks about it and an evaluator should not be given state to weigh that no Rule cites.
_DB_INSTANCE_FIELDS = (
    "DBInstanceIdentifier",
    "DBInstanceStatus",
    "Engine",
    "PubliclyAccessible",
    "StorageEncrypted",
    "KmsKeyId",
    "IAMDatabaseAuthenticationEnabled",
    "EnabledCloudwatchLogsExports",
)
_SUBNET_GROUP_FIELDS = ("DBSubnetGroupName", "VpcId", "SubnetGroupStatus")
_VPC_SECURITY_GROUP_FIELDS = ("VpcSecurityGroupId", "Status")


class RdsClient(Protocol):
    def describe_db_instances(self, **kwargs: object) -> Mapping[str, object]: ...


class AssumeRoleRdsResourceTool(AwsResourceTool):
    """Read RDS DB instance state through one approved Role ARN; no mutation API exists."""

    def __init__(
        self,
        *,
        customer_id: str,
        aws_account_id: str,
        role_arn: str,
        external_id: str,
        sts: object,
        rds_client_factory: Callable[[Mapping[str, str]], RdsClient],
        clock: Callable[[], float] = time,
    ) -> None:
        for name, value in (("customer_id", customer_id), ("aws_account_id", aws_account_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not callable(rds_client_factory):
            raise TypeError("rds_client_factory is required")
        self._customer_id, self._aws_account_id = customer_id, aws_account_id
        self._rds_client_factory = rds_client_factory
        self._session = AssumeRoleReadSession(
            role_arn=role_arn, external_id=external_id, sts=sts, clock=clock
        )

    def read_resource(self, query: AwsResourceQuery) -> AwsResourceView:
        query = require_read_operation(query, AwsResourceOperation.READ_RESOURCE)
        require_scope(query, customer_id=self._customer_id, aws_account_id=self._aws_account_id)
        _require_rds(query)
        identifier = query.resource_id or ""
        try:
            instances = self._rds().describe_db_instances(DBInstanceIdentifier=identifier)
        except Exception as error:
            if error_code(error) in _NOT_FOUND_CODES:
                raise AwsResourceNotFoundError("RDS DB instance state was not found") from None
            raise AwsResourceToolError("RDS DB instance read failed") from None
        for instance in _sequence(instances.get("DBInstances")):
            if instance.get("DBInstanceIdentifier") != identifier:
                continue
            return AwsResourceView(
                aws_account_id=self._aws_account_id,
                resource_type=query.resource_type,
                resource_id=identifier,
                attributes=_attributes(instance),
            )
        raise AwsResourceNotFoundError("RDS DB instance state was not found")

    def list_resources(self, query: AwsResourceQuery) -> Sequence[AwsResourceView]:
        query = require_read_operation(query, AwsResourceOperation.LIST_RESOURCES)
        require_scope(query, customer_id=self._customer_id, aws_account_id=self._aws_account_id)
        _require_rds(query)
        try:
            instances = paginate(
                self._rds().describe_db_instances,
                items_key="DBInstances",
                token_argument="Marker",
            )
        except AwsResourceToolError:
            raise
        except Exception:
            raise AwsResourceToolError("RDS DB instance list failed") from None
        views = []
        for instance in instances:
            identifier = instance.get("DBInstanceIdentifier")
            if not isinstance(identifier, str) or not identifier:
                raise AwsResourceToolError("RDS DB instance list is invalid")
            views.append(
                AwsResourceView(
                    aws_account_id=self._aws_account_id,
                    resource_type=query.resource_type,
                    resource_id=identifier,
                    attributes=_attributes(instance),
                )
            )
        return tuple(views)

    def _rds(self) -> RdsClient:
        return self._rds_client_factory(self._session.credentials())


def _attributes(instance: Mapping[str, object]) -> dict[str, object]:
    return {
        "db_instance": projected(instance, _DB_INSTANCE_FIELDS),
        "db_subnet_group": projected(instance.get("DBSubnetGroup"), _SUBNET_GROUP_FIELDS),
        "vpc_security_groups": [
            projected(group, _VPC_SECURITY_GROUP_FIELDS)
            for group in _sequence(instance.get("VpcSecurityGroups"))
        ],
    }


def _require_rds(query: AwsResourceQuery) -> None:
    if query.resource_type != RDS_INSTANCE_RESOURCE_TYPE:
        raise AwsResourceToolError("RDS adapter supports only AWS::RDS::DBInstance")


def _sequence(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]
