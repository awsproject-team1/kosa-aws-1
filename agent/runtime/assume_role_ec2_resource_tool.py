"""Code-only, read-only EC2 implementation of the AWS Resource Tool port.

The evaluated EC2 target is the instance. `EC2-EBS-ENCRYPT-001` and `EC2-SG-INGRESS-001`
are about state that lives on attached volumes and security groups, so this adapter
resolves those from the instance and returns them as part of the instance view. Reading
them here rather than exposing volumes and security groups as their own targets keeps one
violation on one coordinate: an unencrypted volume is a finding about the instance that
carries it, counted once.
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

EC2_INSTANCE_RESOURCE_TYPE = "AWS::EC2::Instance"

_NOT_FOUND_CODES = frozenset({"InvalidInstanceID.NotFound", "InvalidInstanceID.Malformed"})

# The instance fields the three EC2 Rules cite, and only those. `UserData`, key material,
# tags, instance type, and IAM profile are absent on purpose: they are not evidence for
# these Rules, and every extra field both widens what customer content reaches the model and
# gives the model state it may weigh into a judgement no Rule asked for.
_INSTANCE_FIELDS = (
    "InstanceId",
    "State",
    "SubnetId",
    "VpcId",
    "PublicIpAddress",
    "PublicDnsName",
)
#: `Association` carries the interface-level public IP, which is where a public address
#: attached after launch shows up.
_NETWORK_INTERFACE_FIELDS = ("NetworkInterfaceId", "SubnetId", "Association")
_VOLUME_FIELDS = ("VolumeId", "Encrypted", "KmsKeyId")
#: `EC2-SG-INGRESS-001` is about inbound access, so egress rules are not projected.
_SECURITY_GROUP_FIELDS = ("GroupId", "GroupName", "VpcId", "IpPermissions")


class Ec2Client(Protocol):
    def describe_instances(self, **kwargs: object) -> Mapping[str, object]: ...

    def describe_volumes(self, **kwargs: object) -> Mapping[str, object]: ...

    def describe_security_groups(self, **kwargs: object) -> Mapping[str, object]: ...


class AssumeRoleEc2ResourceTool(AwsResourceTool):
    """Read EC2 instance state through one approved Role ARN; no mutation API exists."""

    def __init__(
        self,
        *,
        customer_id: str,
        aws_account_id: str,
        role_arn: str,
        external_id: str,
        sts: object,
        ec2_client_factory: Callable[[Mapping[str, str]], Ec2Client],
        clock: Callable[[], float] = time,
    ) -> None:
        for name, value in (("customer_id", customer_id), ("aws_account_id", aws_account_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not callable(ec2_client_factory):
            raise TypeError("ec2_client_factory is required")
        self._customer_id, self._aws_account_id = customer_id, aws_account_id
        self._ec2_client_factory = ec2_client_factory
        self._session = AssumeRoleReadSession(
            role_arn=role_arn, external_id=external_id, sts=sts, clock=clock
        )

    def read_resource(self, query: AwsResourceQuery) -> AwsResourceView:
        query = require_read_operation(query, AwsResourceOperation.READ_RESOURCE)
        require_scope(query, customer_id=self._customer_id, aws_account_id=self._aws_account_id)
        _require_ec2(query)
        instance_id = query.resource_id or ""
        ec2 = self._ec2()
        instance = self._describe_instance(ec2, instance_id)
        return AwsResourceView(
            aws_account_id=self._aws_account_id,
            resource_type=query.resource_type,
            resource_id=instance_id,
            attributes={
                "instance": projected(instance, _INSTANCE_FIELDS),
                "network_interfaces": [
                    projected(interface, _NETWORK_INTERFACE_FIELDS)
                    for interface in _sequence(instance.get("NetworkInterfaces"))
                ],
                "volumes": self._describe_volumes(ec2, instance),
                "security_groups": self._describe_security_groups(ec2, instance),
            },
        )

    def list_resources(self, query: AwsResourceQuery) -> Sequence[AwsResourceView]:
        query = require_read_operation(query, AwsResourceOperation.LIST_RESOURCES)
        require_scope(query, customer_id=self._customer_id, aws_account_id=self._aws_account_id)
        _require_ec2(query)
        try:
            reservations = paginate(
                self._ec2().describe_instances, items_key="Reservations", token_argument="NextToken"
            )
        except AwsResourceToolError:
            raise
        except Exception:
            raise AwsResourceToolError("EC2 instance list failed") from None
        views = []
        for reservation in reservations:
            for instance in _sequence(reservation.get("Instances")):
                instance_id = instance.get("InstanceId")
                if not isinstance(instance_id, str) or not instance_id:
                    raise AwsResourceToolError("EC2 instance list is invalid")
                views.append(
                    self.read_resource(
                        AwsResourceQuery(
                            customer_id=self._customer_id,
                            aws_account_id=self._aws_account_id,
                            operation=AwsResourceOperation.READ_RESOURCE,
                            resource_type=query.resource_type,
                            resource_id=instance_id,
                        )
                    )
                )
        return tuple(views)

    def _describe_instance(self, ec2: Ec2Client, instance_id: str) -> Mapping[str, object]:
        try:
            reservations = ec2.describe_instances(InstanceIds=[instance_id]).get("Reservations", [])
        except Exception as error:
            if error_code(error) in _NOT_FOUND_CODES:
                raise AwsResourceNotFoundError("EC2 instance state was not found") from None
            raise AwsResourceToolError("EC2 instance read failed") from None
        for reservation in _sequence(reservations):
            for instance in _sequence(reservation.get("Instances")):
                if isinstance(instance, Mapping) and instance.get("InstanceId") == instance_id:
                    return instance
        raise AwsResourceNotFoundError("EC2 instance state was not found")

    def _describe_volumes(
        self, ec2: Ec2Client, instance: Mapping[str, object]
    ) -> list[dict[str, object]]:
        volume_ids = []
        for mapping in _sequence(instance.get("BlockDeviceMappings")):
            ebs = mapping.get("Ebs") if isinstance(mapping, Mapping) else None
            volume_id = ebs.get("VolumeId") if isinstance(ebs, Mapping) else None
            if isinstance(volume_id, str) and volume_id:
                volume_ids.append(volume_id)
        if not volume_ids:
            return []
        try:
            volumes = ec2.describe_volumes(VolumeIds=volume_ids).get("Volumes", [])
        except Exception:
            raise AwsResourceToolError("EC2 volume read failed") from None
        described = _sequence(volumes)
        _require_complete(
            requested=volume_ids,
            returned=[volume.get("VolumeId") for volume in described],
            message="EC2 volume read returned fewer volumes than the instance attaches",
        )
        return [projected(volume, _VOLUME_FIELDS) for volume in described]

    def _describe_security_groups(
        self, ec2: Ec2Client, instance: Mapping[str, object]
    ) -> list[dict[str, object]]:
        group_ids = []
        for group in _sequence(instance.get("SecurityGroups")):
            group_id = group.get("GroupId") if isinstance(group, Mapping) else None
            if isinstance(group_id, str) and group_id:
                group_ids.append(group_id)
        if not group_ids:
            return []
        try:
            groups = ec2.describe_security_groups(GroupIds=group_ids).get("SecurityGroups", [])
        except Exception:
            raise AwsResourceToolError("EC2 security group read failed") from None
        described = _sequence(groups)
        _require_complete(
            requested=group_ids,
            returned=[group.get("GroupId") for group in described],
            message="EC2 security group read returned fewer groups than the instance uses",
        )
        return [projected(group, _SECURITY_GROUP_FIELDS) for group in described]

    def _ec2(self) -> Ec2Client:
        return self._ec2_client_factory(self._session.credentials())


def _require_ec2(query: AwsResourceQuery) -> None:
    if query.resource_type != EC2_INSTANCE_RESOURCE_TYPE:
        raise AwsResourceToolError("EC2 adapter supports only AWS::EC2::Instance")


def _require_complete(*, requested: list[str], returned: list[object], message: str) -> None:
    """Refuse a partial read of the state a Rule is judged on.

    `EC2-EBS-ENCRYPT-001` reads "are the attached volumes encrypted?" from this evidence. If
    one volume is missing from the response, the remaining volumes can all be encrypted and
    the answer looks like `PASS` while the unencrypted one was simply never shown. Missing
    evidence must not be able to read as compliant evidence.
    """
    described = {value for value in returned if isinstance(value, str)}
    if not set(requested).issubset(described):
        raise AwsResourceToolError(message)


def _sequence(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]
