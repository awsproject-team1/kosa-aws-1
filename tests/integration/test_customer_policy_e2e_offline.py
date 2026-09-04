"""Offline end-to-end: an arbitrary internal policy document decides an Assessment.

    Upload (real parser) → Normalize → Extraction (fake extractor) → Review → Approve
    → Publish → Assessment creation (Profile version pin) → DynamoDB Catalog → plan
    → Worker evaluation (IAC · AWS_ACTUAL · DRIFT · MANUAL) → Result store → Report
    → Findings → Remediation decision → Terraform patch → Pull request (mock writer)

Everything on this path is the production code except three boundaries that need
credentials or a model: Bedrock (a deterministic synthetic evaluator/patch model that reads
the same request bodies the real adapter sends), AWS reads (`MockAwsResourceTool` with
documents in the live adapters' projection shape), and GitHub (`MockGitHubTool` /
`MockGitHubWriteTool`). DynamoDB and S3 are in-memory fakes that enforce the conditional
writes the real repositories rely on.

The policy document below is synthetic — written for this test in the shape of a real
company standard — not a customer original (ADR-0004). The fake extractor is deliberately
text-blind (it returns injected requirements, see `FakePolicyCandidateExtractor`), so what
this file proves is that a *reviewed and approved* customer requirement, not a committed
fixture Rule, is what the Worker evaluates and what the Finding cites.
"""

from __future__ import annotations

import json
import re
import unittest
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from agent.runtime import (
    AwsResourceView,
    IaCDocument,
    MockAwsResourceTool,
    MockGitHubTool,
    MockGitHubWriteTool,
)
from apps.backend.api.jobs import AssessmentRequest, JobApiService
from apps.backend.api.policy_approval import PolicyApprovalApiService
from apps.backend.api.policy_candidates import PolicyCandidateApiService
from apps.backend.api.policy_sources import PolicySourceApiService
from apps.backend.api.remediations import RemediationApiService
from apps.backend.assessment import (
    ActualBedrockEvaluator,
    ActualEvidenceLoader,
    AssessmentResourceWork,
    AssessmentRunner,
    AssessmentWorker,
    BedrockStructuredEvaluator,
    DynamoDbAssessmentReportStore,
    DynamoDbEvaluationResultStore,
    InMemoryModelProfileRegistry,
)
from apps.backend.assessment.manual_review import ManualReviewEvaluator
from apps.backend.assessment.runtime import (
    _evaluable_works,
    _with_complete_evaluation_plan,
    _with_governance_work,
)
from apps.backend.auth import Principal, Role
from apps.backend.jobs import OutboxDispatcher, OutboxStatus, WorkflowOutboxEntry
from apps.backend.policy import (
    DynamoDbPolicyCatalog,
    DynamoDbPolicyCatalogBootstrap,
    PolicyContextResolver,
    load_rule_registry,
)
from apps.backend.policy.authoring import (
    FakePolicyCandidateExtractor,
    NormalizedArtifactReader,
    extract_policy_candidates,
)
from apps.backend.policy.control_catalog import (
    GOVERNANCE_ASSESSMENT_RESOURCE_TYPE,
    MANUAL_CONTROL_KEY,
    MVP_CONTROL_CATALOG,
)
from apps.backend.remediation import (
    InMemoryPatchContentStore,
    PatchPullRequestAction,
    RemediationWork,
    RemediationWorker,
)
from apps.backend.remediation.bedrock import BedrockPatchGenerator
from apps.backend.remediation.sync import SnapshotSyncAction
from apps.backend.repositories.policy_approval import DynamoDbPolicyApprovalRepository
from apps.backend.repositories.policy_ingestion import DynamoDbPolicySourceUploadRepository
from apps.backend.repositories.remediation_context import DynamoDbRemediationContextReader
from packages.contracts import (
    AssessmentPhase,
    AuthoringRunStatus,
    CandidateClassification,
    EvaluationPerspective,
    EvaluationStatus,
    ExtractedRequirement,
    ManualReviewCode,
    ModelProfile,
    ModelProfileRole,
    PolicyRuleReference,
    PolicySourceUploadRequest,
    RemediationAction,
    RuleEvaluationType,
    WorkflowCommand,
    WorkflowTask,
)
from tests.unit.test_authoring_result_persistence import FakeTable

CUSTOMER = "acme-cloud"
REPOSITORY = "acme-platform-iac"
AWS_ACCOUNT = "111122223333"
COMMIT = "d6b2c119872e20a890e14cb6bc41017527e600e6"
RULES_PATH = Path(__file__).parents[2] / "fixtures" / "rules"

BUCKET_ID = "acme-media-assets"
INSTANCE_ID = "i-0f3a1c9d2e4b5a678"
DB_ID = "acme-app-db"
ALB_ARN = (
    f"arn:aws:elasticloadbalancing:us-east-1:{AWS_ACCOUNT}:loadbalancer/app/acme-web/1a2b3c4d5e6f"
)

# --------------------------------------------------------------------------------------
# The uploaded policy: a synthetic internal standard, written the way a company writes one.
# --------------------------------------------------------------------------------------
INTERNAL_POLICY_MD = """# ACME 클라우드 인프라 보안 표준

## 1. 목적과 적용 범위

이 표준은 ACME가 AWS 상에서 운영하는 모든 서비스에 적용된다. 운영 환경과 스테이징 환경의
Terraform으로 관리되는 리소스가 대상이며, 개발자 개인 실험 계정은 제외한다.

## 2. 네트워크 노출 통제

- 외부에 노출되는 서버는 불필요한 Public IP를 사용하지 않는다. 프라이빗 서브넷에 배치된
  인스턴스에는 퍼블릭 IPv4 주소를 부여하지 않으며, 외부 트래픽은 로드밸런서를 통해서만 유입된다.
- 데이터베이스는 외부에서 직접 접근할 수 없도록 구성한다. 관리형 데이터베이스 인스턴스는
  퍼블릭 액세스를 비활성화하고 VPC 내부에서만 접속을 허용한다.
- 외부 통신은 승인된 Port만 허용한다. 보안그룹 인바운드 규칙은 서비스에 필요한 포트로 한정하고
  0.0.0.0/0 전체 개방을 금지한다.

## 3. 데이터 보호

- 중요 데이터는 저장 시 암호화한다. 관리형 데이터베이스의 스토리지 암호화를 활성화한다.
- 객체 스토리지 버킷은 모든 형태의 퍼블릭 액세스를 차단한다.
- 외부 서비스는 HTTPS 사용을 원칙으로 한다. 외부에 공개되는 로드밸런서는 HTTPS/TLS 리스너만
  노출하고 평문 HTTP 리스너를 두지 않는다.

## 4. 로그와 운영 기록

- 운영 환경의 중요 로그는 최소 1년간 보존한다. 데이터베이스 감사 로그와 로드밸런서 액세스 로그를
  중앙 로그 저장소로 내보낸다.
- 운영 Resource에는 회사 표준 Tag(Owner, CostCenter, Environment)를 적용한다.

## 5. 조직 통제

- 전 임직원은 연 1회 이상 정보보호 교육을 이수한다.
- 신규 외부 SaaS 도입은 CISO의 사전 승인을 받는다.
"""

MULTIRESOURCE_TF = """locals {
  name = "acme-app"
}

resource "aws_security_group" "rds" {
  name        = "${local.name}-rds"
  description = "Database ingress"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port   = 3306
    to_port     = 3306
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_db_subnet_group" "app" {
  name       = "${local.name}-db"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_instance" "app" {
  identifier = "acme-app-db"

  engine            = "mysql"
  instance_class    = "db.t3.micro"
  allocated_storage = 20

  db_subnet_group_name   = aws_db_subnet_group.app.name
  vpc_security_group_ids = [aws_security_group.rds.id]

  publicly_accessible                 = true
  storage_encrypted                   = false
  iam_database_authentication_enabled = false
  enabled_cloudwatch_logs_exports     = []

  skip_final_snapshot = true
}

resource "aws_lb" "web" {
  name               = "${local.name}-alb"
  load_balancer_type = "application"
  internal           = false
  subnets            = aws_subnet.public[*].id
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.web.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      message_body = "ok"
      status_code  = "200"
    }
  }
}
"""

STORAGE_TF = """resource "aws_s3_bucket" "media" {
  bucket = "acme-media-assets"
}

resource "aws_s3_bucket_public_access_block" "media" {
  bucket = aws_s3_bucket.media.id

  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}
"""

ASSESSMENT_PROFILE = ModelProfile(
    model_profile_id="assessment-nova-lite-m1-v3",
    role=ModelProfileRole.ASSESSMENT,
    region="us-east-1",
    model_id="amazon.nova-lite-v1:0",
    prompt_version="assessment-three-perspective-rubric-v3",
    rubric_version="m1-three-perspective-v1",
    golden_dataset_version="m3-s3-initial-post-deploy-six-rule-three-perspective-v1",
)
REMEDIATION_PROFILE = ModelProfile(
    model_profile_id="remediation-nova-lite-m1-v1",
    role=ModelProfileRole.REMEDIATION,
    region="us-east-1",
    model_id="amazon.nova-lite-v1:0",
    prompt_version="remediation-terraform-v1",
    rubric_version="remediation-terraform-v1",
    golden_dataset_version="remediation-terraform-v1",
)


# --------------------------------------------------------------------------------------
# In-memory AWS: DynamoDB table with the conditional writes the repositories rely on, S3.
# --------------------------------------------------------------------------------------
class E2ETable(FakeTable):
    """`FakeTable` (transactions) plus the resource-API calls the other repositories make."""

    def put_item(self, **kwargs: object) -> None:
        item = dict(kwargs["Item"])  # type: ignore[arg-type]
        key = (str(item["PK"]), str(item["SK"]))
        expression = kwargs.get("ConditionExpression")
        if expression is not None and "attribute_not_exists" in str(expression):
            if key in self.items:
                raise _Conditional()
        self.items[key] = item

    def update_item(self, **kwargs: object) -> None:
        key_data = kwargs["Key"]
        key = (str(key_data["PK"]), str(key_data["SK"]))  # type: ignore[index]
        item = self.items.get(key)
        if item is None:
            raise _Conditional()
        names = kwargs.get("ExpressionAttributeNames") or {}
        values = kwargs.get("ExpressionAttributeValues") or {}
        condition = str(kwargs.get("ConditionExpression") or "")
        for clause in filter(None, condition.split(" AND ")):
            field, _, placeholder = (part.strip() for part in clause.partition("="))
            field = names.get(field, field)  # type: ignore[union-attr]
            if item.get(field) != values.get(placeholder):  # type: ignore[union-attr]
                raise _Conditional()
        assignments = str(kwargs["UpdateExpression"]).removeprefix("SET ")
        for assignment in assignments.split(","):
            field, _, placeholder = (part.strip() for part in assignment.partition("="))
            item[names.get(field, field)] = values[placeholder]  # type: ignore[index]

    def query(self, **kwargs: object) -> dict[str, object]:
        self.query_calls += 1
        values = kwargs["ExpressionAttributeValues"]
        if kwargs.get("IndexName") == "GSI1":
            job_key = values[":job_id"]  # type: ignore[index]
            return {
                "Items": [
                    dict(item) for item in self.items.values() if item.get("GSI1PK") == job_key
                ]
            }
        condition = str(kwargs["KeyConditionExpression"])
        pk_placeholder = re.search(r"PK = (:\w+)", condition).group(1)  # type: ignore[union-attr]
        prefix_match = re.search(r"begins_with\(SK, (:\w+)\)", condition)
        pk = str(values[pk_placeholder])  # type: ignore[index]
        prefix = "" if prefix_match is None else str(values[prefix_match.group(1)])  # type: ignore[index]
        matched = [
            dict(item)
            for (item_pk, sk), item in sorted(self.items.items())
            if item_pk == pk and sk.startswith(prefix)
        ]
        filter_expression = kwargs.get("FilterExpression")
        if filter_expression is not None:
            for clause in str(filter_expression).split(" AND "):
                field, _, placeholder = (part.strip() for part in clause.partition("="))
                matched = [item for item in matched if item.get(field) == values[placeholder]]  # type: ignore[index]
        return {"Items": matched}


class _Conditional(Exception):
    def __init__(self) -> None:
        super().__init__("ConditionalCheckFailedException")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class S3:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, object]] = {}

    def generate_presigned_url(self, method, params, expires):
        return f"https://s3.example.invalid/{params['Key']}"

    def get_object(self, *, Bucket, Key):
        stored = self.objects[Key]
        return {**stored, "Body": BytesIO(stored["Body"])}  # type: ignore[arg-type]

    def put_object(self, *, Bucket, Key, Body, ContentType):
        self.objects[Key] = {"Body": Body, "ContentType": ContentType}


# --------------------------------------------------------------------------------------
# Synthetic models. They read exactly the request the production adapters send.
# --------------------------------------------------------------------------------------
class SyntheticAssessmentModel:
    """Deterministic stand-in for Bedrock: judges the supplied document against the rule.

    It never sees anything the real model would not see (the request body only), and it
    returns the same status/score for the same input every time. Scores are illustrative
    band values, not a rubric.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def converse(self, **kwargs: object) -> Mapping[str, object]:
        body = json.loads(kwargs["messages"][0]["content"][0]["text"])  # type: ignore[index]
        self.calls.append(body)
        rule_id: str = body["rule"]["rule_id"]
        document = body["resource_document"]
        allowed: list[str] = body["allowed_evidence_references"]
        verdict = _judge(rule_id, body["perspective"], document)
        if verdict is None:
            status, score = "INSUFFICIENT_EVIDENCE", 0
        else:
            status, score = ("PASS", 93) if verdict else ("FAIL", 12)
        return {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": json.dumps(
                                {
                                    "status": status,
                                    "score": score,
                                    "rationale": f"synthetic judgment for {rule_id}",
                                    "evidence_references": allowed[:2],
                                }
                            )
                        }
                    ]
                }
            }
        }


def _judge(rule_id: str, perspective: str, document: Mapping[str, object]) -> bool | None:
    """True = compliant, False = violating, None = the document carries no evidence."""
    files = document.get("files")
    text = (
        "\n".join(str(entry.get("content", "")) for entry in files)
        if isinstance(files, list)
        else json.dumps(document)
    )
    key = rule_id.upper()
    if "RDS_NOT_PUBLIC" in key or "RDS-PUBLIC" in key:
        if perspective == "IAC":
            return "publicly_accessible                 = true" not in text
        value = document.get("attributes", {}).get("db_instance", {}).get("PubliclyAccessible")
        return None if value is None else not value
    if "RDS_ENCRYPTION" in key or "RDS-ENCRYPT" in key:
        if perspective == "IAC":
            return "storage_encrypted                   = false" not in text
        value = document.get("attributes", {}).get("db_instance", {}).get("StorageEncrypted")
        return None if value is None else bool(value)
    if "S3_BLOCK_PUBLIC" in key or "S3-PUBLIC" in key:
        if perspective == "IAC":
            return "block_public_acls       = false" not in text
        block = document.get("attributes", {}).get("public_access_block")
        return None if not block else all(bool(v) for v in block.values())
    if "ALB_HTTPS" in key or "ALB-HTTPS" in key:
        if perspective == "IAC":
            return 'protocol          = "HTTP"' not in text
        listeners = document.get("attributes", {}).get("listeners")
        return None if listeners is None else all(li.get("Protocol") == "HTTPS" for li in listeners)
    return None


class SyntheticPatchModel:
    """Fixes `publicly_accessible` in the one file that declares the DB instance."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def converse(self, **kwargs: object) -> Mapping[str, object]:
        body = json.loads(kwargs["messages"][0]["content"][0]["text"])  # type: ignore[index]
        self.calls.append(body)
        changes: dict[str, str] = {}
        for entry in body["terraform_files"]:
            if 'resource "aws_db_instance"' in entry["content"]:
                changes[entry["path"]] = entry["content"].replace(
                    "publicly_accessible                 = true",
                    "publicly_accessible                 = false",
                )
        return {"output": {"message": {"content": [{"text": json.dumps({"changes": changes})}]}}}


# --------------------------------------------------------------------------------------
# AWS Actual documents, in the live adapters' projection shapes.
# --------------------------------------------------------------------------------------
def _actual_views() -> tuple[AwsResourceView, ...]:
    return (
        AwsResourceView(
            aws_account_id=AWS_ACCOUNT,
            resource_type="AWS::S3::Bucket",
            resource_id=BUCKET_ID,
            attributes={
                "public_access_block": {
                    "BlockPublicAcls": False,
                    "IgnorePublicAcls": False,
                    "BlockPublicPolicy": False,
                    "RestrictPublicBuckets": False,
                },
                "encryption": {},
                "policy": {},
            },
        ),
        AwsResourceView(
            aws_account_id=AWS_ACCOUNT,
            resource_type="AWS::EC2::Instance",
            resource_id=INSTANCE_ID,
            attributes={
                "instance": {
                    "InstanceId": INSTANCE_ID,
                    "State": {"Name": "running"},
                    "SubnetId": "subnet-private-1",
                    "VpcId": "vpc-1",
                    "PublicIpAddress": "3.3.3.3",
                },
                "network_interfaces": [
                    {"NetworkInterfaceId": "eni-1", "SubnetId": "subnet-private-1"}
                ],
                "volumes": [{"VolumeId": "vol-1", "Encrypted": False}],
                "security_groups": [],
            },
        ),
        AwsResourceView(
            aws_account_id=AWS_ACCOUNT,
            resource_type="AWS::RDS::DBInstance",
            resource_id=DB_ID,
            attributes={
                "db_instance": {
                    "DBInstanceIdentifier": DB_ID,
                    "DBInstanceStatus": "available",
                    "Engine": "mysql",
                    "PubliclyAccessible": True,
                    "StorageEncrypted": False,
                    "IAMDatabaseAuthenticationEnabled": False,
                    "EnabledCloudwatchLogsExports": [],
                },
                "db_subnet_group": {"DBSubnetGroupName": "acme-app-db", "VpcId": "vpc-1"},
                "vpc_security_groups": [],
            },
        ),
        AwsResourceView(
            aws_account_id=AWS_ACCOUNT,
            resource_type="AWS::ElasticLoadBalancingV2::LoadBalancer",
            resource_id=ALB_ARN,
            attributes={
                "load_balancer": {
                    "LoadBalancerArn": ALB_ARN,
                    "LoadBalancerName": "acme-web",
                    "Type": "application",
                    "Scheme": "internet-facing",
                },
                "listeners": [{"ListenerArn": f"{ALB_ARN}/l1", "Port": 80, "Protocol": "HTTP"}],
                "load_balancer_attributes": {"access_logs.s3.enabled": "false"},
            },
        ),
    )


def _iac_document() -> IaCDocument:
    return IaCDocument(
        customer_id=CUSTOMER,
        repository_id=REPOSITORY,
        commit_sha=COMMIT,
        files=(("multiresource.tf", MULTIRESOURCE_TF), ("storage.tf", STORAGE_TF)),
    )


# --------------------------------------------------------------------------------------
# Test doubles for the parts of the API composition that only dispatch.
# --------------------------------------------------------------------------------------
class _Queue:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def enqueue(self, request: object) -> None:
        self.requests.append(request)


class _WorkflowRepository:
    """Captures the Assessment/Remediation workflow writes the API would persist."""

    def __init__(self, table: E2ETable) -> None:
        self.table = table
        self.assessments: list[object] = []
        self.remediations: list[tuple] = []
        self.outbox: list[WorkflowOutboxEntry] = []

    def create_assessment_workflow(self, assessment, job, outbox) -> None:
        from apps.backend.repositories.dynamodb import _item_from_assessment, _item_from_job

        self.assessments.append(assessment)
        self.outbox.append(outbox)
        for item in (_item_from_assessment(assessment), _item_from_job(job)):
            self.table.items[(item["PK"], item["SK"])] = item

    def create_remediation_workflow(self, **kwargs) -> None:
        self.remediations.append(kwargs)
        self.outbox.append(kwargs["outbox"])

    def record_remediation_decision(self, **kwargs) -> None:
        self.remediations.append(kwargs)

    def list_pending_outbox(self, *, limit: int):
        return tuple(e for e in self.outbox if e.status is OutboxStatus.PENDING)[:limit]

    def mark_outbox_dispatched(self, entry) -> None:
        return None

    def record_outbox_dispatch_failure(self, entry) -> None:
        raise AssertionError("dispatch must not fail offline")


class _Dispatcher:
    def dispatch(self, task) -> None:
        return None


class _Scope:
    def authorize(self, principal, *, repository_id: str) -> None:
        return None


class _NoExceptions:
    def list_exceptions(self, *, customer_id, finding):
        return ()


def _admin() -> Principal:
    return Principal(
        subject="security-admin@acme.example",
        client_id="console",
        customer_id=CUSTOMER,
        roles=frozenset({Role.ADMIN}),
    )


# --------------------------------------------------------------------------------------
# Requirements the (text-blind) fake extractor proposes for the uploaded document.
# --------------------------------------------------------------------------------------
def _requirements(document) -> tuple[ExtractedRequirement, ...]:
    def locator(section: str, item: int) -> str:
        value = f"heading/{section}/item/{item}"
        assert document.unit(value) is not None, f"{value} is not a unit of the document"
        return value

    network = "acme-클라우드-인프라-보안-표준/2-네트워크-노출-통제"
    data = "acme-클라우드-인프라-보안-표준/3-데이터-보호"
    logs = "acme-클라우드-인프라-보안-표준/4-로그와-운영-기록"
    org = "acme-클라우드-인프라-보안-표준/5-조직-통제"
    return (
        ExtractedRequirement(
            source_locators=(locator(network, 2),),
            requirement="관리형 데이터베이스 인스턴스는 퍼블릭 액세스를 비활성화하고 VPC 내부에서만 접속을 허용한다.",
            requirement_summary="관리형 DB는 퍼블릭 액세스를 비활성화한다.",
            classification=CandidateClassification.AUTOMATABLE,
            mapping_reason="DB의 외부 직접 접근 금지는 RDS 퍼블릭 액세스 통제에 대응한다.",
            mapped_control_key="RDS_NOT_PUBLIC",
            resource_types=("AWS::RDS::DBInstance",),
            evaluation_type=RuleEvaluationType.HYBRID,
            required_evidence=("RDS.PUBLICLY_ACCESSIBLE",),
            optional_evidence=("RDS.IAC_PUBLICLY_ACCESSIBLE",),
            evaluation_rubric="FAIL when publicly_accessible is true; PASS when false.",
        ),
        ExtractedRequirement(
            source_locators=(locator(data, 1),),
            requirement="관리형 데이터베이스의 스토리지 암호화를 활성화한다.",
            requirement_summary="RDS 스토리지 암호화 활성화",
            classification=CandidateClassification.AUTOMATABLE,
            mapping_reason="저장 시 암호화 요구는 RDS 스토리지 암호화 통제에 대응한다.",
            mapped_control_key="RDS_ENCRYPTION_AT_REST",
            resource_types=("AWS::RDS::DBInstance",),
            evaluation_type=RuleEvaluationType.AWS,
            required_evidence=("RDS.STORAGE_ENCRYPTED",),
            evaluation_rubric="FAIL when StorageEncrypted is false.",
        ),
        ExtractedRequirement(
            source_locators=(locator(data, 2),),
            requirement="객체 스토리지 버킷은 모든 형태의 퍼블릭 액세스를 차단한다.",
            requirement_summary="S3 버킷 퍼블릭 액세스 차단",
            classification=CandidateClassification.AUTOMATABLE,
            mapping_reason="버킷 퍼블릭 액세스 차단 요구는 S3 Block Public Access 통제에 대응한다.",
            mapped_control_key="S3_BLOCK_PUBLIC_ACCESS",
            resource_types=("AWS::S3::Bucket",),
            evaluation_type=RuleEvaluationType.HYBRID,
            required_evidence=("S3.PUBLIC_ACCESS_BLOCK",),
            evaluation_rubric="FAIL when any of the four block-public-access flags is false.",
        ),
        ExtractedRequirement(
            source_locators=(locator(data, 3),),
            requirement="외부에 공개되는 로드밸런서는 HTTPS/TLS 리스너만 노출하고 평문 HTTP 리스너를 두지 않는다.",
            requirement_summary="공개 ALB는 HTTPS 리스너만 둔다.",
            classification=CandidateClassification.AUTOMATABLE,
            mapping_reason="HTTPS 원칙은 ALB HTTPS-only 통제에 대응한다.",
            mapped_control_key="ALB_HTTPS_ONLY",
            resource_types=("AWS::ElasticLoadBalancingV2::LoadBalancer",),
            evaluation_type=RuleEvaluationType.IAC,
            required_evidence=("ALB.IAC_LISTENER_PROTOCOL",),
            evaluation_rubric="FAIL when any listener protocol is HTTP.",
        ),
        ExtractedRequirement(
            source_locators=(locator(org, 1),),
            requirement="전 임직원은 연 1회 이상 정보보호 교육을 이수한다.",
            requirement_summary="연 1회 정보보호 교육",
            classification=CandidateClassification.MANUAL,
            mapping_reason="교육 이수는 클라우드 리소스 상태로 관찰할 수 없는 조직 통제다.",
            mapped_control_key=MANUAL_CONTROL_KEY,
            evaluation_type=RuleEvaluationType.MANUAL,
        ),
        ExtractedRequirement(
            source_locators=(locator(logs, 2),),
            requirement="운영 Resource에는 회사 표준 Tag(Owner, CostCenter, Environment)를 적용한다.",
            requirement_summary="표준 태그 적용",
            classification=CandidateClassification.UNSUPPORTED,
            mapping_reason="태그 표준을 평가하는 Catalog 통제가 없다.",
        ),
    )


class CustomerPolicyEndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self.table = E2ETable()
        self.s3 = S3()
        self.admin = _admin()
        self.model = SyntheticAssessmentModel()

    # ---- helpers -------------------------------------------------------------------

    def _approval_repository(self) -> DynamoDbPolicyApprovalRepository:
        return DynamoDbPolicyApprovalRepository(
            table_name="governance",
            transaction_client=self.table,  # type: ignore[arg-type]
            table=self.table,  # type: ignore[arg-type]
        )

    def _upload_and_normalize(self):
        uploads = PolicySourceApiService(
            repository=DynamoDbPolicySourceUploadRepository(
                table=self.table, bucket="policy-artifacts", presigner=self.s3
            ),
            source_id_factory=lambda: "src-acme-security-standard",
            source_version_factory=lambda: "2026-09-04",
        )
        payload = INTERNAL_POLICY_MD.encode("utf-8")
        session = uploads.create_upload_session(
            self.admin,
            PolicySourceUploadRequest(
                filename="acme-cloud-security-standard.md",
                declared_media_type="text/markdown",
                byte_size=len(payload),
            ),
        )
        original_key = session.upload_url.removeprefix("https://s3.example.invalid/")
        self.s3.objects[original_key] = {
            "Body": payload,
            "VersionId": "s3-v-001",
            "ContentType": "text/markdown",
        }
        return uploads, uploads.process_upload(
            self.admin,
            source_id=session.source_id,
            source_version=session.source_version,
            reader=self.s3,
        )

    def _extract(self, document):
        queue = _Queue()
        candidates = PolicyCandidateApiService(
            repository=self._approval_repository(),
            queue=queue,  # type: ignore[arg-type]
            run_id_factory=lambda: "authoring-run-1",
        )
        accepted = candidates.request_extraction(
            self.admin, source_id=document.source_id, source_version=document.source_version
        )
        self.assertIs(accepted.status, AuthoringRunStatus.QUEUED)
        request = queue.requests[0]
        # The Authoring Worker: read the normalized artifact the ingestion step wrote to S3.
        result = extract_policy_candidates(
            customer_id=CUSTOMER,
            document=document,
            artifact_reader=NormalizedArtifactReader(reader=self.s3, bucket="policy-artifacts"),  # type: ignore[arg-type]
            extractor=FakePolicyCandidateExtractor(_requirements(document)),
            catalog=MVP_CONTROL_CATALOG,
            authoring_run_id=request.authoring_run_id,
            requested_at=request.requested_at,
        )
        manifest = self._approval_repository().record_authoring_result(
            customer_id=CUSTOMER, result=result
        )
        self.assertIs(manifest.status, AuthoringRunStatus.READY)
        return candidates

    def _run_assessment(self, profile_id: str, *, aws_tool, github_tool):
        workflow = _WorkflowRepository(self.table)
        jobs = JobApiService(
            repository=workflow,
            assessment_scope=_Scope(),
            policy_catalog_factory=lambda *, customer_id: DynamoDbPolicyCatalog(
                self.table, customer_id=customer_id
            ),
            outbox_dispatcher=OutboxDispatcher(repository=workflow, dispatcher=_Dispatcher()),
            job_id_factory=lambda: f"job-{profile_id}",
            assessment_id_factory=lambda: f"asm-{profile_id}",
        )
        jobs.create_assessment(
            self.admin, AssessmentRequest(repository_id=REPOSITORY, policy_profile_id=profile_id)
        )
        assessment = workflow.assessments[0]

        # --- the live Worker path, with the runtime's own plan helpers ---
        resolver = PolicyContextResolver(DynamoDbPolicyCatalog(self.table, customer_id=CUSTOMER))
        report_store = DynamoDbAssessmentReportStore(self.table)
        works = tuple(
            AssessmentResourceWork(
                customer_id=CUSTOMER,
                assessment_id=assessment.assessment_id,
                job_id=assessment.job_id,
                revision=0,
                policy_profile_id=assessment.policy_profile_id,
                phase=AssessmentPhase.INITIAL,
                resource_id=view.resource_id,
                resource_type=view.resource_type,
                perspective=EvaluationPerspective.AWS_ACTUAL,
                model_profile_id=ASSESSMENT_PROFILE.model_profile_id,
                expected_profile_version=assessment.policy_profile_version,
                assessed_commit_sha=COMMIT,
            )
            for view in _actual_views()
        )
        works = _with_governance_work(works, resolver, repository_id=REPOSITORY)
        works = _with_complete_evaluation_plan(works, resolver)
        report_store.put_plan_if_absent(
            __import__(
                "apps.backend.assessment.reporting", fromlist=["x"]
            ).AssessmentEvaluationPlan(
                customer_id=CUSTOMER,
                assessment_id=assessment.assessment_id,
                planned_coordinates=works[0].planned_coordinates,
            )
        )
        works = _evaluable_works(works)
        result_store = DynamoDbEvaluationResultStore(self.table)
        task = WorkflowTask(
            job_id=assessment.job_id, expected_revision=0, command=WorkflowCommand.ASSESS_RESOURCE
        )
        iac = github_tool.read_iac_document(
            __import__("agent.runtime", fromlist=["x"]).IaCSnapshotRequest(
                customer_id=CUSTOMER, repository_id=REPOSITORY, commit_sha=COMMIT
            )
        )
        for work in works:
            if work.resource_type == GOVERNANCE_ASSESSMENT_RESOURCE_TYPE:
                runners = {
                    EvaluationPerspective.MANUAL: AssessmentRunner(ManualReviewEvaluator()),
                }
                derive_drift = False
            else:
                runners = {
                    EvaluationPerspective.IAC: AssessmentRunner(
                        BedrockStructuredEvaluator(
                            client=self.model,
                            perspective=EvaluationPerspective.IAC,
                            resource_document=iac.to_dict(),
                            evidence_references=iac.evidence_references,
                        )
                    ),
                    EvaluationPerspective.AWS_ACTUAL: AssessmentRunner(
                        ActualBedrockEvaluator(
                            evidence_loader=ActualEvidenceLoader(
                                tool=aws_tool,
                                customer_id=CUSTOMER,
                                aws_account_id=AWS_ACCOUNT,
                                resource_type=work.resource_type,
                            ),
                            client=self.model,
                        )
                    ),
                }
                derive_drift = True
            AssessmentWorker(
                work_repository=_OneWork(work),
                context_resolver=resolver,
                perspective_runners=runners,
                derive_drift=derive_drift,
                model_profiles=InMemoryModelProfileRegistry((ASSESSMENT_PROFILE,)),
                result_store=result_store,
            ).handle(task)
        return assessment, report_store.get_report(
            customer_id=CUSTOMER, assessment_id=assessment.assessment_id
        )

    # ---- the path ------------------------------------------------------------------

    def test_an_uploaded_company_standard_drives_assessment_findings_and_remediation(self):
        # 1. Upload + normalize with the real Markdown parser.
        uploads, document = self._upload_and_normalize()
        self.assertEqual(document.status.value, "READY")
        locators = {unit.locator for unit in document.units}
        self.assertIn(
            "heading/acme-클라우드-인프라-보안-표준/2-네트워크-노출-통제/item/2", locators
        )
        listed = uploads.list_sources(self.admin)
        self.assertEqual(listed[0]["source_id"], document.source_id)

        # 2. Candidate extraction (worker) and reviewer read-back.
        candidates = self._extract(document)
        page = candidates.list_candidates(
            self.admin, source_id=document.source_id, source_version=document.source_version
        )
        self.assertEqual(page.status, AuthoringRunStatus.READY)
        automatable = [
            c for c in page.candidates if c.evaluation_type is not RuleEvaluationType.MANUAL
        ]
        manual = [c for c in page.candidates if c.evaluation_type is RuleEvaluationType.MANUAL]
        self.assertEqual(len(automatable), 4)
        self.assertEqual(len(manual), 1)
        self.assertEqual(len(page.unsupported), 1)  # 표준 Tag: no catalog control
        self.assertEqual(page.rejected, ())
        for entry in automatable:
            self.assertTrue(entry.rule_id.startswith("CUST-"))
            self.assertEqual(entry.rule_version, document.source_version)
            self.assertEqual(entry.locators[0].source_id, document.source_id)
            self.assertEqual(
                entry.locators[0].content_sha256,
                document.unit(entry.locators[0].locator).text_sha256,  # type: ignore[union-attr]
            )

        # 3. Partial approval (everything approvable) and publication.
        approvals = PolicyApprovalApiService(self._approval_repository())
        references = tuple(
            PolicyRuleReference(rule_id=c.rule_id, version=c.rule_version) for c in page.candidates
        )
        approval = approvals.approve(
            self.admin,
            source_id=document.source_id,
            source_version=document.source_version,
            approved_rules=references,
        )
        self.assertEqual(set(approval.approved_rules), set(references))
        profile = approvals.publish(
            self.admin,
            sources=((document.source_id, document.source_version),),
            policy_profile_id="profile-acme-standard",
            version="v1",
        )
        self.assertEqual(len(profile.rule_references), 5)

        # 4. Assessment pins the published version and evaluates only approved rules.
        aws_tool = MockAwsResourceTool(
            customer_id=CUSTOMER, aws_account_id=AWS_ACCOUNT, resources=_actual_views()
        )
        github_tool = MockGitHubTool(
            customer_id=CUSTOMER,
            repository_id=REPOSITORY,
            snapshots=(),
            documents=(_iac_document(),),
        )
        assessment, report = self._run_assessment(
            "profile-acme-standard", aws_tool=aws_tool, github_tool=github_tool
        )
        self.assertEqual(assessment.policy_profile_version, "v1")

        # Every evaluated rule is a customer rule; no fixture rule leaked into the plan.
        evaluated_rules = {result.rule_id for result in report.results}
        self.assertTrue(all(rule.startswith("CUST-") for rule in evaluated_rules), evaluated_rules)
        fixture_rule_ids = {rule.rule_id for rule in load_rule_registry(RULES_PATH).rules}
        self.assertFalse(evaluated_rules & fixture_rule_ids)

        # Coverage is complete: HYBRID rules produce IAC+AWS_ACTUAL+DRIFT, AWS-only produces
        # AWS_ACTUAL, IAC-only produces IAC, MANUAL produces the governance MANUAL coordinate.
        self.assertEqual(report.coverage.percentage, 100.0)
        self.assertEqual(report.coverage.planned_evaluations, 3 + 1 + 3 + 1 + 1)
        self.assertIsNotNone(report.readiness_score)
        by_key = {(r.rule_id.split("-")[1], r.perspective): r for r in report.results}
        self.assertIs(
            by_key[("RDS_NOT_PUBLIC", EvaluationPerspective.IAC)].status, EvaluationStatus.FAIL
        )
        self.assertIs(
            by_key[("RDS_NOT_PUBLIC", EvaluationPerspective.AWS_ACTUAL)].status,
            EvaluationStatus.FAIL,
        )
        # IaC and Actual agree (both FAIL) → DRIFT PASS, per the derivation table.
        self.assertIs(
            by_key[("RDS_NOT_PUBLIC", EvaluationPerspective.DRIFT)].status, EvaluationStatus.PASS
        )
        self.assertIs(
            by_key[("ALB_HTTPS_ONLY", EvaluationPerspective.IAC)].status, EvaluationStatus.FAIL
        )
        self.assertNotIn(("ALB_HTTPS_ONLY", EvaluationPerspective.AWS_ACTUAL), by_key)
        manual_result = by_key[
            ("ORGANIZATIONAL_CONTROL_MANUAL_REVIEW", EvaluationPerspective.MANUAL)
        ]
        self.assertIs(manual_result.status, EvaluationStatus.MANUAL_REVIEW)
        self.assertEqual(manual_result.resource_id, f"governance:{REPOSITORY}")
        for result in report.results:
            self.assertTrue(0 <= result.score <= 100)
            if result.status in {
                EvaluationStatus.MANUAL_REVIEW,
                EvaluationStatus.INSUFFICIENT_EVIDENCE,
            }:
                self.assertEqual(result.score, 0.0)

        # 5. Findings cite the customer's own document and the read that observed the state.
        findings = {(f.rule_id.split("-")[1], f.perspective): f for f in report.findings}
        rds_finding = findings[("RDS_NOT_PUBLIC", EvaluationPerspective.AWS_ACTUAL)]
        self.assertEqual(rds_finding.resource_id, DB_ID)
        self.assertEqual(rds_finding.severity, "CRITICAL")
        self.assertEqual(rds_finding.assessed_commit_sha, COMMIT)
        self.assertIn(f"aws:rds:db-instance/{DB_ID}#read-resource", rds_finding.evidence_references)
        self.assertTrue(
            any(
                ref.startswith(f"{document.source_id}@{document.source_version}#")
                for ref in rds_finding.evidence_references
            )
        )
        # PASS never becomes a Finding; the governance MANUAL coordinate does.
        self.assertNotIn(("RDS_NOT_PUBLIC", EvaluationPerspective.DRIFT), findings)
        self.assertIn(
            ("ORGANIZATIONAL_CONTROL_MANUAL_REVIEW", EvaluationPerspective.MANUAL), findings
        )

        # 6. Remediation decision for a customer-authored rule. The committed eligibility
        #    policy (fixtures/rules/remediation.json) knows fixture rule ids only, so B's policy
        #    closes the gate with MANUAL_REVIEW/RULE_NOT_IN_SCOPE. This is the documented gap:
        #    authored rules need an eligibility decision keyed by control, not by fixture id.
        decision = self._decide(rds_finding.finding_id)
        self.assertIs(decision.action, RemediationAction.MANUAL_REVIEW)
        self.assertIs(decision.manual_review_code, ManualReviewCode.RULE_NOT_IN_SCOPE)

    def test_a_fixture_profile_finding_reaches_a_snapshot_bound_patch_and_pull_request(self):
        # Publish the committed ISMS-P/internal-checklist Registry to this customer's catalog.
        published = DynamoDbPolicyCatalogBootstrap(self.table, customer_id=CUSTOMER).publish(
            load_rule_registry(RULES_PATH)
        )
        self.assertGreater(published, 0)
        aws_tool = MockAwsResourceTool(
            customer_id=CUSTOMER, aws_account_id=AWS_ACCOUNT, resources=_actual_views()
        )
        github_tool = MockGitHubTool(
            customer_id=CUSTOMER,
            repository_id=REPOSITORY,
            snapshots=(),
            documents=(_iac_document(),),
        )
        assessment, report = self._run_assessment(
            "profile-multiresource-baseline", aws_tool=aws_tool, github_tool=github_tool
        )
        self.assertEqual(assessment.policy_profile_version, "v1")
        self.assertEqual(report.coverage.percentage, 100.0)
        # 15 legacy rules × 3 perspectives, each resource only sees its own rules.
        self.assertEqual(report.coverage.planned_evaluations, 15 * 3)

        rds_iac = next(
            f
            for f in report.findings
            if f.rule_id == "RDS-PUBLIC-001" and f.perspective is EvaluationPerspective.IAC
        )
        # 7. B decides TERRAFORM_PATCH (RDS-PUBLIC-001 is AUTOMATIC) …
        decision = self._decide(rds_iac.finding_id)
        self.assertIs(decision.action, RemediationAction.TERRAFORM_PATCH)

        # 8. … and the Worker produces a snapshot-bound minimal patch plus a PR with the diff.
        context = DynamoDbRemediationContextReader(self.table).get_context(
            customer_id=CUSTOMER, finding_id=rds_iac.finding_id
        )
        target = DynamoDbRemediationContextReader(self.table).get_target(
            customer_id=CUSTOMER, finding_id=rds_iac.finding_id
        )
        self.assertEqual(target.resource_type, "AWS::RDS::DBInstance")
        work = RemediationWork(
            customer_id=CUSTOMER,
            remediation_id="rem-rds-public",
            job_id="job-rem-1",
            revision=0,
            context=context,
            decision=decision,
        )
        content_store = InMemoryPatchContentStore()
        writer = MockGitHubWriteTool(customer_id=CUSTOMER, repository_id=REPOSITORY)
        results = _RemediationResults()
        patch_model = SyntheticPatchModel()
        RemediationWorker(
            work_repository=_OneRemediation(work),
            patch_action=BedrockPatchGenerator(
                client=patch_model,
                model_profile=REMEDIATION_PROFILE,
                content_store=content_store,
                iac_documents=github_tool,
            ),
            sync_action=SnapshotSyncAction(),
            result_store=results,
            pull_request_action=PatchPullRequestAction(
                writer=writer, content_store=content_store, iac_documents=github_tool
            ),
        ).handle(
            WorkflowTask(
                job_id="job-rem-1",
                expected_revision=0,
                command=WorkflowCommand.GENERATE_REMEDIATION,
            )
        )
        patch = results.patch
        self.assertEqual(patch.changed_paths, ("multiresource.tf",))
        self.assertEqual(patch.base_commit_sha, COMMIT)
        # The model saw the whole Terraform body of the assessed commit.
        self.assertEqual(
            [entry["path"] for entry in patch_model.calls[0]["terraform_files"]],
            ["multiresource.tf", "storage.tf"],
        )
        stored = content_store.get(patch=patch)
        new_text = stored.changes["multiresource.tf"]
        self.assertIn("publicly_accessible                 = false", new_text)
        # Minimal: exactly one line differs, every resource block survives.
        original_lines = MULTIRESOURCE_TF.splitlines()
        changed = [
            (old, new)
            for old, new in zip(original_lines, new_text.splitlines(), strict=True)
            if old != new
        ]
        self.assertEqual(len(changed), 1)
        self.assertEqual(
            changed[0],
            (
                "  publicly_accessible                 = true",
                "  publicly_accessible                 = false",
            ),
        )
        description = writer.descriptions[0] or ""
        self.assertIn("```diff", description)
        self.assertIn("-  publicly_accessible                 = true", description)
        self.assertIn("+  publicly_accessible                 = false", description)
        self.assertIn("RDS-PUBLIC-001@2026-09-03", description)
        self.assertEqual(results.pull_request.finding_id, rds_iac.finding_id)  # type: ignore[union-attr]

    # ---- remediation decision through the real API service ----------------------------

    def _decide(self, finding_id: str):
        workflow = _WorkflowRepository(self.table)
        reader = DynamoDbRemediationContextReader(self.table)
        service = RemediationApiService(
            contexts=reader,
            targets=reader,
            exceptions=_NoExceptions(),
            decision_maker=load_rule_registry(RULES_PATH).remediation,
            repository=workflow,
            outbox_dispatcher=OutboxDispatcher(repository=workflow, dispatcher=_Dispatcher()),
            now=lambda: datetime.now(UTC),
            job_id_factory=lambda: "job-rem-1",
            remediation_id_factory=lambda: "rem-1",
        )
        return service.create_remediation(self.admin, finding_id).decision


class _OneWork:
    def __init__(self, work: AssessmentResourceWork) -> None:
        self.work = work

    def get_resource_work(self, *, job_id: str, expected_revision: int):
        return replace(self.work) if job_id == self.work.job_id else None


class _OneRemediation:
    def __init__(self, work: RemediationWork) -> None:
        self.work = work

    def get_work(self, *, job_id: str, expected_revision: int):
        return self.work


class _RemediationResults:
    def __init__(self) -> None:
        self.patch = None
        self.pull_request = None

    def put_result_if_absent(self, *, work, result) -> None:
        self.patch = result

    def put_pull_request_if_absent(self, *, work, pull_request) -> None:
        self.pull_request = pull_request


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
