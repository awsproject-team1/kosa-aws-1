"""Unit tests for the read-only AWS Resource Tool boundary and mock adapter."""

import unittest

from agent.runtime import (
    AwsResourceNotFoundError,
    AwsResourceScopeError,
    AwsResourceTool,
    AwsResourceToolError,
    AwsResourceView,
    MockAwsResourceTool,
    require_read_operation,
    require_scope,
)
from packages.contracts import AwsResourceOperation, AwsResourceQuery

CUSTOMER_ID = "cust-001"
ACCOUNT_ID = "123456789012"


def build_tool() -> MockAwsResourceTool:
    return MockAwsResourceTool(
        customer_id=CUSTOMER_ID,
        aws_account_id=ACCOUNT_ID,
        resources=[
            AwsResourceView(
                aws_account_id=ACCOUNT_ID,
                resource_type="AWS::S3::Bucket",
                resource_id="logs-bucket",
                attributes={"public_access_block": True},
            ),
            AwsResourceView(
                aws_account_id=ACCOUNT_ID,
                resource_type="AWS::S3::Bucket",
                resource_id="assets-bucket",
                attributes={"public_access_block": False},
            ),
        ],
    )


def read_query(resource_id: str | None) -> AwsResourceQuery:
    return AwsResourceQuery(
        customer_id=CUSTOMER_ID,
        aws_account_id=ACCOUNT_ID,
        operation=AwsResourceOperation.READ_RESOURCE,
        resource_type="AWS::S3::Bucket",
        resource_id=resource_id,
    )


def list_query() -> AwsResourceQuery:
    return AwsResourceQuery(
        customer_id=CUSTOMER_ID,
        aws_account_id=ACCOUNT_ID,
        operation=AwsResourceOperation.LIST_RESOURCES,
        resource_type="AWS::S3::Bucket",
    )


class AwsResourceToolTest(unittest.TestCase):
    def test_read_resource_returns_the_scoped_view(self) -> None:
        tool = build_tool()

        view = tool.read_resource(read_query("logs-bucket"))

        self.assertEqual(view.resource_id, "logs-bucket")
        self.assertEqual(view.attributes["public_access_block"], True)

    def test_list_resources_returns_only_matching_type(self) -> None:
        tool = build_tool()

        views = tool.list_resources(list_query())

        self.assertEqual(
            {view.resource_id for view in views},
            {"logs-bucket", "assets-bucket"},
        )

    def test_read_resource_rejects_query_outside_customer_scope(self) -> None:
        tool = build_tool()
        other_customer = AwsResourceQuery(
            customer_id="cust-999",
            aws_account_id=ACCOUNT_ID,
            operation=AwsResourceOperation.READ_RESOURCE,
            resource_type="AWS::S3::Bucket",
            resource_id="logs-bucket",
        )

        with self.assertRaises(AwsResourceScopeError):
            tool.read_resource(other_customer)

    def test_read_resource_rejects_query_outside_account_scope(self) -> None:
        tool = build_tool()
        other_account = AwsResourceQuery(
            customer_id=CUSTOMER_ID,
            aws_account_id="210987654321",
            operation=AwsResourceOperation.READ_RESOURCE,
            resource_type="AWS::S3::Bucket",
            resource_id="logs-bucket",
        )

        with self.assertRaises(AwsResourceScopeError):
            tool.read_resource(other_account)

    def test_read_resource_raises_for_unknown_resource(self) -> None:
        tool = build_tool()

        with self.assertRaises(AwsResourceNotFoundError):
            tool.read_resource(read_query("missing-bucket"))

    def test_read_resource_rejects_a_list_operation_query(self) -> None:
        tool = build_tool()

        with self.assertRaises(AwsResourceToolError):
            tool.read_resource(list_query())

    def test_list_resources_rejects_a_read_operation_query(self) -> None:
        tool = build_tool()

        with self.assertRaises(AwsResourceToolError):
            tool.list_resources(read_query("logs-bucket"))

    def test_only_read_operations_exist_in_the_contract(self) -> None:
        # Freeze the read-only boundary: the tool cannot express a write.
        self.assertEqual(
            {operation.value for operation in AwsResourceOperation},
            {"READ_RESOURCE", "LIST_RESOURCES"},
        )

    def test_require_read_operation_rejects_non_query_objects(self) -> None:
        with self.assertRaises(TypeError):
            require_read_operation(object(), AwsResourceOperation.READ_RESOURCE)

    def test_resource_view_round_trips_to_dict(self) -> None:
        view = AwsResourceView(
            aws_account_id=ACCOUNT_ID,
            resource_type="AWS::S3::Bucket",
            resource_id="logs-bucket",
            attributes={"public_access_block": True},
        )

        self.assertEqual(
            view.to_dict(),
            {
                "aws_account_id": ACCOUNT_ID,
                "resource_type": "AWS::S3::Bucket",
                "resource_id": "logs-bucket",
                "attributes": {"public_access_block": True},
            },
        )

    def test_resource_view_isolates_attributes_from_caller_mutation(self) -> None:
        source = {"public_access_block": True}
        view = AwsResourceView(
            aws_account_id=ACCOUNT_ID,
            resource_type="AWS::S3::Bucket",
            resource_id="logs-bucket",
            attributes=source,
        )

        source["public_access_block"] = False

        self.assertEqual(view.attributes["public_access_block"], True)

    def test_resource_view_attributes_reject_item_mutation(self) -> None:
        view = AwsResourceView(
            aws_account_id=ACCOUNT_ID,
            resource_type="AWS::S3::Bucket",
            resource_id="logs-bucket",
            attributes={"public_access_block": True},
        )

        with self.assertRaises(TypeError):
            view.attributes["injected"] = "x"  # type: ignore[index]

    def test_tool_rejects_resource_view_from_a_different_account(self) -> None:
        with self.assertRaises(ValueError):
            MockAwsResourceTool(
                customer_id=CUSTOMER_ID,
                aws_account_id=ACCOUNT_ID,
                resources=[
                    AwsResourceView(
                        aws_account_id="210987654321",
                        resource_type="AWS::S3::Bucket",
                        resource_id="logs-bucket",
                        attributes={},
                    )
                ],
            )

    def test_resource_view_freezes_nested_mapping(self) -> None:
        source = {"policy": {"public": True}, "tags": ["a", "b"]}
        view = AwsResourceView(
            aws_account_id=ACCOUNT_ID,
            resource_type="AWS::S3::Bucket",
            resource_id="logs-bucket",
            attributes=source,
        )

        # Mutating the nested source must not leak into the frozen view.
        source["policy"]["public"] = False
        source["tags"].append("c")

        self.assertEqual(view.attributes["policy"]["public"], True)
        self.assertEqual(view.attributes["tags"], ("a", "b"))

    def test_resource_view_rejects_nested_item_mutation(self) -> None:
        view = AwsResourceView(
            aws_account_id=ACCOUNT_ID,
            resource_type="AWS::S3::Bucket",
            resource_id="logs-bucket",
            attributes={"policy": {"public": True}},
        )

        with self.assertRaises(TypeError):
            view.attributes["policy"]["public"] = False  # type: ignore[index]

    def test_to_dict_returns_a_mutable_nested_copy(self) -> None:
        view = AwsResourceView(
            aws_account_id=ACCOUNT_ID,
            resource_type="AWS::S3::Bucket",
            resource_id="logs-bucket",
            attributes={"policy": {"public": True}, "tags": ["a"]},
        )

        data = view.to_dict()
        # Serializable plain types, and mutating the copy must not affect the view.
        data["attributes"]["policy"]["public"] = False
        data["attributes"]["tags"].append("b")

        self.assertIsInstance(data["attributes"], dict)
        self.assertIsInstance(data["attributes"]["policy"], dict)
        self.assertIsInstance(data["attributes"]["tags"], list)
        self.assertEqual(view.attributes["policy"]["public"], True)
        self.assertEqual(view.attributes["tags"], ("a",))

    def test_list_query_rejects_a_stray_resource_id(self) -> None:
        stray = AwsResourceQuery(
            customer_id=CUSTOMER_ID,
            aws_account_id=ACCOUNT_ID,
            operation=AwsResourceOperation.LIST_RESOURCES,
            resource_type="AWS::S3::Bucket",
            resource_id="logs-bucket",
        )

        with self.assertRaisesRegex(AwsResourceToolError, "must not carry a resource_id"):
            require_read_operation(stray, AwsResourceOperation.LIST_RESOURCES)

    def test_list_resources_rejects_a_stray_resource_id_through_the_tool(self) -> None:
        tool = build_tool()
        stray = AwsResourceQuery(
            customer_id=CUSTOMER_ID,
            aws_account_id=ACCOUNT_ID,
            operation=AwsResourceOperation.LIST_RESOURCES,
            resource_type="AWS::S3::Bucket",
            resource_id="logs-bucket",
        )

        with self.assertRaises(AwsResourceToolError):
            tool.list_resources(stray)

    def test_tool_rejects_duplicate_resource_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate resource"):
            MockAwsResourceTool(
                customer_id=CUSTOMER_ID,
                aws_account_id=ACCOUNT_ID,
                resources=[
                    AwsResourceView(
                        aws_account_id=ACCOUNT_ID,
                        resource_type="AWS::S3::Bucket",
                        resource_id="logs-bucket",
                        attributes={"public_access_block": True},
                    ),
                    AwsResourceView(
                        aws_account_id=ACCOUNT_ID,
                        resource_type="AWS::S3::Bucket",
                        resource_id="logs-bucket",
                        attributes={"public_access_block": False},
                    ),
                ],
            )

    def test_mock_satisfies_the_tool_port(self) -> None:
        tool = build_tool()

        self.assertIsInstance(tool, AwsResourceTool)

    def test_require_scope_rejects_out_of_scope_query(self) -> None:
        other = AwsResourceQuery(
            customer_id="cust-999",
            aws_account_id=ACCOUNT_ID,
            operation=AwsResourceOperation.READ_RESOURCE,
            resource_type="AWS::S3::Bucket",
            resource_id="logs-bucket",
        )

        with self.assertRaises(AwsResourceScopeError):
            require_scope(other, customer_id=CUSTOMER_ID, aws_account_id=ACCOUNT_ID)


if __name__ == "__main__":
    unittest.main()
