"""Semantic security checks for the canonical M0 CloudFormation template."""

import unittest
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

TEMPLATE_PATH = Path(__file__).parents[2] / "infrastructure/cloudformation/m0-foundation.yaml"


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


def _properties(resource: dict[str, object]) -> dict[str, object]:
    value = resource["Properties"]
    if not isinstance(value, dict):
        raise TypeError("CloudFormation resource properties must be a mapping")
    return value


class CloudFormationSecurityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        resources = _template()["Resources"]
        if not isinstance(resources, dict):
            raise TypeError("CloudFormation resources must be a mapping")
        cls.resources = resources

    def test_metadata_table_is_protected_and_retained(self) -> None:
        table = self.resources["MetadataTable"]
        self.assertEqual(table["DeletionPolicy"], "Retain")
        self.assertEqual(table["UpdateReplacePolicy"], "Retain")
        self.assertTrue(_properties(table)["DeletionProtectionEnabled"])

    def test_artifact_bucket_enforces_private_versioned_encrypted_storage(self) -> None:
        bucket = self.resources["ArtifactBucket"]
        properties = _properties(bucket)
        self.assertEqual(bucket["DeletionPolicy"], "Retain")
        self.assertEqual(bucket["UpdateReplacePolicy"], "Retain")
        self.assertEqual(
            properties["BucketEncryption"]["ServerSideEncryptionConfiguration"][0][
                "ServerSideEncryptionByDefault"
            ]["SSEAlgorithm"],
            "AES256",
        )
        self.assertEqual(
            properties["OwnershipControls"]["Rules"][0]["ObjectOwnership"],
            "BucketOwnerEnforced",
        )
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

    def test_artifact_bucket_policy_denies_non_tls_bucket_and_object_access(self) -> None:
        policy = self.resources["ArtifactBucketPolicy"]
        statement = _properties(policy)["PolicyDocument"]["Statement"]
        self.assertEqual(policy["DeletionPolicy"], "Retain")
        self.assertEqual(policy["UpdateReplacePolicy"], "Retain")
        self.assertEqual(len(statement), 1)
        deny = statement[0]
        self.assertEqual(deny["Effect"], "Deny")
        self.assertEqual(deny["Principal"], "*")
        self.assertEqual(deny["Action"], "s3:*")
        self.assertEqual(deny["Condition"], {"Bool": {"aws:SecureTransport": "false"}})
        self.assertEqual(len(deny["Resource"]), 2)

    def test_artifact_access_trail_records_only_artifact_s3_data_events(self) -> None:
        trail = self.resources["ArtifactAccessTrail"]
        properties = _properties(trail)
        self.assertEqual(trail["DeletionPolicy"], "Retain")
        self.assertEqual(trail["UpdateReplacePolicy"], "Retain")
        self.assertEqual(trail["DependsOn"], "ArtifactAuditLogBucketPolicy")
        self.assertTrue(properties["IsLogging"])
        self.assertTrue(properties["EnableLogFileValidation"])
        self.assertFalse(properties["IncludeGlobalServiceEvents"])
        self.assertFalse(properties["IsMultiRegionTrail"])
        selectors = properties["EventSelectors"]
        self.assertEqual(len(selectors), 1)
        selector = selectors[0]
        self.assertFalse(selector["IncludeManagementEvents"])
        self.assertEqual(selector["ReadWriteType"], "All")
        self.assertEqual(
            selector["DataResources"],
            [{"Type": "AWS::S3::Object", "Values": ["${ArtifactBucket.Arn}/"]}],
        )
        self.assertEqual(properties["S3BucketName"], "ArtifactAuditLogBucket")

    def test_audit_destination_is_hardened_and_cloudtrail_delivery_is_limited(self) -> None:
        bucket = self.resources["ArtifactAuditLogBucket"]
        properties = _properties(bucket)
        self.assertEqual(bucket["DeletionPolicy"], "Retain")
        self.assertEqual(bucket["UpdateReplacePolicy"], "Retain")
        self.assertEqual(
            properties["OwnershipControls"]["Rules"][0]["ObjectOwnership"],
            "BucketOwnerEnforced",
        )
        self.assertEqual(properties["VersioningConfiguration"]["Status"], "Enabled")
        self.assertEqual(
            properties["BucketEncryption"]["ServerSideEncryptionConfiguration"][0][
                "ServerSideEncryptionByDefault"
            ]["SSEAlgorithm"],
            "AES256",
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
        audit_policy = self.resources["ArtifactAuditLogBucketPolicy"]
        self.assertEqual(audit_policy["DeletionPolicy"], "Retain")
        self.assertEqual(audit_policy["UpdateReplacePolicy"], "Retain")
        statements = _properties(audit_policy)["PolicyDocument"]["Statement"]
        allowed = {
            statement["Sid"]: statement
            for statement in statements
            if statement["Effect"] == "Allow"
        }
        self.assertEqual(set(allowed), {"AllowCloudTrailGetBucketAcl", "AllowCloudTrailWrite"})
        expected_trail_arn = (
            "arn:${AWS::Partition}:cloudtrail:${AWS::Region}:${AWS::AccountId}:trail/"
            "${ProjectName}-${Environment}-artifact-access"
        )
        self.assertEqual(
            allowed["AllowCloudTrailGetBucketAcl"],
            {
                "Sid": "AllowCloudTrailGetBucketAcl",
                "Effect": "Allow",
                "Principal": {"Service": "cloudtrail.amazonaws.com"},
                "Action": "s3:GetBucketAcl",
                "Resource": "ArtifactAuditLogBucket.Arn",
                "Condition": {"StringEquals": {"aws:SourceArn": expected_trail_arn}},
            },
        )
        self.assertEqual(
            allowed["AllowCloudTrailWrite"],
            {
                "Sid": "AllowCloudTrailWrite",
                "Effect": "Allow",
                "Principal": {"Service": "cloudtrail.amazonaws.com"},
                "Action": "s3:PutObject",
                "Resource": "${ArtifactAuditLogBucket.Arn}/AWSLogs/${AWS::AccountId}/*",
                "Condition": {
                    "StringEquals": {
                        "s3:x-amz-acl": "bucket-owner-full-control",
                        "aws:SourceArn": expected_trail_arn,
                    }
                },
            },
        )
        tls_denies = [
            statement for statement in statements if statement["Sid"] == "DenyInsecureTransport"
        ]
        self.assertEqual(len(tls_denies), 1)
        self.assertEqual(tls_denies[0]["Condition"], {"Bool": {"aws:SecureTransport": "false"}})

    def test_m0_worker_role_has_no_shared_artifact_bucket_access(self) -> None:
        role_properties = _properties(self.resources["WorkflowRuntimeRole"])
        policies = role_properties["Policies"]
        statements = [
            statement for policy in policies for statement in policy["PolicyDocument"]["Statement"]
        ]
        flattened_actions = [
            action
            for statement in statements
            for action in (
                statement["Action"]
                if isinstance(statement["Action"], list)
                else [statement["Action"]]
            )
        ]
        self.assertFalse(
            any(
                isinstance(action, str) and action.lower().startswith("s3:")
                for action in flattened_actions
            )
        )
        self.assertFalse(self._contains_artifact_bucket_reference(role_properties))

    @staticmethod
    def _contains_artifact_bucket_reference(value: object) -> bool:
        if isinstance(value, dict):
            return any(
                CloudFormationSecurityTest._contains_artifact_bucket_reference(item)
                for item in value.values()
            )
        if isinstance(value, list):
            return any(
                CloudFormationSecurityTest._contains_artifact_bucket_reference(item)
                for item in value
            )
        return isinstance(value, str) and "ArtifactBucket" in value


if __name__ == "__main__":
    unittest.main()
