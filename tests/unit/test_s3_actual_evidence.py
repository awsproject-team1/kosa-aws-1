"""M1 C consumes the read-only AWS Resource Tool for S3 Actual evidence."""

import unittest

from agent.runtime import AwsResourceView, MockAwsResourceTool
from apps.backend.assessment import S3ActualEvidenceLoader, S3EvidenceError


class WrongViewTool:
    def read_resource(self, query):
        return AwsResourceView(
            aws_account_id="999999999999",
            resource_type="AWS::S3::Bucket",
            resource_id="other-bucket",
            attributes={},
        )

    def list_resources(self, query):
        return ()


class S3ActualEvidenceLoaderTest(unittest.TestCase):
    def test_loads_a_scoped_bucket_using_only_the_read_only_tool(self) -> None:
        loader = S3ActualEvidenceLoader(
            tool=MockAwsResourceTool(
                customer_id="cust-001",
                aws_account_id="123456789012",
                resources=(
                    AwsResourceView(
                        aws_account_id="123456789012",
                        resource_type="AWS::S3::Bucket",
                        resource_id="logs-bucket",
                        attributes={"public_access_block": False},
                    ),
                ),
            ),
            customer_id="cust-001",
            aws_account_id="123456789012",
        )

        evidence = loader.load("logs-bucket")

        self.assertEqual(evidence.resource_document["resource_id"], "logs-bucket")
        self.assertEqual(evidence.evidence_references, ("aws:s3:bucket/logs-bucket#read-resource",))

    def test_rejects_a_tool_response_outside_the_requested_s3_scope(self) -> None:
        loader = S3ActualEvidenceLoader(
            tool=WrongViewTool(), customer_id="cust-001", aws_account_id="123456789012"
        )

        with self.assertRaisesRegex(S3EvidenceError, "outside the S3 query scope"):
            loader.load("logs-bucket")
