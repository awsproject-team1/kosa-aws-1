"""The code-owned Governance Control Catalog: what this product can actually evaluate.

이 모듈은 **정책 문서의 내용을 저장하는 곳이 아니다.** 고객 정책 문서와 제품이 실제로 실행할 수
있는 평가 기능 사이의 경계를 정의한다. Extractor는 이 경계 밖으로 나가는 Requirement를
`UNSUPPORTED`로 남기고, Rule Builder는 이 경계 밖의 Control·resource type·evidence를 거절한다.

**AWS와 IaC는 대칭이 아니다.** AWS Actual adapter는 구조화된 projected document를 돌려주므로
`document_paths`가 실제 경로를 가리키는지 이 모듈의 테스트가 adapter projection과 대조할 수 있고,
Runtime은 그 경로로 모델 호출 전에 근거 유무를 판정한다. IaC evaluator는 raw HCL 텍스트를 받고
Evidence locator는 `terraform:{path}` 파일 단위이므로, Terraform hint는 prompt 경계와 리뷰 화면
설명에만 쓰는 non-authoritative 값이다. IaC attribute-level 사전 검증에는 별도의 HCL
parser/projection 계층이 필요하며 이번 범위에 없다.

**존재와 지원은 다르다.** `KNOWN_UNSUPPORTED`는 제품이 아는 Control이지만 지금 실행 경로가 없다.
Catalog에서 지우면 Extractor가 그것을 다른 Control로 잘못 매핑하고, `AVAILABLE`로 두면 실행되지
않을 Rule이 승인 가능해진다.
"""

from __future__ import annotations

from agent.runtime import (
    ALB_RESOURCE_TYPE,
    EC2_INSTANCE_RESOURCE_TYPE,
    RDS_INSTANCE_RESOURCE_TYPE,
    S3_RESOURCE_TYPE,
)
from apps.backend.policy.evidence_paths import parse_document_path
from packages.contracts import (
    ControlAutomationSupport,
    EvaluationPerspective,
    EvidenceCapabilityBinding,
    EvidenceExpectation,
    GovernanceControl,
    GovernanceControlCatalog,
    RuleEvaluationType,
    RuleSeverity,
)

CONTROL_CATALOG_VERSION = "governance-control-catalog/2026-09-03"

#: MANUAL Rule이 평가되는 안정된 좌표. Assessment ID를 쓰지 않는다 — Initial과 Post-Deploy
#: Verification이 같은 Repository에 대해 같은 좌표를 가져야 비교가 성립한다.
GOVERNANCE_ASSESSMENT_RESOURCE_TYPE = "AWS::Governance::Assessment"

EC2_VOLUME_RESOURCE_TYPE = "AWS::EC2::Volume"
EC2_SECURITY_GROUP_RESOURCE_TYPE = "AWS::EC2::SecurityGroup"
EC2_SNAPSHOT_RESOURCE_TYPE = "AWS::EC2::Snapshot"

MANUAL_CONTROL_KEY = "ORGANIZATIONAL_CONTROL_MANUAL_REVIEW"


def _aws(
    capability_key: str,
    resource_type: str,
    *document_paths: str,
    expectation: EvidenceExpectation | None = None,
    expected_value: str | None = None,
    expectation_paths: tuple[str, ...] = (),
) -> EvidenceCapabilityBinding:
    """Declare one AWS_ACTUAL capability, and — when its evidence decides the control — how.

    `expectation`이 있으면 Runtime이 모델 없이 그 capability를 판정한다. 없으면 지금처럼 모델이
    문서를 읽고 판단한다. 근거만으로 통제를 **확정할 수 있을 때만** 붙인다.
    """
    for path in document_paths:
        parse_document_path(path)  # 오타 난 경로는 import 시점에 실패한다.
    return EvidenceCapabilityBinding(
        capability_key=capability_key,
        perspective=EvaluationPerspective.AWS_ACTUAL,
        resource_type=resource_type,
        document_paths=document_paths,
        expectation=expectation,
        expected_value=expected_value,
        expectation_paths=expectation_paths,
    )


def _iac(
    capability_key: str,
    resource_type: str,
    *,
    terraform_resource_types: tuple[str, ...],
    terraform_attribute_names: tuple[str, ...] = (),
) -> EvidenceCapabilityBinding:
    return EvidenceCapabilityBinding(
        capability_key=capability_key,
        perspective=EvaluationPerspective.IAC,
        resource_type=resource_type,
        terraform_resource_types=terraform_resource_types,
        terraform_attribute_names=terraform_attribute_names,
    )


# --------------------------------------------------------------------------------------
# S3
# --------------------------------------------------------------------------------------

_S3_BLOCK_PUBLIC_ACCESS = GovernanceControl(
    control_key="S3_BLOCK_PUBLIC_ACCESS",
    title="Object storage blocks public access",
    description=(
        "Every bucket must block public ACLs and public bucket policies at the account or "
        "bucket level."
    ),
    automation_support=ControlAutomationSupport.AVAILABLE,
    supported_resource_types=(S3_RESOURCE_TYPE,),
    supported_evaluation_types=(
        RuleEvaluationType.IAC,
        RuleEvaluationType.AWS,
        RuleEvaluationType.HYBRID,
    ),
    available_evidence_capabilities=(
        _aws(
            "S3.PUBLIC_ACCESS_BLOCK",
            S3_RESOURCE_TYPE,
            "attributes.public_access_block.BlockPublicAcls",
            "attributes.public_access_block.IgnorePublicAcls",
            "attributes.public_access_block.BlockPublicPolicy",
            "attributes.public_access_block.RestrictPublicBuckets",
            expectation=EvidenceExpectation.ALL_TRUE,
        ),
        _iac(
            "S3.IAC_PUBLIC_ACCESS_BLOCK",
            S3_RESOURCE_TYPE,
            terraform_resource_types=("aws_s3_bucket_public_access_block",),
            terraform_attribute_names=(
                "block_public_acls",
                "ignore_public_acls",
                "block_public_policy",
                "restrict_public_buckets",
            ),
        ),
    ),
    allowed_tool_bindings=("aws:s3:GetPublicAccessBlock",),
    baseline_required_evidence=("S3.PUBLIC_ACCESS_BLOCK",),
    baseline_optional_evidence=("S3.IAC_PUBLIC_ACCESS_BLOCK",),
    severity_guidance="A publicly readable bucket exposes stored data with no further step.",
    default_severity=RuleSeverity.CRITICAL,
)

_S3_ENCRYPTION_AT_REST = GovernanceControl(
    control_key="S3_ENCRYPTION_AT_REST",
    title="Object storage is encrypted at rest",
    description="Every bucket must declare server-side encryption by default.",
    automation_support=ControlAutomationSupport.AVAILABLE,
    supported_resource_types=(S3_RESOURCE_TYPE,),
    supported_evaluation_types=(
        RuleEvaluationType.IAC,
        RuleEvaluationType.AWS,
        RuleEvaluationType.HYBRID,
    ),
    available_evidence_capabilities=(
        _aws(
            "S3.ENCRYPTION",
            S3_RESOURCE_TYPE,
            "attributes.encryption.Rules[]",
            expectation=EvidenceExpectation.NON_EMPTY,
        ),
        _iac(
            "S3.IAC_ENCRYPTION",
            S3_RESOURCE_TYPE,
            terraform_resource_types=("aws_s3_bucket_server_side_encryption_configuration",),
            terraform_attribute_names=("sse_algorithm", "kms_master_key_id"),
        ),
    ),
    allowed_tool_bindings=("aws:s3:GetBucketEncryption",),
    baseline_required_evidence=("S3.ENCRYPTION",),
    baseline_optional_evidence=("S3.IAC_ENCRYPTION",),
    severity_guidance="Unencrypted storage fails at-rest protection requirements outright.",
    default_severity=RuleSeverity.HIGH,
)

# `PolicyStatus.IsPublic`은 "누구에게나 공개인가"만 답한다. "필요한 주체로 제한됐는가"는 그 값으로
# 판정할 수 없으므로 이 Control은 AWS/HYBRID를 지원하지 않는다. 지원한다고 선언하면 임의 Principal
# 허용 정책이 `IsPublic=false`라는 이유로 통과한다.
_S3_BUCKET_POLICY_RESTRICTED = GovernanceControl(
    control_key="S3_BUCKET_POLICY_RESTRICTED",
    title="Bucket policy restricts principals and networks",
    description=(
        "A bucket policy must name the principals, source networks, or resources it allows, "
        "rather than granting broadly."
    ),
    automation_support=ControlAutomationSupport.AVAILABLE,
    supported_resource_types=(S3_RESOURCE_TYPE,),
    supported_evaluation_types=(RuleEvaluationType.IAC,),
    available_evidence_capabilities=(
        _iac(
            "S3.IAC_BUCKET_POLICY",
            S3_RESOURCE_TYPE,
            terraform_resource_types=("aws_s3_bucket_policy", "aws_iam_policy_document"),
            terraform_attribute_names=("principals", "condition", "actions", "resources"),
        ),
    ),
    baseline_required_evidence=("S3.IAC_BUCKET_POLICY",),
    severity_guidance="An over-broad bucket policy grants access no reviewer approved.",
    default_severity=RuleSeverity.HIGH,
)

_S3_BUCKET_ACL_DISABLED = GovernanceControl(
    control_key="S3_BUCKET_ACL_DISABLED",
    title="Bucket ACLs are disabled",
    description="Object ownership must be enforced so ACLs cannot grant access.",
    automation_support=ControlAutomationSupport.AVAILABLE,
    supported_resource_types=(S3_RESOURCE_TYPE,),
    supported_evaluation_types=(RuleEvaluationType.IAC,),
    available_evidence_capabilities=(
        _iac(
            "S3.IAC_OBJECT_OWNERSHIP",
            S3_RESOURCE_TYPE,
            terraform_resource_types=("aws_s3_bucket_ownership_controls", "aws_s3_bucket_acl"),
            terraform_attribute_names=("object_ownership", "acl"),
        ),
    ),
    baseline_required_evidence=("S3.IAC_OBJECT_OWNERSHIP",),
    severity_guidance="ACL-based grants bypass the bucket policy review path.",
    default_severity=RuleSeverity.MEDIUM,
)

_S3_TLS_ONLY = GovernanceControl(
    control_key="S3_TLS_ONLY",
    title="Object storage requires TLS in transit",
    description="A bucket policy must deny requests that do not use TLS.",
    automation_support=ControlAutomationSupport.AVAILABLE,
    supported_resource_types=(S3_RESOURCE_TYPE,),
    supported_evaluation_types=(RuleEvaluationType.IAC,),
    available_evidence_capabilities=(
        _iac(
            "S3.IAC_TLS_ONLY_POLICY",
            S3_RESOURCE_TYPE,
            terraform_resource_types=("aws_s3_bucket_policy", "aws_iam_policy_document"),
            terraform_attribute_names=("aws:SecureTransport", "condition", "effect"),
        ),
    ),
    baseline_required_evidence=("S3.IAC_TLS_ONLY_POLICY",),
    severity_guidance="Plaintext transfer exposes objects and credentials on the wire.",
    default_severity=RuleSeverity.MEDIUM,
)

_S3_SERVER_ACCESS_LOGGING = GovernanceControl(
    control_key="S3_SERVER_ACCESS_LOGGING",
    title="Object storage records server access logs",
    description="Buckets must deliver server access logs to a logging destination.",
    automation_support=ControlAutomationSupport.AVAILABLE,
    supported_resource_types=(S3_RESOURCE_TYPE,),
    supported_evaluation_types=(RuleEvaluationType.IAC,),
    available_evidence_capabilities=(
        _iac(
            "S3.IAC_SERVER_ACCESS_LOGGING",
            S3_RESOURCE_TYPE,
            terraform_resource_types=("aws_s3_bucket_logging",),
            terraform_attribute_names=("target_bucket", "target_prefix"),
        ),
    ),
    baseline_required_evidence=("S3.IAC_SERVER_ACCESS_LOGGING",),
    severity_guidance="Without access logs an incident cannot be reconstructed afterwards.",
    default_severity=RuleSeverity.MEDIUM,
)

# --------------------------------------------------------------------------------------
# EC2
# --------------------------------------------------------------------------------------

_EC2_NO_PUBLIC_IP = GovernanceControl(
    control_key="EC2_NO_PUBLIC_IP",
    title="Private-tier compute has no public address",
    description="Instances in a private tier must not carry a public IPv4 address.",
    automation_support=ControlAutomationSupport.AVAILABLE,
    supported_resource_types=(EC2_INSTANCE_RESOURCE_TYPE,),
    supported_evaluation_types=(
        RuleEvaluationType.IAC,
        RuleEvaluationType.AWS,
        RuleEvaluationType.HYBRID,
    ),
    available_evidence_capabilities=(
        _aws(
            "EC2.PUBLIC_ADDRESS",
            EC2_INSTANCE_RESOURCE_TYPE,
            "attributes.instance.SubnetId",
        ),
        _aws(
            "EC2.NETWORK_INTERFACE_ASSOCIATION",
            EC2_INSTANCE_RESOURCE_TYPE,
            "attributes.network_interfaces[].NetworkInterfaceId",
        ),
        _iac(
            "EC2.IAC_PUBLIC_ADDRESS",
            EC2_INSTANCE_RESOURCE_TYPE,
            terraform_resource_types=("aws_instance", "aws_subnet"),
            terraform_attribute_names=(
                "associate_public_ip_address",
                "map_public_ip_on_launch",
            ),
        ),
    ),
    allowed_tool_bindings=("aws:ec2:DescribeInstances",),
    baseline_required_evidence=("EC2.PUBLIC_ADDRESS",),
    baseline_optional_evidence=("EC2.NETWORK_INTERFACE_ASSOCIATION", "EC2.IAC_PUBLIC_ADDRESS"),
    severity_guidance="A public address puts a private-tier host directly on the internet.",
    default_severity=RuleSeverity.HIGH,
)

_EC2_EBS_ENCRYPTION = GovernanceControl(
    control_key="EC2_EBS_ENCRYPTION",
    title="Attached block storage is encrypted",
    description="Every EBS volume attached to an instance must be encrypted.",
    automation_support=ControlAutomationSupport.AVAILABLE,
    supported_resource_types=(EC2_INSTANCE_RESOURCE_TYPE, EC2_VOLUME_RESOURCE_TYPE),
    supported_evaluation_types=(
        RuleEvaluationType.IAC,
        RuleEvaluationType.AWS,
        RuleEvaluationType.HYBRID,
    ),
    available_evidence_capabilities=(
        _aws(
            "EC2.VOLUME_ENCRYPTION",
            EC2_INSTANCE_RESOURCE_TYPE,
            "attributes.volumes[].VolumeId",
            "attributes.volumes[].Encrypted",
            expectation=EvidenceExpectation.ALL_TRUE,
            # VolumeId는 근거의 좌표이지 기준이 아니다. 판정 대상은 암호화 여부뿐이다.
            expectation_paths=("attributes.volumes[].Encrypted",),
        ),
        _iac(
            "EC2.IAC_VOLUME_ENCRYPTION",
            EC2_INSTANCE_RESOURCE_TYPE,
            terraform_resource_types=("aws_instance", "aws_ebs_volume"),
            terraform_attribute_names=("encrypted", "root_block_device", "ebs_block_device"),
        ),
    ),
    allowed_tool_bindings=("aws:ec2:DescribeVolumes",),
    baseline_required_evidence=("EC2.VOLUME_ENCRYPTION",),
    baseline_optional_evidence=("EC2.IAC_VOLUME_ENCRYPTION",),
    severity_guidance="An unencrypted volume leaks its whole contents if a snapshot escapes.",
    default_severity=RuleSeverity.HIGH,
)

_EC2_SG_INGRESS_RESTRICTED = GovernanceControl(
    control_key="EC2_SG_INGRESS_RESTRICTED",
    title="Inbound access is restricted to required sources and ports",
    description="Security group ingress must name the sources and ports it allows.",
    automation_support=ControlAutomationSupport.AVAILABLE,
    supported_resource_types=(EC2_INSTANCE_RESOURCE_TYPE, EC2_SECURITY_GROUP_RESOURCE_TYPE),
    supported_evaluation_types=(
        RuleEvaluationType.IAC,
        RuleEvaluationType.AWS,
        RuleEvaluationType.HYBRID,
    ),
    available_evidence_capabilities=(
        _aws(
            "EC2.SECURITY_GROUP_INGRESS",
            EC2_INSTANCE_RESOURCE_TYPE,
            "attributes.security_groups[].GroupId",
            "attributes.security_groups[].IpPermissions",
        ),
        _iac(
            "EC2.IAC_SECURITY_GROUP_INGRESS",
            EC2_INSTANCE_RESOURCE_TYPE,
            terraform_resource_types=(
                "aws_security_group",
                "aws_vpc_security_group_ingress_rule",
            ),
            terraform_attribute_names=("ingress", "cidr_blocks", "from_port", "to_port"),
        ),
    ),
    allowed_tool_bindings=("aws:ec2:DescribeSecurityGroups",),
    baseline_required_evidence=("EC2.SECURITY_GROUP_INGRESS",),
    baseline_optional_evidence=("EC2.IAC_SECURITY_GROUP_INGRESS",),
    severity_guidance="Open ingress reaches the workload without any other control in between.",
    default_severity=RuleSeverity.HIGH,
)

# M1 planner는 Snapshot work를 만들지 못한다. Catalog에 남기되 실행 가능한 것은 아무것도 선언하지
# 않는다 — 여기 evaluation type이나 capability를 적으면 실행되지 않을 Rule이 승인 가능해진다.
_EC2_SNAPSHOT_NOT_PUBLIC = GovernanceControl(
    control_key="EC2_SNAPSHOT_NOT_PUBLIC",
    title="Block storage snapshots are not shared publicly",
    description="EBS snapshots must not be shared with all AWS accounts.",
    automation_support=ControlAutomationSupport.KNOWN_UNSUPPORTED,
    supported_resource_types=(EC2_SNAPSHOT_RESOURCE_TYPE,),
    severity_guidance="A public snapshot exposes a whole volume image to any AWS account.",
    default_severity=RuleSeverity.HIGH,
)

# --------------------------------------------------------------------------------------
# RDS
# --------------------------------------------------------------------------------------

_RDS_NOT_PUBLIC = GovernanceControl(
    control_key="RDS_NOT_PUBLIC",
    title="Managed databases are not publicly accessible",
    description="A DB instance must not be reachable from outside its VPC.",
    automation_support=ControlAutomationSupport.AVAILABLE,
    supported_resource_types=(RDS_INSTANCE_RESOURCE_TYPE,),
    supported_evaluation_types=(
        RuleEvaluationType.IAC,
        RuleEvaluationType.AWS,
        RuleEvaluationType.HYBRID,
    ),
    available_evidence_capabilities=(
        _aws(
            "RDS.PUBLICLY_ACCESSIBLE",
            RDS_INSTANCE_RESOURCE_TYPE,
            "attributes.db_instance.PubliclyAccessible",
            expectation=EvidenceExpectation.ALL_FALSE,
        ),
        _iac(
            "RDS.IAC_PUBLICLY_ACCESSIBLE",
            RDS_INSTANCE_RESOURCE_TYPE,
            terraform_resource_types=("aws_db_instance",),
            terraform_attribute_names=("publicly_accessible",),
        ),
    ),
    allowed_tool_bindings=("aws:rds:DescribeDBInstances",),
    baseline_required_evidence=("RDS.PUBLICLY_ACCESSIBLE",),
    baseline_optional_evidence=("RDS.IAC_PUBLICLY_ACCESSIBLE",),
    severity_guidance="A publicly reachable database is one credential away from full access.",
    default_severity=RuleSeverity.CRITICAL,
)

_RDS_ACCESS_RESTRICTED = GovernanceControl(
    control_key="RDS_ACCESS_RESTRICTED",
    title="Database network access is restricted to required sources",
    description="Security groups attached to a DB instance must name the sources they allow.",
    automation_support=ControlAutomationSupport.AVAILABLE,
    supported_resource_types=(RDS_INSTANCE_RESOURCE_TYPE,),
    supported_evaluation_types=(
        RuleEvaluationType.IAC,
        RuleEvaluationType.AWS,
        RuleEvaluationType.HYBRID,
    ),
    available_evidence_capabilities=(
        _aws(
            "RDS.SECURITY_GROUP_INGRESS",
            RDS_INSTANCE_RESOURCE_TYPE,
            "attributes.vpc_security_groups[].VpcSecurityGroupId",
            "attributes.vpc_security_groups[].IpPermissions",
        ),
        _aws(
            "RDS.SUBNET_GROUP",
            RDS_INSTANCE_RESOURCE_TYPE,
            "attributes.db_subnet_group.VpcId",
        ),
        _iac(
            "RDS.IAC_SECURITY_GROUP_INGRESS",
            RDS_INSTANCE_RESOURCE_TYPE,
            terraform_resource_types=("aws_security_group", "aws_db_subnet_group"),
            terraform_attribute_names=("ingress", "cidr_blocks", "vpc_security_group_ids"),
        ),
    ),
    allowed_tool_bindings=("aws:rds:DescribeDBInstances", "aws:ec2:DescribeSecurityGroups"),
    baseline_required_evidence=("RDS.SECURITY_GROUP_INGRESS",),
    baseline_optional_evidence=("RDS.SUBNET_GROUP", "RDS.IAC_SECURITY_GROUP_INGRESS"),
    severity_guidance="Broad database ingress removes the network boundary in front of the data.",
    default_severity=RuleSeverity.HIGH,
)

_RDS_ENCRYPTION_AT_REST = GovernanceControl(
    control_key="RDS_ENCRYPTION_AT_REST",
    title="Managed database storage is encrypted at rest",
    description="A DB instance must have storage encryption enabled.",
    automation_support=ControlAutomationSupport.AVAILABLE,
    supported_resource_types=(RDS_INSTANCE_RESOURCE_TYPE,),
    supported_evaluation_types=(
        RuleEvaluationType.IAC,
        RuleEvaluationType.AWS,
        RuleEvaluationType.HYBRID,
    ),
    available_evidence_capabilities=(
        _aws(
            "RDS.STORAGE_ENCRYPTED",
            RDS_INSTANCE_RESOURCE_TYPE,
            "attributes.db_instance.StorageEncrypted",
            expectation=EvidenceExpectation.ALL_TRUE,
        ),
        _iac(
            "RDS.IAC_STORAGE_ENCRYPTED",
            RDS_INSTANCE_RESOURCE_TYPE,
            terraform_resource_types=("aws_db_instance",),
            terraform_attribute_names=("storage_encrypted", "kms_key_id"),
        ),
    ),
    allowed_tool_bindings=("aws:rds:DescribeDBInstances",),
    baseline_required_evidence=("RDS.STORAGE_ENCRYPTED",),
    baseline_optional_evidence=("RDS.IAC_STORAGE_ENCRYPTED",),
    severity_guidance="Unencrypted database storage fails at-rest protection outright.",
    default_severity=RuleSeverity.HIGH,
)

_RDS_LOG_EXPORTS = GovernanceControl(
    control_key="RDS_LOG_EXPORTS",
    title="Managed databases export access logs",
    description="A DB instance must export its engine access logs to CloudWatch Logs.",
    automation_support=ControlAutomationSupport.AVAILABLE,
    supported_resource_types=(RDS_INSTANCE_RESOURCE_TYPE,),
    supported_evaluation_types=(
        RuleEvaluationType.IAC,
        RuleEvaluationType.AWS,
        RuleEvaluationType.HYBRID,
    ),
    available_evidence_capabilities=(
        _aws(
            "RDS.LOG_EXPORTS",
            RDS_INSTANCE_RESOURCE_TYPE,
            "attributes.db_instance.EnabledCloudwatchLogsExports",
            expectation=EvidenceExpectation.NON_EMPTY,
        ),
        _iac(
            "RDS.IAC_LOG_EXPORTS",
            RDS_INSTANCE_RESOURCE_TYPE,
            terraform_resource_types=("aws_db_instance",),
            terraform_attribute_names=("enabled_cloudwatch_logs_exports",),
        ),
    ),
    allowed_tool_bindings=("aws:rds:DescribeDBInstances",),
    baseline_required_evidence=("RDS.LOG_EXPORTS",),
    baseline_optional_evidence=("RDS.IAC_LOG_EXPORTS",),
    severity_guidance="Without database access logs, misuse of valid credentials leaves no trace.",
    default_severity=RuleSeverity.MEDIUM,
)

# --------------------------------------------------------------------------------------
# ALB
# --------------------------------------------------------------------------------------

_ALB_HTTPS_ONLY = GovernanceControl(
    control_key="ALB_HTTPS_ONLY",
    title="Load balancers terminate HTTPS/TLS only",
    description="Every listener must use HTTPS or TLS with an approved security policy.",
    automation_support=ControlAutomationSupport.AVAILABLE,
    supported_resource_types=(ALB_RESOURCE_TYPE,),
    supported_evaluation_types=(
        RuleEvaluationType.IAC,
        RuleEvaluationType.AWS,
        RuleEvaluationType.HYBRID,
    ),
    available_evidence_capabilities=(
        _aws(
            "ALB.LISTENER_PROTOCOL",
            ALB_RESOURCE_TYPE,
            "attributes.listeners[].ListenerArn",
            "attributes.listeners[].Protocol",
            expectation=EvidenceExpectation.NONE_EQUAL,
            expected_value="HTTP",
            # ListenerArn은 좌표다. HTTPS 전용 여부는 Protocol만이 답한다.
            expectation_paths=("attributes.listeners[].Protocol",),
        ),
        _iac(
            "ALB.IAC_LISTENER_PROTOCOL",
            ALB_RESOURCE_TYPE,
            terraform_resource_types=("aws_lb_listener",),
            terraform_attribute_names=("protocol", "ssl_policy", "certificate_arn"),
        ),
    ),
    allowed_tool_bindings=("aws:elasticloadbalancing:DescribeListeners",),
    baseline_required_evidence=("ALB.LISTENER_PROTOCOL",),
    baseline_optional_evidence=("ALB.IAC_LISTENER_PROTOCOL",),
    severity_guidance="A plaintext listener exposes session tokens on every request.",
    default_severity=RuleSeverity.HIGH,
)

_ALB_ACCESS_LOGGING = GovernanceControl(
    control_key="ALB_ACCESS_LOGGING",
    title="Load balancers record access logs",
    description="Access logging must be enabled and delivered to a logging bucket.",
    automation_support=ControlAutomationSupport.AVAILABLE,
    supported_resource_types=(ALB_RESOURCE_TYPE,),
    supported_evaluation_types=(
        RuleEvaluationType.IAC,
        RuleEvaluationType.AWS,
        RuleEvaluationType.HYBRID,
    ),
    available_evidence_capabilities=(
        _aws(
            "ALB.ACCESS_LOGS",
            ALB_RESOURCE_TYPE,
            "attributes.load_balancer_attributes.{access_logs.s3.enabled}",
            expectation=EvidenceExpectation.ALL_EQUAL,
            expected_value="true",
        ),
        _iac(
            "ALB.IAC_ACCESS_LOGS",
            ALB_RESOURCE_TYPE,
            terraform_resource_types=("aws_lb",),
            terraform_attribute_names=("access_logs", "bucket", "enabled"),
        ),
    ),
    allowed_tool_bindings=("aws:elasticloadbalancing:DescribeLoadBalancerAttributes",),
    baseline_required_evidence=("ALB.ACCESS_LOGS",),
    baseline_optional_evidence=("ALB.IAC_ACCESS_LOGS",),
    severity_guidance="Without request logs an exposure cannot be scoped after the fact.",
    default_severity=RuleSeverity.MEDIUM,
)

# --------------------------------------------------------------------------------------
# MANUAL
# --------------------------------------------------------------------------------------

_ORGANIZATIONAL_CONTROL_MANUAL_REVIEW = GovernanceControl(
    control_key=MANUAL_CONTROL_KEY,
    title="Organizational control settled by human review",
    description=(
        "A requirement about people, contracts, or process that no tool in this product "
        "observes. It is recorded as an evaluation coordinate and left to a reviewer."
    ),
    automation_support=ControlAutomationSupport.MANUAL,
    supported_evaluation_types=(RuleEvaluationType.MANUAL,),
    severity_guidance="Severity follows the organizational owner of the policy, not a tool signal.",
    default_severity=RuleSeverity.MEDIUM,
)


MVP_CONTROL_CATALOG = GovernanceControlCatalog(
    version=CONTROL_CATALOG_VERSION,
    controls=(
        _S3_BLOCK_PUBLIC_ACCESS,
        _S3_ENCRYPTION_AT_REST,
        _S3_BUCKET_POLICY_RESTRICTED,
        _S3_BUCKET_ACL_DISABLED,
        _S3_TLS_ONLY,
        _S3_SERVER_ACCESS_LOGGING,
        _EC2_NO_PUBLIC_IP,
        _EC2_EBS_ENCRYPTION,
        _EC2_SG_INGRESS_RESTRICTED,
        _EC2_SNAPSHOT_NOT_PUBLIC,
        _RDS_NOT_PUBLIC,
        _RDS_ACCESS_RESTRICTED,
        _RDS_ENCRYPTION_AT_REST,
        _RDS_LOG_EXPORTS,
        _ALB_HTTPS_ONLY,
        _ALB_ACCESS_LOGGING,
        _ORGANIZATIONAL_CONTROL_MANUAL_REVIEW,
    ),
)

#: 커밋된 legacy fixture Rule이 어떤 Control을 구현하는지. legacy Rule 자체는 `control_key`를
#: 갖지 않으므로(계약상 실행 의미를 갖지 않는다), 이 매핑은 **Catalog가 이미 배송된 평가 범위를
#: 빠짐없이 덮는지**를 검증하는 회귀 대조표다. Runtime 조회 경로에는 쓰이지 않는다.
LEGACY_RULE_CONTROL_KEYS: dict[str, str] = {
    "S3-PUBLIC-001": "S3_BLOCK_PUBLIC_ACCESS",
    "S3-ENCRYPT-001": "S3_ENCRYPTION_AT_REST",
    "S3-POLICY-001": "S3_BUCKET_POLICY_RESTRICTED",
    "S3-ACL-001": "S3_BUCKET_ACL_DISABLED",
    "S3-TLS-001": "S3_TLS_ONLY",
    "S3-LOGGING-001": "S3_SERVER_ACCESS_LOGGING",
    "EC2-PUBLIC-IP-001": "EC2_NO_PUBLIC_IP",
    "EC2-EBS-ENCRYPT-001": "EC2_EBS_ENCRYPTION",
    "EC2-SG-INGRESS-001": "EC2_SG_INGRESS_RESTRICTED",
    "EC2-SNAPSHOT-PUBLIC-001": "EC2_SNAPSHOT_NOT_PUBLIC",
    "RDS-PUBLIC-001": "RDS_NOT_PUBLIC",
    "RDS-ACCESS-001": "RDS_ACCESS_RESTRICTED",
    "RDS-ENCRYPT-001": "RDS_ENCRYPTION_AT_REST",
    "RDS-LOGGING-001": "RDS_LOG_EXPORTS",
    "ALB-HTTPS-001": "ALB_HTTPS_ONLY",
    "ALB-LOGGING-001": "ALB_ACCESS_LOGGING",
}


def manual_control() -> GovernanceControl:
    """The single MANUAL control every MANUAL candidate maps to."""
    control = MVP_CONTROL_CATALOG.control(MANUAL_CONTROL_KEY)
    if control is None:  # pragma: no cover - the catalog literal above declares it
        raise LookupError("the MVP catalog must declare a MANUAL control")
    return control
