"""AssumeRole S3 adapter remains scoped and read-only without AWS credentials."""

import unittest

from agent.runtime import AssumeRoleS3ResourceTool
from packages.contracts import AwsResourceOperation, AwsResourceQuery


class Sts:
    def assume_role(self, **kwargs):
        return {
            "Credentials": {
                "AccessKeyId": "key",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
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
        tool = AssumeRoleS3ResourceTool(
            customer_id="cust-001",
            aws_account_id="123456789012",
            role_arn="arn:aws:iam::123456789012:role/read",
            sts=Sts(),
            s3_client_factory=lambda credentials: S3(),
        )
        view = tool.read_resource(query(AwsResourceOperation.READ_RESOURCE, "logs-bucket"))
        views = tool.list_resources(query(AwsResourceOperation.LIST_RESOURCES))
        self.assertTrue(view.attributes["public_access_block"]["BlockPublicAcls"])
        self.assertEqual([item.resource_id for item in views], ["logs-bucket"])
