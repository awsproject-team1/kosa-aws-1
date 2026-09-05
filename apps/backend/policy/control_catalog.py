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
    PlanEvidencePath,
    PolicyRule,
    RuleEvaluationType,
    RuleSeverity,
)

#: 2026-09-05: S3 네 Control에 AWS 근거, EC2 서브넷 capability, `ALL_IN`·`NO_PUBLIC_INGRESS` 술어.
#: 이전 판본으로 승인된 Rule은 그 판본을 `control_catalog_version`에 그대로 갖는다 — 여기 값은
#: 앞으로 만들어질 Rule이 어느 Catalog로 검증됐는지 말한다.
CONTROL_CATALOG_VERSION = "governance-control-catalog/2026-09-05.2"

#: MANUAL Rule이 평가되는 안정된 좌표. Assessment ID를 쓰지 않는다 — Initial과 Post-Deploy
#: Verification이 같은 Repository에 대해 같은 좌표를 가져야 비교가 성립한다.
GOVERNANCE_ASSESSMENT_RESOURCE_TYPE = "AWS::Governance::Assessment"

EC2_VOLUME_RESOURCE_TYPE = "AWS::EC2::Volume"
EC2_SECURITY_GROUP_RESOURCE_TYPE = "AWS::EC2::SecurityGroup"
EC2_SNAPSHOT_RESOURCE_TYPE = "AWS::EC2::Snapshot"

MANUAL_CONTROL_KEY = "ORGANIZATIONAL_CONTROL_MANUAL_REVIEW"
#: 기술 통제인데 Catalog에 근거 capability가 아직 없어 사람에게 가는 것. 조직 통제와 같은
#: MANUAL 좌표를 만들지만, 화면과 보고서는 이 둘을 갈라 센다 — "사람이 판정할 일"과
#: "아직 지원하지 않는 일"은 다른 답을 부른다(전자는 검토, 후자는 Catalog 확장).
NOT_YET_SUPPORTED_CONTROL_KEY = "TECHNICAL_CONTROL_NOT_YET_SUPPORTED"


def _aws(
    capability_key: str,
    resource_type: str,
    *document_paths: str,
    expectation: EvidenceExpectation | None = None,
    expected_value: str | None = None,
    expected_values: tuple[str, ...] = (),
    expectation_paths: tuple[str, ...] = (),
    plan_paths: tuple[tuple[str, str], ...] = (),
) -> EvidenceCapabilityBinding:
    """Declare one AWS_ACTUAL capability, and — when its evidence decides the control — how.

    `expectation`이 있으면 Runtime이 모델 없이 그 capability를 판정한다. 없으면 지금처럼 모델이
    문서를 읽고 판단한다. 근거만으로 통제를 **확정할 수 있을 때만** 붙인다.

    `plan_paths`는 `(terraform resource type, after 경로)` 쌍이다: 같은 술어를 terraform plan의
    `after` 값에서 판정할 수 있는 위치. plan의 모양이 AWS 문서와 달라 술어가 맞지 않으면(SG
    ingress block, logging block의 존재 자체가 활성) 비워 둔다 — 억지로 맞추면 다른 술어다.
    """
    for path in document_paths:
        parse_document_path(path)  # 오타 난 경로는 import 시점에 실패한다.
    for _, path in plan_paths:
        parse_document_path(path)
    return EvidenceCapabilityBinding(
        capability_key=capability_key,
        perspective=EvaluationPerspective.AWS_ACTUAL,
        resource_type=resource_type,
        document_paths=document_paths,
        expectation=expectation,
        expected_value=expected_value,
        expected_values=expected_values,
        expectation_paths=expectation_paths,
        plan_paths=tuple(
            PlanEvidencePath(terraform_resource_type=kind, path=path) for kind, path in plan_paths
        ),
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
            plan_paths=(
                ("aws_s3_bucket_public_access_block", "block_public_acls"),
                ("aws_s3_bucket_public_access_block", "ignore_public_acls"),
                ("aws_s3_bucket_public_access_block", "block_public_policy"),
                ("aws_s3_bucket_public_access_block", "restrict_public_buckets"),
            ),
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
            "attributes.encryption.Rules[].ApplyServerSideEncryptionByDefault.SSEAlgorithm",
            # `NON_EMPTY`는 "규칙이 있다"만 답해 어떤 알고리즘이든 통과시켰다. S3가 실제로
            # 받아들이는 세 알고리즘만 허용한다 — AES256이 통과하는 것은 라이브에서 확인된 정답이다.
            expectation=EvidenceExpectation.ALL_IN,
            expected_values=("AES256", "aws:kms", "aws:kms:dsse"),
            plan_paths=(
                (
                    "aws_s3_bucket_server_side_encryption_configuration",
                    "rule[].apply_server_side_encryption_by_default[].sse_algorithm",
                ),
            ),
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

_S3_ALL_TYPES = (RuleEvaluationType.IAC, RuleEvaluationType.AWS, RuleEvaluationType.HYBRID)

# 이 네 Control은 처음에 IaC 전용이었다 — S3 adapter가 `PolicyStatus.IsPublic`만 읽었고 그 값으로는
# "필요한 주체로 제한됐는가"를 판정할 수 없었기 때문이다. 그런데 배포된 baseline Profile의 legacy
# Rule은 이 Control들의 AWS_ACTUAL 좌표를 계획했고, AWS binding이 없어 게이트를 건너뛴 그 좌표에서
# 모델은 public-access-block 플래그를 근거로 PASS를 냈다. adapter가 이제 bucket policy 본문·
# ownership controls·logging을 읽으므로(2026-09-05), 답이 사실인 것(ACL·logging)은 코드가 판정하고
# 해석이 필요한 것(policy 본문의 principal 범위, TLS deny 문)은 실재하는 근거 위에서 모델이 판단한다.
_S3_BUCKET_POLICY_RESTRICTED = GovernanceControl(
    control_key="S3_BUCKET_POLICY_RESTRICTED",
    title="Bucket policy restricts principals and networks",
    description=(
        "A bucket policy must name the principals, source networks, or resources it allows, "
        "rather than granting broadly."
    ),
    automation_support=ControlAutomationSupport.AVAILABLE,
    supported_resource_types=(S3_RESOURCE_TYPE,),
    supported_evaluation_types=_S3_ALL_TYPES,
    available_evidence_capabilities=(
        # 술어 없음: "필요한 주체로 제한됐는가"는 정책 문서의 해석이다. 근거는 본문 그 자체다.
        _aws(
            "S3.BUCKET_POLICY_PRINCIPALS",
            S3_RESOURCE_TYPE,
            "attributes.bucket_policy.present",
        ),
        _iac(
            "S3.IAC_BUCKET_POLICY",
            S3_RESOURCE_TYPE,
            terraform_resource_types=("aws_s3_bucket_policy", "aws_iam_policy_document"),
            terraform_attribute_names=("principals", "condition", "actions", "resources"),
        ),
    ),
    allowed_tool_bindings=("aws:s3:GetBucketPolicy",),
    baseline_required_evidence=("S3.BUCKET_POLICY_PRINCIPALS",),
    baseline_optional_evidence=("S3.IAC_BUCKET_POLICY",),
    severity_guidance="An over-broad bucket policy grants access no reviewer approved.",
    default_severity=RuleSeverity.HIGH,
)

_S3_BUCKET_ACL_DISABLED = GovernanceControl(
    control_key="S3_BUCKET_ACL_DISABLED",
    title="Bucket ACLs are disabled",
    description="Object ownership must be enforced so ACLs cannot grant access.",
    automation_support=ControlAutomationSupport.AVAILABLE,
    supported_resource_types=(S3_RESOURCE_TYPE,),
    supported_evaluation_types=_S3_ALL_TYPES,
    available_evidence_capabilities=(
        _aws(
            "S3.OWNERSHIP_CONTROLS",
            S3_RESOURCE_TYPE,
            "attributes.ownership_controls.ObjectOwnership",
            # `BucketOwnerEnforced`만 ACL을 비활성화한다. `BucketOwnerPreferred`는 ACL이 여전히
            # 작동하는 상태다.
            expectation=EvidenceExpectation.ALL_EQUAL,
            expected_value="BucketOwnerEnforced",
            plan_paths=(("aws_s3_bucket_ownership_controls", "rule[].object_ownership"),),
        ),
        _iac(
            "S3.IAC_OBJECT_OWNERSHIP",
            S3_RESOURCE_TYPE,
            terraform_resource_types=("aws_s3_bucket_ownership_controls", "aws_s3_bucket_acl"),
            terraform_attribute_names=("object_ownership", "acl"),
        ),
    ),
    allowed_tool_bindings=("aws:s3:GetBucketOwnershipControls",),
    baseline_required_evidence=("S3.OWNERSHIP_CONTROLS",),
    baseline_optional_evidence=("S3.IAC_OBJECT_OWNERSHIP",),
    severity_guidance="ACL-based grants bypass the bucket policy review path.",
    default_severity=RuleSeverity.MEDIUM,
)

_S3_TLS_ONLY = GovernanceControl(
    control_key="S3_TLS_ONLY",
    title="Object storage requires TLS in transit",
    description="A bucket policy must deny requests that do not use TLS.",
    automation_support=ControlAutomationSupport.AVAILABLE,
    supported_resource_types=(S3_RESOURCE_TYPE,),
    supported_evaluation_types=_S3_ALL_TYPES,
    available_evidence_capabilities=(
        # 술어 없음: `aws:SecureTransport=false`에 대한 Deny 문이 실제로 모든 요청을 덮는지는
        # Resource·Principal·Condition을 함께 읽어야 한다. 정책이 없으면 `present=false`가 근거다.
        _aws(
            "S3.BUCKET_POLICY_TLS",
            S3_RESOURCE_TYPE,
            "attributes.bucket_policy.present",
        ),
        _iac(
            "S3.IAC_TLS_ONLY_POLICY",
            S3_RESOURCE_TYPE,
            terraform_resource_types=("aws_s3_bucket_policy", "aws_iam_policy_document"),
            terraform_attribute_names=("aws:SecureTransport", "condition", "effect"),
        ),
    ),
    allowed_tool_bindings=("aws:s3:GetBucketPolicy",),
    baseline_required_evidence=("S3.BUCKET_POLICY_TLS",),
    baseline_optional_evidence=("S3.IAC_TLS_ONLY_POLICY",),
    severity_guidance="Plaintext transfer exposes objects and credentials on the wire.",
    default_severity=RuleSeverity.MEDIUM,
)

_S3_SERVER_ACCESS_LOGGING = GovernanceControl(
    control_key="S3_SERVER_ACCESS_LOGGING",
    title="Object storage records server access logs",
    description="Buckets must deliver server access logs to a logging destination.",
    automation_support=ControlAutomationSupport.AVAILABLE,
    supported_resource_types=(S3_RESOURCE_TYPE,),
    supported_evaluation_types=_S3_ALL_TYPES,
    available_evidence_capabilities=(
        _aws(
            "S3.SERVER_ACCESS_LOGGING",
            S3_RESOURCE_TYPE,
            # adapter는 "꺼져 있음"을 field 부재가 아니라 `enabled=false`로 투영한다. 부재는
            # "읽지 못함"이고, 꺼짐은 사실이다 — 둘을 같은 모양으로 두면 위반이 근거 부족이 된다.
            "attributes.logging.enabled",
            expectation=EvidenceExpectation.ALL_TRUE,
        ),
        _iac(
            "S3.IAC_SERVER_ACCESS_LOGGING",
            S3_RESOURCE_TYPE,
            terraform_resource_types=("aws_s3_bucket_logging",),
            terraform_attribute_names=("target_bucket", "target_prefix"),
        ),
    ),
    allowed_tool_bindings=("aws:s3:GetBucketLogging",),
    baseline_required_evidence=("S3.SERVER_ACCESS_LOGGING",),
    baseline_optional_evidence=("S3.IAC_SERVER_ACCESS_LOGGING",),
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
        # 술어 없음: Rule의 전제("private tier")를 판정하는 것은 해석이다. 그러나 그 해석에
        # 필요한 사실 — 서브넷이 공인 IP를 배정하는가 — 는 문서에 있어야 한다. 이 field가 없을 때
        # 모델은 5/5 `OUT_OF_SCOPE`로 회피했다(측정된 gap).
        _aws(
            "EC2.SUBNET_PUBLIC_IP_ASSIGNMENT",
            EC2_INSTANCE_RESOURCE_TYPE,
            "attributes.subnet.MapPublicIpOnLaunch",
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
    allowed_tool_bindings=("aws:ec2:DescribeInstances", "aws:ec2:DescribeSubnets"),
    baseline_required_evidence=("EC2.PUBLIC_ADDRESS", "EC2.SUBNET_PUBLIC_IP_ASSIGNMENT"),
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
            # `aws_instance.id`는 생성 시 계산 중이므로 갱신 plan에서만 identity가 잡힌다 — 아직
            # 없는 인스턴스에 대한 Finding은 없으므로 맞다. 선언되지 않은 block은 plan에 아예
            # 없으므로 둘 중 값이 있는 쪽만 판정한다(`plan_facts`).
            plan_paths=(
                ("aws_instance", "root_block_device[].encrypted"),
                ("aws_instance", "ebs_block_device[].encrypted"),
            ),
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
            "attributes.security_groups[]",
            "attributes.security_groups[].GroupId",
            "attributes.security_groups[].IpPermissions",
            # "인터넷 전체에 열려 있는가"는 사실이다. 술어는 그룹 객체 하나를 통째로 보고
            # `IpPermissions`의 CIDR을 읽는다 — ingress가 없는 그룹은 충족이다.
            expectation=EvidenceExpectation.NO_PUBLIC_INGRESS,
            expectation_paths=("attributes.security_groups[]",),
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
            plan_paths=(("aws_db_instance", "publicly_accessible"),),
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
            "attributes.vpc_security_groups[]",
            "attributes.vpc_security_groups[].VpcSecurityGroupId",
            "attributes.vpc_security_groups[].IpPermissions",
            # 라이브 측정에서 "퍼블릭 아님 + 3306을 0.0.0.0/0에 개방"을 모델은 `OUT_OF_SCOPE`로
            # 회피했다. 인터넷 전체에 열렸는가는 사실이고, 그 사실은 코드가 읽는다.
            expectation=EvidenceExpectation.NO_PUBLIC_INGRESS,
            expectation_paths=("attributes.vpc_security_groups[]",),
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
            plan_paths=(("aws_db_instance", "storage_encrypted"),),
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
            plan_paths=(("aws_db_instance", "enabled_cloudwatch_logs_exports"),),
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
            # 리스너는 plan에서 로드밸런서와 별개 리소스이며 `load_balancer_arn`으로 부모를 가리킨다.
            plan_paths=(("aws_lb_listener", "protocol"), ("aws_alb_listener", "protocol")),
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
            # plan은 boolean `true`를 준다. 술어는 boolean을 "true"/"false"로 읽는다.
            plan_paths=(("aws_lb", "access_logs[].enabled"), ("aws_alb", "access_logs[].enabled")),
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


_TECHNICAL_CONTROL_NOT_YET_SUPPORTED = GovernanceControl(
    control_key=NOT_YET_SUPPORTED_CONTROL_KEY,
    title="Technical control not yet supported by the evidence catalog",
    description=(
        "A requirement about infrastructure facts (identity, keys, backups, logging, patching, "
        "threat detection) that this catalog declares no evidence capability for yet. It is "
        "recorded as a coordinate a reviewer settles until a capability exists; it is not an "
        "organizational control."
    ),
    automation_support=ControlAutomationSupport.MANUAL,
    supported_evaluation_types=(RuleEvaluationType.MANUAL,),
    severity_guidance="Severity follows the control's owner until the catalog can evidence it.",
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
        _TECHNICAL_CONTROL_NOT_YET_SUPPORTED,
    ),
)

#: 커밋된 legacy fixture Rule이 어떤 Control을 구현하는지. legacy Rule 자체는 `control_key`를
#: 갖지 않으므로(계약상 실행 의미를 갖지 않는다) 이 매핑이 그 자리를 대신한다.
#:
#: **Runtime이 이 매핑을 읽는다 (2026-09-05).** 처음에는 회귀 대조표로만 두었고, legacy Rule의
#: AWS_ACTUAL 평가는 근거 게이트 없이 모델로 갔다. 그 결과 baseline Profile의 S3 Rule 넷
#: (ACL·Bucket Policy·TLS·Logging)은 AWS 문서에 답이 존재할 수 없는데도 모델이 판정했고, 모델은
#: 있지도 않은 근거(public-access-block 플래그)를 대신 인용해 PASS를 냈다. Catalog가 이미 아는
#: 것("이 Control은 AWS 근거가 없다")을 Runtime이 모른 척할 이유가 없다 — `control_for_rule()`.
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


class RuleControlLookupError(LookupError):
    """Raised when an authored Rule names a Control the catalog does not declare."""


def control_for_rule(
    rule: PolicyRule, catalog: GovernanceControlCatalog = MVP_CONTROL_CATALOG
) -> tuple[GovernanceControl, tuple[str, ...]] | None:
    """The Control a Rule implements and the capabilities its evidence must satisfy.

    authored Rule은 `control_key`와 `required_evidence`를 스스로 갖는다. legacy Rule은
    `LEGACY_RULE_CONTROL_KEYS`로 Control을 찾고, 요구 근거는 그 Control의
    `baseline_required_evidence`다 — 그 Rule들이 승인될 때 존재했던 유일한 근거 선언이다.

    `None`은 "Catalog가 이 Rule을 모른다"이며 매핑에 없는 legacy Rule에서만 나온다. authored Rule이
    없는 Control을 가리키는 것은 승인 데이터의 손상이므로 `RuleControlLookupError`다.
    """
    if not isinstance(rule, PolicyRule):
        raise TypeError("rule must be a PolicyRule")
    if rule.evaluation_type is not None:
        control = catalog.control(rule.control_key or "")
        if control is None:
            raise RuleControlLookupError(
                f"approved rule {rule.rule_id!r} names a control the catalog does not declare"
            )
        return control, rule.required_evidence
    control_key = LEGACY_RULE_CONTROL_KEYS.get(rule.rule_id)
    if control_key is None:
        return None
    control = catalog.control(control_key)
    if control is None:  # pragma: no cover - the catalog test pins every mapped key
        raise RuleControlLookupError(f"legacy rule {rule.rule_id!r} maps to an unknown control")
    return control, control.baseline_required_evidence


def control_for_finding(
    rule_id: str,
    rule: PolicyRule | None,
    catalog: GovernanceControlCatalog = MVP_CONTROL_CATALOG,
) -> tuple[GovernanceControl, tuple[str, ...]] | None:
    """`control_for_rule` for callers that hold a Finding, not necessarily the Rule.

    조치·readiness는 Finding의 `rule_id`/`rule_version`만 갖는다. Rule을 읽을 수 있으면
    (`rule`) 그것이 정본이고, 읽을 수 없으면 legacy 매핑만 남는다 — authored Rule은 Catalog 매핑에
    없으므로 그 경우 `None`이며, 호출자는 "판정 없음"으로 다룬다.
    """
    if rule is not None:
        return control_for_rule(rule, catalog)
    control_key = LEGACY_RULE_CONTROL_KEYS.get(rule_id)
    if control_key is None:
        return None
    control = catalog.control(control_key)
    if control is None:  # pragma: no cover - the catalog test pins every mapped key
        return None
    return control, control.baseline_required_evidence
