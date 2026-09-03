"""M1 C consumes the read-only AWS Resource Tool for Actual evidence of every scoped type."""

import unittest

from agent.runtime import AwsResourceView, MockAwsResourceTool
from apps.backend.assessment import (
    SUPPORTED_ACTUAL_RESOURCE_TYPES,
    ActualEvidenceError,
    ActualEvidenceLoader,
    actual_evidence_reference,
)

CUSTOMER = "cust-001"
ACCOUNT = "123456789012"
ALB_ARN = (
    "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/demo/50dc6c495c0c9188"
)


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


def _loader(*, resource_type, resource_id, attributes):
    return ActualEvidenceLoader(
        tool=MockAwsResourceTool(
            customer_id=CUSTOMER,
            aws_account_id=ACCOUNT,
            resources=(
                AwsResourceView(
                    aws_account_id=ACCOUNT,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    attributes=attributes,
                ),
            ),
        ),
        customer_id=CUSTOMER,
        aws_account_id=ACCOUNT,
        resource_type=resource_type,
    )


class ActualEvidenceLoaderTest(unittest.TestCase):
    def test_loads_a_scoped_bucket_using_only_the_read_only_tool(self) -> None:
        loader = _loader(
            resource_type="AWS::S3::Bucket",
            resource_id="logs-bucket",
            attributes={"public_access_block": False},
        )

        evidence = loader.load("logs-bucket")

        self.assertEqual(evidence.resource_document["resource_id"], "logs-bucket")
        self.assertEqual(evidence.evidence_references, ("aws:s3:bucket/logs-bucket#read-resource",))

    def test_rejects_a_tool_response_outside_the_requested_s3_scope(self) -> None:
        loader = ActualEvidenceLoader(
            tool=WrongViewTool(),
            customer_id=CUSTOMER,
            aws_account_id=ACCOUNT,
            resource_type="AWS::S3::Bucket",
        )

        with self.assertRaisesRegex(ActualEvidenceError, "outside the S3 query scope"):
            loader.load("logs-bucket")

    def test_loads_each_scoped_resource_type_with_its_own_evidence_namespace(self) -> None:
        expected = {
            "AWS::S3::Bucket": ("logs-bucket", "aws:s3:bucket/logs-bucket#read-resource"),
            "AWS::EC2::Instance": (
                "i-0123456789abcdef0",
                "aws:ec2:instance/i-0123456789abcdef0#read-resource",
            ),
            "AWS::RDS::DBInstance": (
                "demo-db-001",
                "aws:rds:db-instance/demo-db-001#read-resource",
            ),
            "AWS::ElasticLoadBalancingV2::LoadBalancer": (
                ALB_ARN,
                # Only the ARN's resource part: the namespace and the customer scope already
                # fix service, region, and account.
                "aws:elasticloadbalancing:loadbalancer/app/demo/50dc6c495c0c9188#read-resource",
            ),
        }

        self.assertEqual(set(expected), set(SUPPORTED_ACTUAL_RESOURCE_TYPES))
        for resource_type, (resource_id, reference) in expected.items():
            with self.subTest(resource_type=resource_type):
                loader = _loader(
                    resource_type=resource_type,
                    resource_id=resource_id,
                    attributes={"state": "read"},
                )

                evidence = loader.load(resource_id)

                self.assertEqual(evidence.resource_type, resource_type)
                self.assertEqual(evidence.evidence_references, (reference,))
                self.assertEqual(evidence.resource_document["resource_type"], resource_type)

    def test_rejects_a_resource_type_without_a_reviewed_evidence_scope(self) -> None:
        """Actual 근거 어휘가 없는 type은 loader를 만들 수 없다 (fail-closed)."""
        with self.assertRaisesRegex(ActualEvidenceError, "no Actual evidence scope"):
            ActualEvidenceLoader(
                tool=WrongViewTool(),
                customer_id=CUSTOMER,
                aws_account_id=ACCOUNT,
                resource_type="AWS::DynamoDB::Table",
            )
        with self.assertRaisesRegex(ActualEvidenceError, "no Actual evidence scope"):
            actual_evidence_reference("AWS::DynamoDB::Table", "table-1")

    def test_requires_the_alb_resource_id_to_be_an_arn(self) -> None:
        """ALB의 resource_id는 ARN이며, ARN이 아니면 locator를 지어내지 않는다."""
        with self.assertRaisesRegex(ActualEvidenceError, "must be a load balancer ARN"):
            actual_evidence_reference("AWS::ElasticLoadBalancingV2::LoadBalancer", "demo-alb-name")

    def test_rejects_a_view_of_a_different_type_than_the_loader_asked_for(self) -> None:
        loader = ActualEvidenceLoader(
            tool=WrongViewTool(),
            customer_id=CUSTOMER,
            aws_account_id=ACCOUNT,
            resource_type="AWS::EC2::Instance",
        )

        with self.assertRaisesRegex(ActualEvidenceError, "outside the EC2 query scope"):
            loader.load("i-0123456789abcdef0")


if __name__ == "__main__":
    unittest.main()
