"""Semantic security checks for the customer-operated M1 bootstrap template."""

import unittest
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

TEMPLATE_PATH = (
    Path(__file__).parents[2] / "infrastructure/cloudformation/m1-customer-bootstrap.yaml"
)


def _construct_intrinsic(loader: yaml.SafeLoader, _suffix: str, node: yaml.Node) -> object:
    if isinstance(node, ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, MappingNode):
        return loader.construct_mapping(node)
    raise TypeError(f"Unsupported CloudFormation node: {type(node).__name__}")


yaml.SafeLoader.add_multi_constructor("!", _construct_intrinsic)


def _template() -> dict[str, object]:
    loaded = yaml.safe_load(TEMPLATE_PATH.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise TypeError("CloudFormation template must be a mapping")
    return loaded


class CustomerBootstrapSecurityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.resources = _template()["Resources"]

    def test_lambda_code_bucket_is_private_versioned_and_retained(self) -> None:
        bucket = self.resources["LambdaCodeBucket"]
        self.assertEqual(bucket["DeletionPolicy"], "Retain")
        self.assertEqual(bucket["UpdateReplacePolicy"], "Retain")
        properties = bucket["Properties"]
        self.assertEqual(properties["VersioningConfiguration"]["Status"], "Enabled")
        self.assertEqual(
            properties["OwnershipControls"]["Rules"][0]["ObjectOwnership"],
            "BucketOwnerEnforced",
        )
        self.assertEqual(
            properties["PublicAccessBlockConfiguration"],
            {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
        )

    def test_oidc_trust_requires_exact_environment_subjects_and_sts_audience(self) -> None:
        role = self.resources["GitHubActionsDeploymentRole"]
        statement = role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
        self.assertEqual(statement["Action"], "sts:AssumeRoleWithWebIdentity")
        condition = statement["Condition"]["StringEquals"]
        self.assertEqual(condition["token.actions.githubusercontent.com:aud"], "sts.amazonaws.com")
        subjects = condition["token.actions.githubusercontent.com:sub"]
        self.assertEqual(len(subjects), 2)
        self.assertTrue(all("environment:" in subject for subject in subjects))

    def test_github_role_cannot_assume_runtime_roles_and_passes_only_execution_role(self) -> None:
        policies = self.resources["GitHubActionsDeploymentRole"]["Properties"]["Policies"]
        statements = policies[0]["PolicyDocument"]["Statement"]
        actions = [action for statement in statements for action in statement["Action"]]
        self.assertNotIn("sts:AssumeRole", actions)
        pass_role = next(
            statement for statement in statements if statement["Action"] == "iam:PassRole"
        )
        self.assertEqual(pass_role["Resource"], "FoundationExecutionRole.Arn")
        self.assertEqual(
            pass_role["Condition"],
            {"StringEquals": {"iam:PassedToService": "cloudformation.amazonaws.com"}},
        )

    def test_github_role_can_reverify_the_exact_artifact_version(self) -> None:
        policies = self.resources["GitHubActionsDeploymentRole"]["Properties"]["Policies"]
        statements = policies[0]["PolicyDocument"]["Statement"]
        artifact_read = next(
            statement for statement in statements if "s3:GetObject" in statement["Action"]
        )
        self.assertIn("s3:GetObjectVersion", artifact_read["Action"])
        self.assertEqual(artifact_read["Resource"], "${LambdaCodeBucket.Arn}/lambda/m0/*")

    def test_github_role_can_read_scoped_deployment_diagnostics(self) -> None:
        policies = self.resources["GitHubActionsDeploymentRole"]["Properties"]["Policies"]
        statements = policies[0]["PolicyDocument"]["Statement"]
        cloudformation_read = next(
            statement
            for statement in statements
            if "cloudformation:DescribeChangeSet" in statement["Action"]
        )
        self.assertIn("cloudformation:DescribeStackEvents", cloudformation_read["Action"])
        self.assertIn("cloudformation:ListChangeSets", cloudformation_read["Action"])
        self.assertEqual(
            cloudformation_read["Resource"],
            "arn:${AWS::Partition}:cloudformation:${AWS::Region}:${AWS::AccountId}:stack/${FoundationStackName}/*",
        )
