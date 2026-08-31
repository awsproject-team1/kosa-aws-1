"""AssumeRole S3 adapter remains scoped and read-only without AWS credentials."""

import unittest
from datetime import UTC, datetime, timedelta

from agent.runtime import AssumeRoleS3ResourceTool
from packages.contracts import AwsResourceOperation, AwsResourceQuery


class Sts:
    def __init__(self):
        self.calls = []

    def assume_role(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "Credentials": {
                "AccessKeyId": "key",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": datetime.now(UTC) + timedelta(minutes=10),
            }
        }


class S3:
    def list_buckets(self):
        return {"Buckets": [{"Name": "logs-bucket"}]}

    def get_public_access_block(self, **kwargs):
        return {"PublicAccessBlockConfiguration": {"BlockPublicAcls": True}}

    def get_bucket_encryption(self, **kwargs):
        return {"ServerSideEncryptionConfiguration": {"Rules": []}}

    def get_bucket_policy_status(self, **kwargs):
        return {"PolicyStatus": {"IsPublic": False}}


def query(operation, resource_id=None):
    return AwsResourceQuery(
        customer_id="cust-001",
        aws_account_id="123456789012",
        operation=operation,
        resource_type="AWS::S3::Bucket",
        resource_id=resource_id,
    )


class AssumeRoleS3ResourceToolTest(unittest.TestCase):
    def test_reads_and_lists_s3_through_injected_assume_role_clients(self):
        sts = Sts()
        tool = AssumeRoleS3ResourceTool(
            customer_id="cust-001",
            aws_account_id="123456789012",
            role_arn="arn:aws:iam::123456789012:role/read",
            external_id="random-customer-bound-external-id",
            sts=sts,
            s3_client_factory=lambda credentials: S3(),
        )
        view = tool.read_resource(query(AwsResourceOperation.READ_RESOURCE, "logs-bucket"))
        views = tool.list_resources(query(AwsResourceOperation.LIST_RESOURCES))
        self.assertTrue(view.attributes["public_access_block"]["BlockPublicAcls"])
        self.assertEqual([item.resource_id for item in views], ["logs-bucket"])
        self.assertEqual(len(sts.calls), 1)
        self.assertEqual(sts.calls[0]["ExternalId"], "random-customer-bound-external-id")

    def test_refreshes_expired_credentials(self):
        now = [1000.0]

        class ExpiringSts(Sts):
            def assume_role(self, **kwargs):
                response = super().assume_role(**kwargs)
                response["Credentials"]["Expiration"] = now[0] + 61
                return response

        sts = ExpiringSts()
        tool = AssumeRoleS3ResourceTool(
            customer_id="cust-001",
            aws_account_id="123456789012",
            role_arn="arn:aws:iam::123456789012:role/read",
            external_id="random-customer-bound-external-id",
            sts=sts,
            s3_client_factory=lambda credentials: S3(),
            clock=lambda: now[0],
        )
        tool.read_resource(query(AwsResourceOperation.READ_RESOURCE, "logs-bucket"))
        now[0] += 2
        tool.read_resource(query(AwsResourceOperation.READ_RESOURCE, "logs-bucket"))
        self.assertEqual(len(sts.calls), 2)
