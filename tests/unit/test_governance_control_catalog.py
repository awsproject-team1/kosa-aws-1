"""The Governance Control Catalog must describe what the product can really do.

가장 중요한 검사는 하나다: **AWS capability가 선언한 `document_paths`는 실제 adapter가 만드는
projected document에 존재해야 한다.** 그 경로가 틀리면 Runtime의 pre-flight가 언제나 "근거 없음"을
보고하고, 위반 있음과 근거 없음이 구별되지 않는다. 그래서 이 파일은 경로를 손으로 적은 기대값과
비교하지 않고, 실제 adapter를 가짜 AWS 응답으로 돌려 나온 문서에 대고 확인한다.

IaC hint는 반대다. authoritative가 아니므로 존재 검증을 하지 않고, 대신 **authoritative처럼
쓰이지 않는지**를 검사한다.
"""

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent.runtime import (
    ALB_RESOURCE_TYPE,
    EC2_INSTANCE_RESOURCE_TYPE,
    RDS_INSTANCE_RESOURCE_TYPE,
    S3_RESOURCE_TYPE,
    AssumeRoleAlbResourceTool,
    AssumeRoleEc2ResourceTool,
    AssumeRoleRdsResourceTool,
    AssumeRoleS3ResourceTool,
)
from apps.backend.assessment.actual import ActualEvidenceLoader
from apps.backend.policy.control_catalog import (
    CONTROL_CATALOG_VERSION,
    GOVERNANCE_ASSESSMENT_RESOURCE_TYPE,
    LEGACY_RULE_CONTROL_KEYS,
    MANUAL_CONTROL_KEY,
    MVP_CONTROL_CATALOG,
    NOT_YET_SUPPORTED_CONTROL_KEY,
    manual_control,
)
from apps.backend.policy.evidence_paths import missing_document_paths
from apps.backend.policy.registry import load_rule_registry
from packages.contracts import (
    ControlAutomationSupport,
    EvaluationPerspective,
    EvidenceExpectation,
    RuleEvaluationType,
)

RULES_PATH = Path(__file__).parents[2] / "fixtures" / "rules"

CUSTOMER = "cust-001"
ACCOUNT = "123456789012"
ROLE = "arn:aws:iam::123456789012:role/read"
EXTERNAL_ID = "random-customer-bound-external-id"
BUCKET = "demo-bucket"
INSTANCE_ID = "i-0123456789abcdef0"
VOLUME_ID = "vol-0123456789abcdef0"
GROUP_ID = "sg-0123456789abcdef0"
DB_IDENTIFIER = "demo-db-001"
ALB_ARN = (
    "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/demo/50dc6c495c0c9188"
)


class Sts:
    def assume_role(self, **_kwargs: object) -> dict[str, object]:
        return {
            "Credentials": {
                "AccessKeyId": "key",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": datetime.now(UTC) + timedelta(minutes=10),
            }
        }


class FakeS3:
    def get_public_access_block(self, **_kwargs: object) -> dict[str, object]:
        return {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            }
        }

    def get_bucket_encryption(self, **_kwargs: object) -> dict[str, object]:
        return {
            "ServerSideEncryptionConfiguration": {
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"}}]
            }
        }

    def get_bucket_policy_status(self, **_kwargs: object) -> dict[str, object]:
        return {"PolicyStatus": {"IsPublic": False}}

    def get_bucket_policy(self, **_kwargs: object) -> dict[str, object]:
        return {
            "Policy": (
                '{"Version":"2012-10-17","Statement":[{"Effect":"Deny","Principal":"*",'
                '"Action":"s3:*","Resource":"arn:aws:s3:::demo-bucket/*",'
                '"Condition":{"Bool":{"aws:SecureTransport":"false"}}}]}'
            )
        }

    def get_bucket_ownership_controls(self, **_kwargs: object) -> dict[str, object]:
        return {"OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}}

    def get_bucket_logging(self, **_kwargs: object) -> dict[str, object]:
        return {"LoggingEnabled": {"TargetBucket": "demo-logs", "TargetPrefix": "s3/"}}


class FakeEc2:
    def describe_subnets(self, **_kwargs: object) -> dict[str, object]:
        return {
            "Subnets": [{"SubnetId": "subnet-01", "VpcId": "vpc-01", "MapPublicIpOnLaunch": False}]
        }

    def describe_instances(self, **_kwargs: object) -> dict[str, object]:
        return {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": INSTANCE_ID,
                            "State": {"Name": "running"},
                            "SubnetId": "subnet-01",
                            "VpcId": "vpc-01",
                            "PublicIpAddress": "203.0.113.10",
                            "PublicDnsName": "ec2-203-0-113-10.compute.amazonaws.com",
                            "NetworkInterfaces": [
                                {
                                    "NetworkInterfaceId": "eni-01",
                                    "SubnetId": "subnet-01",
                                    "Association": {"PublicIp": "203.0.113.10"},
                                }
                            ],
                            "BlockDeviceMappings": [{"Ebs": {"VolumeId": VOLUME_ID}}],
                            "SecurityGroups": [{"GroupId": GROUP_ID}],
                        }
                    ]
                }
            ]
        }

    def describe_volumes(self, **_kwargs: object) -> dict[str, object]:
        return {
            "Volumes": [{"VolumeId": VOLUME_ID, "Encrypted": True, "KmsKeyId": "arn:aws:kms:k"}]
        }

    def describe_security_groups(self, **_kwargs: object) -> dict[str, object]:
        return {
            "SecurityGroups": [
                {
                    "GroupId": GROUP_ID,
                    "GroupName": "demo",
                    "VpcId": "vpc-01",
                    "IpPermissions": [
                        {
                            "IpProtocol": "tcp",
                            "FromPort": 443,
                            "ToPort": 443,
                            "IpRanges": [{"CidrIp": "10.0.0.0/16"}],
                        }
                    ],
                }
            ]
        }


class FakeRds:
    def describe_db_instances(self, **_kwargs: object) -> dict[str, object]:
        return {
            "DBInstances": [
                {
                    "DBInstanceIdentifier": DB_IDENTIFIER,
                    "DBInstanceStatus": "available",
                    "Engine": "postgres",
                    "PubliclyAccessible": False,
                    "StorageEncrypted": True,
                    "KmsKeyId": "arn:aws:kms:k",
                    "IAMDatabaseAuthenticationEnabled": True,
                    "EnabledCloudwatchLogsExports": ["postgresql"],
                    "DBSubnetGroup": {
                        "DBSubnetGroupName": "demo-subnets",
                        "VpcId": "vpc-01",
                        "SubnetGroupStatus": "Complete",
                    },
                    "VpcSecurityGroups": [{"VpcSecurityGroupId": GROUP_ID, "Status": "active"}],
                }
            ]
        }


class FakeElbV2:
    def describe_load_balancers(self, **_kwargs: object) -> dict[str, object]:
        return {
            "LoadBalancers": [
                {
                    "LoadBalancerArn": ALB_ARN,
                    "LoadBalancerName": "demo",
                    "Type": "application",
                    "Scheme": "internet-facing",
                    "State": {"Code": "active"},
                    "VpcId": "vpc-01",
                }
            ]
        }

    def describe_listeners(self, **_kwargs: object) -> dict[str, object]:
        return {
            "Listeners": [
                {
                    "ListenerArn": f"{ALB_ARN}/listener/1",
                    "Port": 443,
                    "Protocol": "HTTPS",
                    "SslPolicy": "ELBSecurityPolicy-TLS13-1-2-2021-06",
                    "Certificates": [{"CertificateArn": "arn:aws:acm:cert"}],
                }
            ]
        }

    def describe_load_balancer_attributes(self, **_kwargs: object) -> dict[str, object]:
        return {
            "Attributes": [
                {"Key": "access_logs.s3.enabled", "Value": "true"},
                {"Key": "access_logs.s3.bucket", "Value": "demo-logs"},
                {"Key": "access_logs.s3.prefix", "Value": "alb"},
            ]
        }


def _document(tool: object, resource_type: str, resource_id: str) -> dict[str, object]:
    loader = ActualEvidenceLoader(
        tool=tool,  # type: ignore[arg-type]
        customer_id=CUSTOMER,
        aws_account_id=ACCOUNT,
        resource_type=resource_type,
    )
    return loader.load(resource_id).resource_document


def projected_documents() -> dict[str, dict[str, object]]:
    """One fully populated projected document per readable resource type."""
    sts = Sts()
    s3 = AssumeRoleS3ResourceTool(
        customer_id=CUSTOMER,
        aws_account_id=ACCOUNT,
        role_arn=ROLE,
        external_id=EXTERNAL_ID,
        sts=sts,
        s3_client_factory=lambda _credentials: FakeS3(),
    )
    ec2 = AssumeRoleEc2ResourceTool(
        customer_id=CUSTOMER,
        aws_account_id=ACCOUNT,
        role_arn=ROLE,
        external_id=EXTERNAL_ID,
        sts=sts,
        ec2_client_factory=lambda _credentials: FakeEc2(),
    )
    rds = AssumeRoleRdsResourceTool(
        customer_id=CUSTOMER,
        aws_account_id=ACCOUNT,
        role_arn=ROLE,
        external_id=EXTERNAL_ID,
        sts=sts,
        rds_client_factory=lambda _credentials: FakeRds(),
        ec2_client_factory=lambda _credentials: FakeEc2(),
    )
    alb = AssumeRoleAlbResourceTool(
        customer_id=CUSTOMER,
        aws_account_id=ACCOUNT,
        role_arn=ROLE,
        external_id=EXTERNAL_ID,
        sts=sts,
        elbv2_client_factory=lambda _credentials: FakeElbV2(),
    )
    return {
        S3_RESOURCE_TYPE: _document(s3, S3_RESOURCE_TYPE, BUCKET),
        EC2_INSTANCE_RESOURCE_TYPE: _document(ec2, EC2_INSTANCE_RESOURCE_TYPE, INSTANCE_ID),
        RDS_INSTANCE_RESOURCE_TYPE: _document(rds, RDS_INSTANCE_RESOURCE_TYPE, DB_IDENTIFIER),
        ALB_RESOURCE_TYPE: _document(alb, ALB_RESOURCE_TYPE, ALB_ARN),
    }


class AwsCapabilityBindingTest(unittest.TestCase):
    def test_every_declared_aws_path_exists_in_the_real_adapter_projection(self) -> None:
        """선언한 경로를 손으로 적은 기대값이 아니라 실제 adapter 출력에 대고 확인한다.

        adapter의 projection field 목록이 바뀌면 이 테스트가 먼저 실패한다. 그렇지 않으면
        틀린 경로가 런타임에 영원한 `INSUFFICIENT_EVIDENCE`로만 드러난다.
        """
        documents = projected_documents()

        for control in MVP_CONTROL_CATALOG.controls:
            for binding in control.available_evidence_capabilities:
                if binding.perspective is not EvaluationPerspective.AWS_ACTUAL:
                    continue
                with self.subTest(capability=binding.capability_key):
                    document = documents.get(binding.resource_type)
                    self.assertIsNotNone(
                        document,
                        f"{binding.capability_key} binds {binding.resource_type}, "
                        "which no Actual read adapter produces",
                    )
                    self.assertEqual(missing_document_paths(document, binding.document_paths), ())

    def test_an_aws_capability_only_binds_a_readable_resource_type(self) -> None:
        readable = set(projected_documents())

        for control in MVP_CONTROL_CATALOG.controls:
            for binding in control.available_evidence_capabilities:
                if binding.perspective is EvaluationPerspective.AWS_ACTUAL:
                    with self.subTest(capability=binding.capability_key):
                        self.assertIn(binding.resource_type, readable)


class IacHintTest(unittest.TestCase):
    def test_iac_hints_are_never_authoritative(self) -> None:
        """IaC hint를 authoritative capability proof로 취급하지 않는다.

        `is_authoritative`가 IaC에서 참이 되면 Runtime이 HCL을 파싱하지도 않고 attribute
        수준 hard gate를 걸게 된다.
        """
        for control in MVP_CONTROL_CATALOG.controls:
            for binding in control.available_evidence_capabilities:
                if binding.perspective is EvaluationPerspective.IAC:
                    with self.subTest(capability=binding.capability_key):
                        self.assertFalse(binding.is_authoritative)
                        self.assertEqual(binding.document_paths, ())

    def test_every_iac_capability_declares_at_least_one_terraform_resource_type(self) -> None:
        """hint는 prompt 경계와 리뷰 화면 설명으로 실제로 쓰이므로 비어 있으면 안 된다."""
        for control in MVP_CONTROL_CATALOG.controls:
            for binding in control.available_evidence_capabilities:
                if binding.perspective is EvaluationPerspective.IAC:
                    with self.subTest(capability=binding.capability_key):
                        self.assertTrue(binding.terraform_resource_types)


class CatalogScopeTest(unittest.TestCase):
    def test_the_catalog_covers_every_shipped_fixture_rule_exactly_once(self) -> None:
        registry = load_rule_registry(RULES_PATH)
        rule_ids = sorted(rule.rule_id for rule in registry.rules)

        self.assertEqual(rule_ids, sorted(LEGACY_RULE_CONTROL_KEYS))
        for rule_id, control_key in LEGACY_RULE_CONTROL_KEYS.items():
            with self.subTest(rule=rule_id):
                self.assertIsNotNone(MVP_CONTROL_CATALOG.control(control_key))
        self.assertEqual(len(set(LEGACY_RULE_CONTROL_KEYS.values())), len(LEGACY_RULE_CONTROL_KEYS))

    def test_every_available_control_declares_aws_evidence_for_every_aws_type_it_supports(
        self,
    ) -> None:
        """A Control that says it supports AWS evaluation must have AWS evidence to judge on.

        네 S3 Control은 처음에 IaC 전용이었지만 배포된 legacy Rule은 그 AWS 좌표를 계획했고,
        AWS binding이 없는 좌표에서 모델은 다른 field를 근거로 PASS를 냈다. 선언과 계획이 어긋난
        Control이 다시 생기지 않게 여기서 막는다.
        """
        aws_types = {RuleEvaluationType.AWS, RuleEvaluationType.HYBRID}
        for control in MVP_CONTROL_CATALOG.controls:
            if control.automation_support is not ControlAutomationSupport.AVAILABLE:
                continue
            with self.subTest(control=control.control_key):
                has_aws_binding = any(
                    binding.perspective is EvaluationPerspective.AWS_ACTUAL
                    for binding in control.available_evidence_capabilities
                )
                supports_aws = bool(aws_types & set(control.supported_evaluation_types))
                self.assertEqual(has_aws_binding, supports_aws)
                if supports_aws:
                    required_aws = [
                        key
                        for key in control.baseline_required_evidence
                        if control.capability(key) is not None
                        and control.capability(key).perspective  # type: ignore[union-attr]
                        is EvaluationPerspective.AWS_ACTUAL
                    ]
                    self.assertTrue(required_aws, "baseline required evidence must include AWS")

    def test_s3_facts_are_decidable_and_s3_interpretations_are_not(self) -> None:
        """ACL과 logging은 값 하나로 정해지는 사실이다. policy 본문의 범위와 TLS deny 문은 해석이다."""
        expectations = {
            "S3_BUCKET_ACL_DISABLED": ("S3.OWNERSHIP_CONTROLS", True),
            "S3_SERVER_ACCESS_LOGGING": ("S3.SERVER_ACCESS_LOGGING", True),
            "S3_BUCKET_POLICY_RESTRICTED": ("S3.BUCKET_POLICY_PRINCIPALS", False),
            "S3_TLS_ONLY": ("S3.BUCKET_POLICY_TLS", False),
        }
        for control_key, (capability_key, decidable) in expectations.items():
            with self.subTest(control=control_key):
                control = MVP_CONTROL_CATALOG.control(control_key)
                assert control is not None
                binding = control.capability(capability_key)
                assert binding is not None
                self.assertIs(binding.perspective, EvaluationPerspective.AWS_ACTUAL)
                self.assertEqual(binding.is_decidable, decidable)

    def test_security_group_ingress_is_decided_by_the_cidr_not_the_model(self) -> None:
        for control_key, capability_key in (
            ("EC2_SG_INGRESS_RESTRICTED", "EC2.SECURITY_GROUP_INGRESS"),
            ("RDS_ACCESS_RESTRICTED", "RDS.SECURITY_GROUP_INGRESS"),
        ):
            with self.subTest(control=control_key):
                control = MVP_CONTROL_CATALOG.control(control_key)
                assert control is not None
                binding = control.capability(capability_key)
                assert binding is not None
                self.assertIs(binding.expectation, EvidenceExpectation.NO_PUBLIC_INGRESS)

    def test_ec2_snapshot_is_known_but_not_exposed_as_automatable(self) -> None:
        control = MVP_CONTROL_CATALOG.control("EC2_SNAPSHOT_NOT_PUBLIC")

        assert control is not None
        self.assertIs(control.automation_support, ControlAutomationSupport.KNOWN_UNSUPPORTED)
        self.assertEqual(control.supported_evaluation_types, ())
        self.assertNotIn(control, MVP_CONTROL_CATALOG.automatable_controls())

    def test_the_catalog_declares_the_two_manual_controls(self) -> None:
        """조직 통제와 "아직 지원하지 않는 기술 통제"는 둘 다 사람이 판정하지만 이유가 다르다."""
        manual = MVP_CONTROL_CATALOG.manual_controls()

        self.assertEqual(
            [control.control_key for control in manual],
            [MANUAL_CONTROL_KEY, NOT_YET_SUPPORTED_CONTROL_KEY],
        )
        self.assertIs(manual_control(), manual[0])

    def test_the_governance_resource_type_is_not_a_readable_aws_type(self) -> None:
        """MANUAL 좌표는 실제 AWS 리소스가 아니다. 읽기 어댑터가 있는 type과 겹치면 안 된다."""
        self.assertNotIn(GOVERNANCE_ASSESSMENT_RESOURCE_TYPE, projected_documents())

    def test_the_catalog_version_is_pinned(self) -> None:
        self.assertEqual(MVP_CONTROL_CATALOG.version, CONTROL_CATALOG_VERSION)


if __name__ == "__main__":
    unittest.main()
