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
            statement
            for policy in policies
            for policy_document in [
                policy["PolicyDocument"]
                if isinstance(policy, dict)
                else policy[1]["PolicyDocument"]
            ]
            for statement in _policy_statements(policy_document["Statement"])
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

    def test_fixture_mode_omits_the_m1_input_policy_instead_of_creating_an_empty_policy(
        self,
    ) -> None:
        role_properties = _properties(self.resources["WorkflowRuntimeRole"])
        policies = role_properties["Policies"]
        live_policy_condition = policies[1]
        self.assertEqual(live_policy_condition[0], "M1LiveAssessmentEnabled")
        self.assertEqual(live_policy_condition[1]["PolicyName"], "M1ReadOnlyAssessmentInputs")
        self.assertEqual(live_policy_condition[2], "AWS::NoValue")
        statements = live_policy_condition[1]["PolicyDocument"]["Statement"]
        self.assertEqual(len(statements), 3)

    def test_api_runtime_can_dispatch_deployment_work(self) -> None:
        """The API composition root requires this exact queue URL at cold start."""
        function = self.resources["ApiRuntimeFunction"]
        variables = _properties(function)["Environment"]["Variables"]
        self.assertEqual(variables["DEPLOYMENT_QUEUE_URL"], "DeploymentQueue")

    def test_deployment_http_routes_are_explicitly_jwt_protected(self) -> None:
        """Handler branches are unreachable unless API Gateway declares each route."""
        expected = {
            "PostRemediationDeploymentsRoute": "POST /remediations/{remediationId}/deployments",
            "GetDeploymentRoute": "GET /deployments/{deploymentId}",
            "GetDeploymentVerificationRoute": "GET /deployments/{deploymentId}/verification",
            "PostDeploymentRejectRoute": "POST /deployments/{deploymentId}/reject",
            # 감사 이력 조회도 같은 JWT authorizer 뒤에 있어야 한다. handler가 Admin 권한을
            # 검사하더라도, route가 authorizer 없이 선언되면 인증 없는 호출이 handler까지 닿는다.
            "GetAuditEventsRoute": "GET /audit-events",
        }
        for name, route_key in expected.items():
            route = _properties(self.resources[name])
            self.assertEqual(route["RouteKey"], route_key)
            self.assertEqual(route["AuthorizationType"], "JWT")
            self.assertEqual(route["AuthorizerId"], "HttpApiAuthorizer")

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


def _policy_statements(value: object) -> list[dict[str, object]]:
    """Expand both branches of a static CloudFormation policy `Fn::If`."""
    if isinstance(value, list):
        return [statement for statement in value if isinstance(statement, dict)]
    if not isinstance(value, dict):
        return []
    branches = value.get("Fn::If")
    if not isinstance(branches, list) or len(branches) != 3:
        return []
    return [statement for branch in branches[1:] for statement in _policy_statements(branch)]


class DeploymentArtifactSecurityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = _template()
        workflow_path = Path(__file__).parents[2] / ".github/workflows/deploy-m0-foundation.yml"
        workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        if not isinstance(workflow, dict):
            raise TypeError("Deployment workflow must be a mapping")
        cls.workflow = workflow
        cls.prepare_job = workflow["jobs"]["prepare-artifact"]
        cls.deploy_job = workflow["jobs"]["deploy"]
        cls.prepare_steps = {step["name"]: step for step in cls.prepare_job["steps"]}
        cls.deploy_steps = {step["name"]: step for step in cls.deploy_job["steps"]}
        runbook_path = (
            Path(__file__).parents[2]
            / "infrastructure/parameters/m0-foundation-sandbox-deployment-runbook.md"
        )
        cls.runbook = runbook_path.read_text(encoding="utf-8")
        package_script_path = Path(__file__).parents[2] / "scripts/package-m0-lambda.sh"
        cls.package_script = package_script_path.read_text(encoding="utf-8")
        package_workflow_path = (
            Path(__file__).parents[2] / ".github/workflows/m0-lambda-package.yml"
        )
        cls.package_workflow = package_workflow_path.read_text(encoding="utf-8")

    def test_lambda_functions_pin_the_uploaded_s3_object_version(self) -> None:
        parameters = self.template["Parameters"]
        self.assertIn("LambdaCodeS3ObjectVersion", parameters)
        self.assertEqual(parameters["LambdaCodeS3ObjectVersion"]["MinLength"], 1)
        functions = {
            name: resource
            for name, resource in self.template["Resources"].items()
            if resource["Type"] == "AWS::Lambda::Function"
        }
        self.assertEqual(
            set(functions),
            {"ApiRuntimeFunction", "OutboxSweeperFunction", "AssessmentWorkerFunction"},
        )
        for function in functions.values():
            self.assertEqual(
                _properties(function)["Code"],
                {
                    "S3Bucket": "LambdaCodeS3Bucket",
                    "S3Key": "LambdaCodeS3Key",
                    "S3ObjectVersion": "LambdaCodeS3ObjectVersion",
                },
            )

    def test_lambda_package_is_deterministic_before_hashing(self) -> None:
        for required in (
            "source_files = sorted(",
            "ZIP_STORED",
            "date_time=(1980, 1, 1, 0, 0, 0)",
            "entry.external_attr = 0o100644 << 16",
        ):
            self.assertIn(required, self.package_script)
        self.assertIn("m0-lambda-first.zip", self.package_workflow)
        self.assertIn("m0-lambda-second.zip", self.package_workflow)
        self.assertIn('cmp -- "${RUNNER_TEMP}/m0-lambda-first.zip"', self.package_workflow)

    def test_exact_artifact_binding_has_a_second_human_gate(self) -> None:
        inputs = self.workflow["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["artifact_approval_environment"]["required"], "true")
        self.assertEqual(self.prepare_job["environment"], "${{ inputs.environment }}")
        self.assertEqual(self.deploy_job["needs"], "prepare-artifact")
        self.assertEqual(
            self.deploy_job["environment"],
            "${{ inputs.artifact_approval_environment }}",
        )
        self.assertIn(
            'test "${ARTIFACT_APPROVAL_ENVIRONMENT}" != "${DEPLOYMENT_ENVIRONMENT}"',
            self.prepare_steps["Validate protected artifact inputs"]["run"],
        )
        self.assertEqual(
            self.deploy_job["env"]["LAMBDA_CODE_S3_OBJECT_VERSION"],
            "${{ needs.prepare-artifact.outputs.lambda_code_s3_object_version }}",
        )
        self.assertIn("awaiting deployment approval", self._artifact_binding_script())

    def test_both_jobs_enforce_the_protected_account(self) -> None:
        for job, steps, configure_name, validation_name in (
            (
                self.prepare_job,
                self.prepare_steps,
                "Configure customer artifact credentials",
                "Validate protected artifact inputs",
            ),
            (
                self.deploy_job,
                self.deploy_steps,
                "Configure customer deployment credentials",
                "Validate protected deployment inputs",
            ),
        ):
            self.assertEqual(
                job["env"]["EXPECTED_AWS_ACCOUNT_ID"],
                "${{ vars.EXPECTED_AWS_ACCOUNT_ID }}",
            )
            configure = steps[configure_name]
            self.assertEqual(
                configure["with"]["allowed-account-ids"],
                "${{ vars.EXPECTED_AWS_ACCOUNT_ID }}",
            )
            self.assertEqual(configure["with"]["mask-aws-account-id"], "true")
            validation = steps[validation_name]["run"]
            self.assertIn('[[ "${EXPECTED_AWS_ACCOUNT_ID}" =~ ^[0-9]{12}$ ]]', validation)
            self.assertIn(
                "^arn:[^:]+:iam::${EXPECTED_AWS_ACCOUNT_ID}:role/.+$",
                validation,
            )

        prepare_boundary = self.prepare_steps["Verify artifact account and bucket"]["run"]
        self.assertIn("aws sts get-caller-identity", prepare_boundary)
        self.assertIn(
            'test "${actual_account_id}" = "${EXPECTED_AWS_ACCOUNT_ID}"',
            prepare_boundary,
        )
        self.assertIn(
            '--expected-bucket-owner "${EXPECTED_AWS_ACCOUNT_ID}"',
            prepare_boundary,
        )
        deploy_boundary = self.deploy_steps["Reverify approved account and artifact version"]["run"]
        self.assertIn("aws sts get-caller-identity", deploy_boundary)
        self.assertIn(
            'test "${actual_account_id}" = "${EXPECTED_AWS_ACCOUNT_ID}"',
            deploy_boundary,
        )
        self.assertIn(
            '--expected-bucket-owner "${EXPECTED_AWS_ACCOUNT_ID}"',
            deploy_boundary,
        )

    def test_artifact_creation_and_matching_rerun_are_fail_closed(self) -> None:
        script = self._artifact_binding_script()
        for required in (
            "sha256sum",
            "openssl dgst -sha256 -binary",
            "aws s3api put-object",
            '--if-none-match "*"',
            '--expected-bucket-owner "${EXPECTED_AWS_ACCOUNT_ID}"',
            "--checksum-algorithm SHA256",
            "--checksum-sha256",
            '--metadata "commit-sha=${GITHUB_SHA},zip-sha256=${zip_sha256_hex}"',
            "aws s3api head-object",
            "--checksum-mode ENABLED",
            '.Metadata["commit-sha"] // empty',
            '.Metadata["zip-sha256"] // empty',
            ".VersionId // empty",
            ".ChecksumSHA256 // empty",
            'test -n "${object_version}"',
            'test "${returned_checksum}" = "${zip_sha256_base64}"',
            'test "${recorded_commit_sha}" = "${GITHUB_SHA}"',
            'test "${recorded_zip_sha256}" = "${zip_sha256_hex}"',
            "lambda_code_s3_object_version=",
            "lambda_zip_sha256=",
            "GITHUB_STEP_SUMMARY",
        ):
            self.assertIn(required, script)
        self.assertNotIn("aws s3 cp", script)

    def test_deploy_revalidates_the_approved_exact_version(self) -> None:
        reverify = self.deploy_steps["Reverify approved account and artifact version"]["run"]
        for required in (
            "aws s3api head-object",
            '--version-id "${LAMBDA_CODE_S3_OBJECT_VERSION}"',
            '--expected-bucket-owner "${EXPECTED_AWS_ACCOUNT_ID}"',
            "--checksum-mode ENABLED",
            '.Metadata["commit-sha"] // empty',
            '.Metadata["zip-sha256"] // empty',
            ".ChecksumSHA256 // empty",
            'test "$(jq -r',
            '"${GITHUB_SHA}"',
            '"${LAMBDA_ZIP_SHA256}"',
        ):
            self.assertIn(required, reverify)
        deploy = self.deploy_steps["Deploy approved CloudFormation artifact"]["run"]
        self.assertIn(
            '"LambdaCodeS3ObjectVersion=${LAMBDA_CODE_S3_OBJECT_VERSION}"',
            deploy,
        )

    def test_failed_deployments_emit_scoped_diagnostics(self) -> None:
        diagnostic = self.deploy_steps["Diagnose failed CloudFormation deployment"]
        self.assertEqual(diagnostic["if"], "failure()")
        script = diagnostic["run"]
        self.assertIn("aws cloudformation list-change-sets", script)
        self.assertIn("aws cloudformation describe-events", script)
        self.assertIn("--filters FailedEvents=true", script)
        self.assertIn("ValidationPath", script)
        self.assertIn("aws cloudformation describe-stack-events", script)
        self.assertNotIn("--include-property-values", script)

    def test_audit_metadata_listing_is_account_bound_and_not_paginated(self) -> None:
        self.assertEqual(self.runbook.count("aws s3api list-objects-v2"), 2)
        self.assertEqual(self.runbook.count("--max-keys 20"), 2)
        self.assertEqual(self.runbook.count("--no-paginate"), 2)
        self.assertGreaterEqual(
            self.runbook.count('--expected-bucket-owner "${ACCOUNT_ID}"'),
            4,
        )

    def _artifact_binding_script(self) -> str:
        return self.prepare_steps["Create or verify immutable Lambda artifact"]["run"]


if __name__ == "__main__":
    unittest.main()
