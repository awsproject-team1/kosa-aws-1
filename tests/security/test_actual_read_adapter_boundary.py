"""Every Actual-state read adapter stays read-only and inside one customer/account scope.

The resource expansion tripled the number of adapters reaching into a customer account.
These regressions hold the two axes ADR-0007 defines — read-only and scope — across all of
them, so a new resource type cannot arrive with a weaker boundary than S3 had.
"""

import inspect
import unittest

from agent.runtime import (
    ALB_RESOURCE_TYPE,
    EC2_INSTANCE_RESOURCE_TYPE,
    RDS_INSTANCE_RESOURCE_TYPE,
    S3_RESOURCE_TYPE,
    AssumeRoleAlbResourceTool,
    AssumeRoleEc2ResourceTool,
    AssumeRoleRdsResourceTool,
    AssumeRoleS3ResourceTool,
    AwsResourceScopeError,
    AwsResourceToolError,
)
from packages.contracts import AwsResourceOperation, AwsResourceQuery

CUSTOMER = "cust-001"
ACCOUNT = "123456789012"

#: Verbs an AWS mutation would need. None of them may appear on a read adapter, on the
#: service client Protocol it declares, or in the SDK calls it makes.
_MUTATION_VERBS = (
    "create_",
    "delete_",
    "put_",
    "update_",
    "modify_",
    "attach_",
    "detach_",
    "authorize_",
    "revoke_",
    "reboot_",
    "terminate_",
    "start_",
    "stop_",
    "run_",
    "set_",
    "tag_",
    "untag_",
)

_ADAPTERS = (
    (AssumeRoleS3ResourceTool, "s3_client_factory", S3_RESOURCE_TYPE),
    (AssumeRoleEc2ResourceTool, "ec2_client_factory", EC2_INSTANCE_RESOURCE_TYPE),
    (AssumeRoleRdsResourceTool, "rds_client_factory", RDS_INSTANCE_RESOURCE_TYPE),
    (AssumeRoleAlbResourceTool, "elbv2_client_factory", ALB_RESOURCE_TYPE),
)


class Sts:
    def assume_role(self, **kwargs):
        raise AssertionError("no read in these tests should reach STS")


class RefusingClient:
    """Any AWS call is a failure: these tests must be refused before the SDK is touched."""

    def __getattr__(self, name):
        raise AssertionError(f"adapter called {name} before enforcing its boundary")


def _tool(adapter, factory_name):
    factories = {factory_name: lambda credentials: RefusingClient()}
    if adapter is AssumeRoleRdsResourceTool:
        factories["ec2_client_factory"] = lambda credentials: RefusingClient()
    return adapter(
        customer_id=CUSTOMER,
        aws_account_id=ACCOUNT,
        role_arn=f"arn:aws:iam::{ACCOUNT}:role/read",
        external_id="random-customer-bound-external-id",
        sts=Sts(),
        **factories,
    )


class ActualReadAdapterBoundaryTest(unittest.TestCase):
    def test_no_adapter_exposes_a_mutating_method(self) -> None:
        for adapter, _, _ in _ADAPTERS:
            public = [name for name in dir(adapter) if not name.startswith("_")]
            with self.subTest(adapter=adapter.__name__):
                self.assertEqual(sorted(public), ["list_resources", "read_resource"])

    def test_no_declared_service_client_protocol_can_express_a_mutation(self) -> None:
        """어댑터가 선언한 client Protocol에 변경 API가 없으면 주입으로도 쓸 수 없다."""
        for adapter, _, _ in _ADAPTERS:
            module = inspect.getmodule(adapter)
            assert module is not None
            for name, member in vars(module).items():
                if not inspect.isclass(member) or not name.endswith("Client"):
                    continue
                for method in dir(member):
                    with self.subTest(adapter=adapter.__name__, protocol=name, method=method):
                        self.assertFalse(method.startswith(_MUTATION_VERBS))

    def test_no_adapter_source_calls_a_mutating_sdk_operation(self) -> None:
        for adapter, _, _ in _ADAPTERS:
            source = inspect.getsource(inspect.getmodule(adapter))
            for verb in _MUTATION_VERBS:
                with self.subTest(adapter=adapter.__name__, verb=verb):
                    self.assertNotIn(f".{verb}", source)

    def test_every_adapter_refuses_another_customers_query(self) -> None:
        for adapter, factory_name, resource_type in _ADAPTERS:
            with self.subTest(adapter=adapter.__name__):
                with self.assertRaises(AwsResourceScopeError):
                    _tool(adapter, factory_name).read_resource(
                        AwsResourceQuery(
                            customer_id="cust-999",
                            aws_account_id=ACCOUNT,
                            operation=AwsResourceOperation.READ_RESOURCE,
                            resource_type=resource_type,
                            resource_id="resource",
                        )
                    )

    def test_every_adapter_refuses_another_accounts_query(self) -> None:
        for adapter, factory_name, resource_type in _ADAPTERS:
            with self.subTest(adapter=adapter.__name__):
                with self.assertRaises(AwsResourceScopeError):
                    _tool(adapter, factory_name).read_resource(
                        AwsResourceQuery(
                            customer_id=CUSTOMER,
                            aws_account_id="999999999999",
                            operation=AwsResourceOperation.READ_RESOURCE,
                            resource_type=resource_type,
                            resource_id="resource",
                        )
                    )

    def test_every_adapter_refuses_a_resource_type_it_does_not_own(self) -> None:
        """한 어댑터가 남의 type을 대신 답하면 근거 문서와 Rule 집합이 어긋난다."""
        for adapter, factory_name, owned in _ADAPTERS:
            for other in (
                S3_RESOURCE_TYPE,
                EC2_INSTANCE_RESOURCE_TYPE,
                RDS_INSTANCE_RESOURCE_TYPE,
                ALB_RESOURCE_TYPE,
            ):
                if other == owned:
                    continue
                with self.subTest(adapter=adapter.__name__, resource_type=other):
                    with self.assertRaises(AwsResourceToolError):
                        _tool(adapter, factory_name).read_resource(
                            AwsResourceQuery(
                                customer_id=CUSTOMER,
                                aws_account_id=ACCOUNT,
                                operation=AwsResourceOperation.READ_RESOURCE,
                                resource_type=other,
                                resource_id="resource",
                            )
                        )

    def test_every_adapter_refuses_a_list_query_carrying_a_resource_id(self) -> None:
        for adapter, factory_name, resource_type in _ADAPTERS:
            with self.subTest(adapter=adapter.__name__):
                with self.assertRaises(AwsResourceToolError):
                    _tool(adapter, factory_name).list_resources(
                        AwsResourceQuery(
                            customer_id=CUSTOMER,
                            aws_account_id=ACCOUNT,
                            operation=AwsResourceOperation.READ_RESOURCE,
                            resource_type=resource_type,
                            resource_id="resource",
                        )
                    )


if __name__ == "__main__":
    unittest.main()
