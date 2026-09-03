"""Code-only, read-only RDS implementation of the AWS Resource Tool port.

`describe_db_instances` carries the instance state for public access, encryption, and log
exports. `RDS-ACCESS-001` also needs the ingress rules of every attached VPC security group;
a group id alone says nothing about which networks or ports it allows. This adapter therefore
resolves those memberships through EC2 and refuses a partial group response.
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
_SECURITY_GROUP_FIELDS = ("GroupId", "GroupName", "VpcId", "IpPermissions")
_SECURITY_GROUP_BATCH_SIZE = 100


class RdsClient(Protocol):
    def describe_db_instances(self, **kwargs: object) -> Mapping[str, object]: ...


class Ec2SecurityGroupClient(Protocol):
    def describe_security_groups(self, **kwargs: object) -> Mapping[str, object]: ...


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
        ec2_client_factory: Callable[[Mapping[str, str]], Ec2SecurityGroupClient],
        clock: Callable[[], float] = time,
    ) -> None:
        for name, value in (("customer_id", customer_id), ("aws_account_id", aws_account_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not callable(rds_client_factory) or not callable(ec2_client_factory):
            raise TypeError("rds_client_factory and ec2_client_factory are required")
        self._customer_id, self._aws_account_id = customer_id, aws_account_id
        self._rds_client_factory = rds_client_factory
        self._ec2_client_factory = ec2_client_factory
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
            groups = self._security_groups_for((instance,))
            return AwsResourceView(
                aws_account_id=self._aws_account_id,
                resource_type=query.resource_type,
                resource_id=identifier,
                attributes=_attributes(instance, groups),
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
        groups = self._security_groups_for(tuple(instances))
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
                    attributes=_attributes(instance, groups),
                )
            )
        return tuple(views)

    def _rds(self) -> RdsClient:
        return self._rds_client_factory(self._session.credentials())

    def _ec2(self) -> Ec2SecurityGroupClient:
        return self._ec2_client_factory(self._session.credentials())

    def _security_groups_for(
        self, instances: tuple[Mapping[str, object], ...]
    ) -> dict[str, Mapping[str, object]]:
        """Read every attached security group, failing instead of returning a partial view."""
        group_ids: list[str] = []
        for instance in instances:
            for membership in _sequence(instance.get("VpcSecurityGroups")):
                group_id = membership.get("VpcSecurityGroupId")
                if isinstance(group_id, str) and group_id and group_id not in group_ids:
                    group_ids.append(group_id)
        if not group_ids:
            return {}
        described: dict[str, Mapping[str, object]] = {}
        ec2 = self._ec2()
        try:
            for offset in range(0, len(group_ids), _SECURITY_GROUP_BATCH_SIZE):
                response = ec2.describe_security_groups(
                    GroupIds=group_ids[offset : offset + _SECURITY_GROUP_BATCH_SIZE]
                )
                for group in _sequence(response.get("SecurityGroups")):
                    group_id = group.get("GroupId")
                    if isinstance(group_id, str) and group_id:
                        described[group_id] = group
        except Exception:
            raise AwsResourceToolError("RDS security group read failed") from None
        if not set(group_ids).issubset(described):
            raise AwsResourceToolError(
                "RDS security group read returned fewer groups than the DB instances use"
            )
        return described


def _attributes(
    instance: Mapping[str, object], security_groups: Mapping[str, Mapping[str, object]]
) -> dict[str, object]:
    return {
        "db_instance": projected(instance, _DB_INSTANCE_FIELDS),
        "db_subnet_group": projected(instance.get("DBSubnetGroup"), _SUBNET_GROUP_FIELDS),
        "vpc_security_groups": _attached_security_groups(instance, security_groups),
    }


def _attached_security_groups(
    instance: Mapping[str, object], security_groups: Mapping[str, Mapping[str, object]]
) -> list[dict[str, object]]:
    attached: list[dict[str, object]] = []
    for membership in _sequence(instance.get("VpcSecurityGroups")):
        group_id = membership.get("VpcSecurityGroupId")
        if not isinstance(group_id, str) or group_id not in security_groups:
            continue
        attached.append(
            {
                **projected(membership, _VPC_SECURITY_GROUP_FIELDS),
                **projected(security_groups[group_id], _SECURITY_GROUP_FIELDS),
            }
        )
    return attached


def _require_rds(query: AwsResourceQuery) -> None:
    if query.resource_type != RDS_INSTANCE_RESOURCE_TYPE:
        raise AwsResourceToolError("RDS adapter supports only AWS::RDS::DBInstance")


def _sequence(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]
