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

    def get_bucket_policy(self, **kwargs):
        raise NoSuch("NoSuchBucketPolicy")

    def get_bucket_ownership_controls(self, **kwargs):
        raise NoSuch("OwnershipControlsNotFoundError")

    def get_bucket_logging(self, **kwargs):
        return {}


class NoSuch(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


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

    def test_an_unconfigured_setting_is_projected_as_a_value_not_an_absence(self):
        """ "설정 없음"은 사실이고 "읽지 못함"은 근거 부족이다. 둘을 같은 모양으로 두지 않는다."""
        tool = AssumeRoleS3ResourceTool(
            customer_id="cust-001",
            aws_account_id="123456789012",
            role_arn="arn:aws:iam::123456789012:role/read",
            external_id="random-customer-bound-external-id",
            sts=Sts(),
            s3_client_factory=lambda credentials: S3(),
        )
        view = tool.read_resource(query(AwsResourceOperation.READ_RESOURCE, "logs-bucket"))
        self.assertEqual(
            view.to_dict()["attributes"]["bucket_policy"], {"present": False, "document": None}
        )
        self.assertEqual(
            view.to_dict()["attributes"]["ownership_controls"],
            {"ObjectOwnership": "ObjectWriter", "configured": False},
        )
        self.assertEqual(
            view.to_dict()["attributes"]["logging"],
            {"enabled": False, "target_bucket": None, "target_prefix": None},
        )

    def test_a_configured_bucket_projects_the_parsed_policy_ownership_and_logging(self):
        class Configured(S3):
            def get_bucket_policy(self, **kwargs):
                return {"Policy": '{"Version":"2012-10-17","Statement":[]}'}

            def get_bucket_ownership_controls(self, **kwargs):
                return {
                    "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}
                }

            def get_bucket_logging(self, **kwargs):
                return {"LoggingEnabled": {"TargetBucket": "logs", "TargetPrefix": "p/"}}

        tool = AssumeRoleS3ResourceTool(
            customer_id="cust-001",
            aws_account_id="123456789012",
            role_arn="arn:aws:iam::123456789012:role/read",
            external_id="random-customer-bound-external-id",
            sts=Sts(),
            s3_client_factory=lambda credentials: Configured(),
        )
        view = tool.read_resource(query(AwsResourceOperation.READ_RESOURCE, "logs-bucket"))
        self.assertEqual(
            view.to_dict()["attributes"]["bucket_policy"],
            {"present": True, "document": {"Version": "2012-10-17", "Statement": []}},
        )
        self.assertEqual(
            view.to_dict()["attributes"]["ownership_controls"],
            {"ObjectOwnership": "BucketOwnerEnforced", "configured": True},
        )
        self.assertEqual(
            view.to_dict()["attributes"]["logging"],
            {"enabled": True, "target_bucket": "logs", "target_prefix": "p/"},
        )

    def test_a_denied_sub_read_is_a_failure_not_an_absence(self):
        """권한 하나 빠진 계정이 전부 위반 또는 전부 준수로 보이면 안 된다."""
        from agent.runtime import AwsResourceToolError

        class Denied(S3):
            def get_bucket_logging(self, **kwargs):
                raise NoSuch("AccessDenied")

        tool = AssumeRoleS3ResourceTool(
            customer_id="cust-001",
            aws_account_id="123456789012",
            role_arn="arn:aws:iam::123456789012:role/read",
            external_id="random-customer-bound-external-id",
            sts=Sts(),
            s3_client_factory=lambda credentials: Denied(),
        )
        with self.assertRaises(AwsResourceToolError):
            tool.read_resource(query(AwsResourceOperation.READ_RESOURCE, "logs-bucket"))

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
