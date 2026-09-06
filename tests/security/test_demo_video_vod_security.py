"""Security and deployment-boundary checks for the separate classroom VOD stack."""

import unittest
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

ROOT = Path(__file__).parents[2]
TEMPLATE = ROOT / "infrastructure/cloudformation/demo-video-vod.yaml"
WORKFLOW = ROOT / ".github/workflows/deploy-demo-video.yml"


def _construct_intrinsic(loader: yaml.SafeLoader, _suffix: str, node: yaml.Node) -> object:
    if isinstance(node, ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, MappingNode):
        return loader.construct_mapping(node)
    raise TypeError(f"Unsupported CloudFormation node: {type(node).__name__}")


yaml.SafeLoader.add_multi_constructor("!", _construct_intrinsic)


class DemoVideoVodSecurityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
        cls.resources = cls.template["Resources"]

    def test_source_and_output_buckets_are_private_versioned_and_retained(self) -> None:
        for logical_id in ("SourceBucket", "OutputBucket"):
            with self.subTest(logical_id=logical_id):
                bucket = self.resources[logical_id]
                self.assertEqual(bucket["DeletionPolicy"], "Retain")
                self.assertEqual(bucket["UpdateReplacePolicy"], "Retain")
                properties = bucket["Properties"]
                self.assertEqual(properties["VersioningConfiguration"]["Status"], "Enabled")
                self.assertEqual(
                    properties["PublicAccessBlockConfiguration"],
                    {
                        "BlockPublicAcls": True,
                        "BlockPublicPolicy": True,
                        "IgnorePublicAcls": True,
                        "RestrictPublicBuckets": True,
                    },
                )

    def test_both_bucket_policies_reject_insecure_transport(self) -> None:
        for logical_id in ("SourceBucketPolicy", "OutputBucketPolicy"):
            with self.subTest(logical_id=logical_id):
                statements = self.resources[logical_id]["Properties"]["PolicyDocument"]["Statement"]
                deny = next(statement for statement in statements if statement["Effect"] == "Deny")
                self.assertEqual(deny["Action"], "s3:*")
                self.assertEqual(deny["Condition"], {"Bool": {"aws:SecureTransport": "false"}})

    def test_cloudfront_uses_signed_oac_and_output_policy_is_distribution_scoped(self) -> None:
        oac = self.resources["VideoOriginAccessControl"]["Properties"]["OriginAccessControlConfig"]
        self.assertEqual(oac["SigningBehavior"], "always")
        self.assertEqual(oac["SigningProtocol"], "sigv4")
        statements = self.resources["OutputBucketPolicy"]["Properties"]["PolicyDocument"][
            "Statement"
        ]
        allow = next(statement for statement in statements if statement["Effect"] == "Allow")
        self.assertEqual(allow["Principal"], {"Service": "cloudfront.amazonaws.com"})
        self.assertIn(
            "distribution/${VideoDistribution}", allow["Condition"]["StringEquals"]["AWS:SourceArn"]
        )

    def test_mediaconvert_role_cannot_write_outside_video_output_prefix(self) -> None:
        statements = self.resources["MediaConvertRole"]["Properties"]["Policies"][0][
            "PolicyDocument"
        ]["Statement"]
        write = next(statement for statement in statements if "s3:PutObject" in statement["Action"])
        self.assertEqual(write["Resource"], "${OutputBucket.Arn}/videos/*")

    def test_submit_lambda_passes_only_the_exact_mediaconvert_role(self) -> None:
        statements = self.resources["SubmitJobRole"]["Properties"]["Policies"][0]["PolicyDocument"][
            "Statement"
        ]
        pass_role = next(
            statement for statement in statements if statement["Action"] == "iam:PassRole"
        )
        self.assertEqual(pass_role["Resource"], "MediaConvertRole.Arn")
        self.assertEqual(
            pass_role["Condition"],
            {"StringEquals": {"iam:PassedToService": "mediaconvert.amazonaws.com"}},
        )

    def test_deploy_workflow_pins_actions_and_exact_stack_name(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('test "${STACK_NAME}" = "kosa-governance-demo-video"', workflow)
        self.assertIn("actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09", workflow)
        self.assertIn(
            "aws-actions/configure-aws-credentials@e6de054238d6b7531b4efff3b6587d9aade6a06c",
            workflow,
        )
        self.assertNotIn("s3 cp GovLens.mp4", workflow)


if __name__ == "__main__":
    unittest.main()
