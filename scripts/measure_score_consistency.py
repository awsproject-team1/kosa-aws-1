"""Measure how stable the evaluation is when the same input is evaluated repeatedly.

이 스크립트는 **Runtime이 쓰는 그 경로**로 대표 Case를 N회 반복 평가하고 통계를 낸다. AWS_ACTUAL
Case는 `ActualBedrockEvaluator`를 지난다 — 근거 게이트, 결정적 판정, 모델 호출이 Worker와 같은
순서로 일어난다. IAC Case는 `BedrockStructuredEvaluator`다. 처음 버전은 모델 어댑터만 직접 만들어
게이트와 결정적 경로를 통째로 우회했고, 그래서 그 두 경로가 고친 것을 이 도구로 확인할 수 없었다.

허용 오차나 합격 기준을 정하지 않는다 — 측정값과 계약 위반을 그대로 보고하고 판단은 사람이 한다
(ADR-0003 정정, ADR-0024). **지표는 판정 주체별로 나뉜다.** 코드가 판정한 좌표(`decided_by=CODE`)는
같은 입력에 항상 같은 답을 내므로 반복 일치·분산은 정보가 아니고 기대 status 정확도만 뜻이 있다.
모델이 판정한 좌표(`MODEL`)는 반복 일치와 정확도를 둘 다 본다. `model_calls`는 실제로 Bedrock을
부른 횟수이며, 그 수가 곧 비용이다.

score는 status의 재진술이다(PASS 100, FAIL 0). 그래서 score 통계는 status 뒤집힘의 통계이며,
모델이 돌려준 숫자 자체는 결과에 남지 않는다.

Case 종류
- self-agreement: 같은 입력 N회 (RDS/S3/ALB/EC2 위반·준수, IAC/AWS_ACTUAL 관점)
- partial-compliance: 일부만 충족한 리소스. **연속 점수가 등급을 담는지 확인하는 유일한 Case**이며,
  전부/전무 Case만으로는 0/100 분포가 Case 설계 탓인지 모델 탓인지 구별할 수 없다.
- expected transition: before(위반) → after(준수) 쌍. after 평균이 before 평균보다 낮으면 방향 위반.
- invariance/attribute-order: 같은 문서의 key 순서만 바꾼 입력. 평가기의 request body는
  `sort_keys=True`라 prompt 바이트가 같아야 한다 — 모델을 부르지 않고 결정적으로 확인한다.
- invariance/policy-phrasing: 같은 Control에 대한 표현만 다른 authored Rule 두 개. 같은 문서를 각각
  평가해 평균 score·status를 나란히 보고한다.

Severe Overestimation 후보는 새 전역 기준이 아니라 Golden Case가 이미 쓰는 기대 범위(위반 Case
`expected_score_max`=30, `fixtures/m1/golden_dataset_cases.json`)를 넘는 FAIL 결과다. 후보로 기록만
한다.

실행
    python scripts/measure_score_consistency.py --dry-run                     # 배관 검증(가짜 모델)
    python scripts/measure_score_consistency.py --repetitions 5 --output out.json --markdown out.md
라이브 실행은 Bedrock 호출 권한이 있는 AWS 자격 증명이 필요하다. 응답 원문은 저장하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.runtime import AwsResourceView, MockAwsResourceTool  # noqa: E402
from apps.backend.assessment.actual import ActualEvidenceLoader  # noqa: E402
from apps.backend.assessment.actual_evaluator import ActualBedrockEvaluator  # noqa: E402
from apps.backend.assessment.bedrock import (  # noqa: E402
    BedrockEvaluationError,
    BedrockStructuredEvaluator,
)
from apps.backend.assessment.findings import finding_from_result  # noqa: E402
from apps.backend.policy import PolicyContext  # noqa: E402
from apps.backend.policy.control_catalog import CONTROL_CATALOG_VERSION  # noqa: E402
from apps.backend.policy.registry import load_rule_registry  # noqa: E402
from packages.contracts import (  # noqa: E402
    AssessmentPhase,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    ModelProfile,
    ModelProfileRole,
    PolicyRule,
    RuleEvaluationType,
    RuleSeverity,
    SourceReference,
)

#: Golden Case가 위반 Case에 쓰는 기대 상한. 새 기준이 아니라 기존 fixture의 값이다.
GOLDEN_FAIL_SCORE_MAX = 30

AWS_ACCOUNT = "111122223333"
DB_ID = "acme-app-db"
BUCKET_ID = "acme-media-assets"
INSTANCE_ID = "i-0f3a1c9d2e4b5a678"
ALB_ARN = f"arn:aws:elasticloadbalancing:us-east-1:{AWS_ACCOUNT}:loadbalancer/app/acme-web/1a2b3c"


@dataclass(frozen=True, slots=True, kw_only=True)
class ConsistencyCase:
    """One immutable evaluator input, repeated N times."""

    case_id: str
    kind: str
    rule: PolicyRule
    perspective: EvaluationPerspective
    resource_id: str
    resource_document: Mapping[str, object]
    evidence_references: tuple[str, ...]
    expected_status: EvaluationStatus
    #: transition 쌍의 before Case id (after Case에만 있다).
    transition_from: str | None = None
    #: policy-phrasing 쌍의 상대 Case id.
    phrasing_pair: str | None = None


@dataclass(slots=True)
class RunRecord:
    status: str | None
    score: float | None
    evidence: tuple[str, ...]
    finding: bool | None
    rationale_length: int
    contract_error: str | None = None
    #: `--show-rationales`일 때만 채운다. 합성 문서에 대한 모델 문장이며 고객 데이터가 아니다.
    rationale: str | None = None
    #: 누가 status를 정했는가(`CODE` | `MODEL`). `MODEL`인 실행만 Bedrock을 불렀다.
    decided_by: str | None = None


@dataclass(slots=True)
class CaseReport:
    case_id: str
    kind: str
    rule_id: str
    perspective: str
    expected_status: str
    runs: list[RunRecord] = field(default_factory=list)

    @property
    def scores(self) -> list[float]:
        return [run.score for run in self.runs if run.score is not None]

    def summary(self) -> dict[str, object]:
        scores = self.scores
        statuses = [run.status for run in self.runs if run.status is not None]
        findings = [run.finding for run in self.runs if run.finding is not None]
        mode_status = _mode(statuses)
        mode_finding = _mode(findings)
        pairwise = [abs(a - b) for index, a in enumerate(scores) for b in scores[index + 1 :]]
        expected_hits = sum(1 for status in statuses if status == self.expected_status)
        overestimation = [
            score
            for run in self.runs
            if run.status == EvaluationStatus.FAIL.value
            and run.score is not None
            and (score := run.score) > GOLDEN_FAIL_SCORE_MAX
        ]
        contradictions = [
            run.status
            for run in self.runs
            if run.status in {"MANUAL_REVIEW", "INSUFFICIENT_EVIDENCE", "OUT_OF_SCOPE"}
            and run.score not in (None, 0, 0.0)
        ]
        sources = [run.decided_by for run in self.runs if run.decided_by is not None]
        return {
            "case_id": self.case_id,
            "kind": self.kind,
            "rule_id": self.rule_id,
            "perspective": self.perspective,
            "expected_status": self.expected_status,
            # 판정 주체. 한 Case 안에서 갈리면 안 된다 — 같은 입력이 게이트를 다르게 통과했다는 뜻.
            "decided_by": _mode(sources),
            "decision_source_agreement": _share(sources, _mode(sources)),
            "model_calls": sum(1 for source in sources if source == "MODEL"),
            "runs": len(self.runs),
            "scores": scores,
            "mean": round(statistics.fmean(scores), 2) if scores else None,
            "min": min(scores) if scores else None,
            "max": max(scores) if scores else None,
            "range": (max(scores) - min(scores)) if scores else None,
            "stdev": round(statistics.stdev(scores), 2) if len(scores) > 1 else 0.0,
            "mean_pairwise_diff": round(statistics.fmean(pairwise), 2) if pairwise else 0.0,
            "max_pairwise_diff": max(pairwise) if pairwise else 0.0,
            "status_mode": mode_status,
            "status_agreement": _share(statuses, mode_status),
            "expected_status_accuracy": (expected_hits / len(statuses)) if statuses else None,
            "finding_agreement": _share(findings, mode_finding),
            "evidence_agreement": _share(
                [run.evidence for run in self.runs if run.status is not None],
                _mode([run.evidence for run in self.runs if run.status is not None]),
            ),
            "contract_errors": [run.contract_error for run in self.runs if run.contract_error],
            "evidence_per_run": [list(run.evidence) for run in self.runs],
            "rationales": [run.rationale for run in self.runs if run.rationale is not None],
            "non_judgment_score_contradictions": contradictions,
            "severe_overestimation_candidates": overestimation,
        }


def _mode(values: Sequence[object]) -> object | None:
    if not values:
        return None
    counts: dict[object, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return max(counts.items(), key=lambda item: item[1])[0]


def _share(values: Sequence[object], mode: object) -> float | None:
    if not values:
        return None
    return round(sum(1 for value in values if value == mode) / len(values), 4)


# --------------------------------------------------------------------------------------
# Built-in cases
# --------------------------------------------------------------------------------------
def _iac_document(files: Mapping[str, str]) -> dict[str, object]:
    return {
        "customer_id": "acme-cloud",
        "repository_id": "acme-platform-iac",
        "commit_sha": "d6b2c119872e20a890e14cb6bc41017527e600e6",
        "files": [{"path": path, "content": content} for path, content in files.items()],
    }


def _iac_evidence(files: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(f"terraform:{path}" for path in files)


RDS_TF = """resource "aws_db_instance" "app" {
  identifier          = "acme-app-db"
  engine              = "mysql"
  instance_class      = "db.t3.micro"
  allocated_storage   = 20
  publicly_accessible = %(public)s
  storage_encrypted   = %(encrypted)s
  skip_final_snapshot = true
}
"""
S3_TF = """resource "aws_s3_bucket" "media" {
  bucket = "acme-media-assets"
}

resource "aws_s3_bucket_public_access_block" "media" {
  bucket                  = aws_s3_bucket.media.id
  block_public_acls       = %(flag)s
  block_public_policy     = %(flag)s
  ignore_public_acls      = %(flag)s
  restrict_public_buckets = %(flag)s
}
"""
ALB_TF = """resource "aws_lb" "web" {
  name               = "acme-web"
  load_balancer_type = "application"
  internal           = false
}

resource "aws_lb_listener" "front" {
  load_balancer_arn = aws_lb.web.arn
  port              = %(port)s
  protocol          = "%(protocol)s"%(tls)s
}
"""
EC2_TF = """resource "aws_subnet" "private" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.10.0/24"
  map_public_ip_on_launch = false
}

resource "aws_instance" "app" {
  ami                         = "ami-0123456789abcdef0"
  instance_type               = "t3.micro"
  subnet_id                   = aws_subnet.private.id
  associate_public_ip_address = %(public)s
}
"""


def _rds_actual(*, public: bool, encrypted: bool) -> dict[str, object]:
    return {
        "resource_type": "AWS::RDS::DBInstance",
        "resource_id": DB_ID,
        "attributes": {
            "db_instance": {
                "DBInstanceIdentifier": DB_ID,
                "DBInstanceStatus": "available",
                "Engine": "mysql",
                "PubliclyAccessible": public,
                "StorageEncrypted": encrypted,
                "KmsKeyId": "arn:aws:kms:us-east-1:111122223333:key/k1" if encrypted else None,
                "IAMDatabaseAuthenticationEnabled": False,
                "EnabledCloudwatchLogsExports": [],
            },
            "db_subnet_group": {"DBSubnetGroupName": "acme-app-db", "VpcId": "vpc-1"},
            "vpc_security_groups": [],
        },
    }


def _s3_actual(*, blocked: bool) -> dict[str, object]:
    return {
        "resource_type": "AWS::S3::Bucket",
        "resource_id": BUCKET_ID,
        "attributes": {
            "public_access_block": {
                "BlockPublicAcls": blocked,
                "IgnorePublicAcls": blocked,
                "BlockPublicPolicy": blocked,
                "RestrictPublicBuckets": blocked,
            },
            "encryption": {},
            "policy": {"IsPublic": not blocked},
        },
    }


def _alb_actual(*, https: bool) -> dict[str, object]:
    listener = {"ListenerArn": f"{ALB_ARN}/l1", "Port": 443 if https else 80}
    if https:
        listener.update(
            {
                "Protocol": "HTTPS",
                "SslPolicy": "ELBSecurityPolicy-TLS13-1-2-2021-06",
                "Certificates": [{"CertificateArn": "arn:aws:acm:us-east-1:111122223333:cert/1"}],
            }
        )
    else:
        listener["Protocol"] = "HTTP"
    return {
        "resource_type": "AWS::ElasticLoadBalancingV2::LoadBalancer",
        "resource_id": ALB_ARN,
        "attributes": {
            "load_balancer": {
                "LoadBalancerArn": ALB_ARN,
                "LoadBalancerName": "acme-web",
                "Type": "application",
                "Scheme": "internet-facing",
                "State": {"Code": "active"},
                "VpcId": "vpc-1",
            },
            "listeners": [listener],
            "load_balancer_attributes": {"access_logs.s3.enabled": "false"},
        },
    }


def _ec2_actual(*, public: bool) -> dict[str, object]:
    instance = {
        "InstanceId": INSTANCE_ID,
        "State": {"Name": "running"},
        "SubnetId": "subnet-private-1",
        "VpcId": "vpc-1",
    }
    interface: dict[str, object] = {"NetworkInterfaceId": "eni-1", "SubnetId": "subnet-private-1"}
    if public:
        instance["PublicIpAddress"] = "3.3.3.3"
        instance["PublicDnsName"] = "ec2-3-3-3-3.compute-1.amazonaws.com"
        interface["Association"] = {"PublicIp": "3.3.3.3"}
    return {
        "resource_type": "AWS::EC2::Instance",
        "resource_id": INSTANCE_ID,
        "attributes": {
            "instance": instance,
            "network_interfaces": [interface],
            "volumes": [{"VolumeId": "vol-1", "Encrypted": True}],
            "security_groups": [],
        },
    }


def _s3_partial(flags: Mapping[str, bool]) -> dict[str, object]:
    """A bucket whose Block Public Access is only partly enabled."""
    return {
        "resource_type": "AWS::S3::Bucket",
        "resource_id": BUCKET_ID,
        "attributes": {
            "public_access_block": dict(flags),
            "encryption": {},
            "policy": {"IsPublic": not all(flags.values())},
        },
    }


def _alb_mixed_listeners() -> dict[str, object]:
    """HTTPS **and** a plaintext HTTP listener: the rule allows HTTPS/TLS listeners only."""
    document = _alb_actual(https=True)
    document["attributes"]["listeners"] = [
        *document["attributes"]["listeners"],
        {"ListenerArn": f"{ALB_ARN}/l2", "Port": 80, "Protocol": "HTTP"},
    ]
    return document


def _rds_open_security_group() -> dict[str, object]:
    """Not publicly accessible, yet 3306 is open to the internet and IAM auth is off."""
    document = _rds_actual(public=False, encrypted=True)
    document["attributes"]["vpc_security_groups"] = [
        {
            "VpcSecurityGroupId": "sg-rds",
            "Status": "active",
            "GroupId": "sg-rds",
            "GroupName": "acme-rds",
            "VpcId": "vpc-1",
            "IpPermissions": [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 3306,
                    "ToPort": 3306,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                }
            ],
        }
    ]
    return document


def _actual_evidence(resource_type: str, resource_id: str) -> tuple[str, ...]:
    from apps.backend.assessment.actual import actual_evidence_reference

    return (actual_evidence_reference(resource_type, resource_id),)


def _authored_rule(rule_id: str, title: str, rubric: str) -> PolicyRule:
    return PolicyRule(
        rule_id=rule_id,
        version="2026-09-04",
        title=title,
        severity=RuleSeverity.CRITICAL,
        applicable_phases=(AssessmentPhase.INITIAL,),
        resource_types=("AWS::RDS::DBInstance",),
        source_references=(
            SourceReference(
                source_id="src-acme-security-standard",
                source_version="2026-09-04",
                locator="heading/acme-standard/2-network/item/2",
                content_sha256="0" * 64,
            ),
        ),
        control_key="RDS_NOT_PUBLIC",
        control_catalog_version=CONTROL_CATALOG_VERSION,
        evaluation_type=RuleEvaluationType.HYBRID,
        required_evidence=("RDS.PUBLICLY_ACCESSIBLE",),
        optional_evidence=("RDS.IAC_PUBLICLY_ACCESSIBLE",),
        evaluation_rubric=rubric,
    )


def builtin_cases(rules_path: Path) -> tuple[ConsistencyCase, ...]:
    registry = load_rule_registry(rules_path)
    rules = {rule.rule_id: rule for rule in registry.rules}
    iac, actual = EvaluationPerspective.IAC, EvaluationPerspective.AWS_ACTUAL
    rds = "AWS::RDS::DBInstance"
    s3 = "AWS::S3::Bucket"
    alb = "AWS::ElasticLoadBalancingV2::LoadBalancer"
    ec2 = "AWS::EC2::Instance"

    def case(
        case_id: str,
        kind: str,
        rule: PolicyRule,
        perspective: EvaluationPerspective,
        resource_id: str,
        document: Mapping[str, object],
        evidence: tuple[str, ...],
        expected: EvaluationStatus,
        **extra: object,
    ) -> ConsistencyCase:
        return ConsistencyCase(
            case_id=case_id,
            kind=kind,
            rule=rule,
            perspective=perspective,
            resource_id=resource_id,
            resource_document=document,
            evidence_references=evidence,
            expected_status=expected,
            **extra,  # type: ignore[arg-type]
        )

    rds_public_tf = {"database.tf": RDS_TF % {"public": "true", "encrypted": "false"}}
    rds_private_tf = {"database.tf": RDS_TF % {"public": "false", "encrypted": "false"}}
    s3_open_tf = {"storage.tf": S3_TF % {"flag": "false"}}
    s3_blocked_tf = {"storage.tf": S3_TF % {"flag": "true"}}
    alb_http_tf = {"loadbalancer.tf": ALB_TF % {"port": 80, "protocol": "HTTP", "tls": ""}}
    alb_https_tf = {
        "loadbalancer.tf": ALB_TF
        % {
            "port": 443,
            "protocol": "HTTPS",
            "tls": (
                '\n  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"'
                '\n  certificate_arn   = "arn:aws:acm:us-east-1:111122223333:certificate/1"'
            ),
        }
    }
    ec2_public_tf = {"compute.tf": EC2_TF % {"public": "true"}}
    ec2_private_tf = {"compute.tf": EC2_TF % {"public": "false"}}

    fail, ok = EvaluationStatus.FAIL, EvaluationStatus.PASS
    cases = [
        # --- RDS public access (CRITICAL) ---
        case(
            "rds-public-iac",
            "self-agreement",
            rules["RDS-PUBLIC-001"],
            iac,
            DB_ID,
            _iac_document(rds_public_tf),
            _iac_evidence(rds_public_tf),
            fail,
        ),
        case(
            "rds-private-iac",
            "expected-transition",
            rules["RDS-PUBLIC-001"],
            iac,
            DB_ID,
            _iac_document(rds_private_tf),
            _iac_evidence(rds_private_tf),
            ok,
            transition_from="rds-public-iac",
        ),
        case(
            "rds-public-actual",
            "self-agreement",
            rules["RDS-PUBLIC-001"],
            actual,
            DB_ID,
            _rds_actual(public=True, encrypted=True),
            _actual_evidence(rds, DB_ID),
            fail,
        ),
        case(
            "rds-private-actual",
            "expected-transition",
            rules["RDS-PUBLIC-001"],
            actual,
            DB_ID,
            _rds_actual(public=False, encrypted=True),
            _actual_evidence(rds, DB_ID),
            ok,
            transition_from="rds-public-actual",
        ),
        # --- RDS storage encryption (HIGH) ---
        case(
            "rds-unencrypted-actual",
            "self-agreement",
            rules["RDS-ENCRYPT-001"],
            actual,
            DB_ID,
            _rds_actual(public=False, encrypted=False),
            _actual_evidence(rds, DB_ID),
            fail,
        ),
        case(
            "rds-encrypted-actual",
            "expected-transition",
            rules["RDS-ENCRYPT-001"],
            actual,
            DB_ID,
            _rds_actual(public=False, encrypted=True),
            _actual_evidence(rds, DB_ID),
            ok,
            transition_from="rds-unencrypted-actual",
        ),
        # --- S3 block public access (CRITICAL) ---
        case(
            "s3-public-iac",
            "self-agreement",
            rules["S3-PUBLIC-001"],
            iac,
            BUCKET_ID,
            _iac_document(s3_open_tf),
            _iac_evidence(s3_open_tf),
            fail,
        ),
        case(
            "s3-blocked-iac",
            "expected-transition",
            rules["S3-PUBLIC-001"],
            iac,
            BUCKET_ID,
            _iac_document(s3_blocked_tf),
            _iac_evidence(s3_blocked_tf),
            ok,
            transition_from="s3-public-iac",
        ),
        case(
            "s3-public-actual",
            "self-agreement",
            rules["S3-PUBLIC-001"],
            actual,
            BUCKET_ID,
            _s3_actual(blocked=False),
            _actual_evidence(s3, BUCKET_ID),
            fail,
        ),
        case(
            "s3-blocked-actual",
            "expected-transition",
            rules["S3-PUBLIC-001"],
            actual,
            BUCKET_ID,
            _s3_actual(blocked=True),
            _actual_evidence(s3, BUCKET_ID),
            ok,
            transition_from="s3-public-actual",
        ),
        # --- ALB HTTPS only (HIGH) ---
        case(
            "alb-http-iac",
            "self-agreement",
            rules["ALB-HTTPS-001"],
            iac,
            ALB_ARN,
            _iac_document(alb_http_tf),
            _iac_evidence(alb_http_tf),
            fail,
        ),
        case(
            "alb-https-iac",
            "expected-transition",
            rules["ALB-HTTPS-001"],
            iac,
            ALB_ARN,
            _iac_document(alb_https_tf),
            _iac_evidence(alb_https_tf),
            ok,
            transition_from="alb-http-iac",
        ),
        case(
            "alb-http-actual",
            "self-agreement",
            rules["ALB-HTTPS-001"],
            actual,
            ALB_ARN,
            _alb_actual(https=False),
            _actual_evidence(alb, ALB_ARN),
            fail,
        ),
        case(
            "alb-https-actual",
            "expected-transition",
            rules["ALB-HTTPS-001"],
            actual,
            ALB_ARN,
            _alb_actual(https=True),
            _actual_evidence(alb, ALB_ARN),
            ok,
            transition_from="alb-http-actual",
        ),
        # --- EC2 public IP in a private subnet (HIGH) ---
        case(
            "ec2-public-ip-iac",
            "self-agreement",
            rules["EC2-PUBLIC-IP-001"],
            iac,
            INSTANCE_ID,
            _iac_document(ec2_public_tf),
            _iac_evidence(ec2_public_tf),
            fail,
        ),
        case(
            "ec2-private-iac",
            "expected-transition",
            rules["EC2-PUBLIC-IP-001"],
            iac,
            INSTANCE_ID,
            _iac_document(ec2_private_tf),
            _iac_evidence(ec2_private_tf),
            ok,
            transition_from="ec2-public-ip-iac",
        ),
        case(
            "ec2-public-ip-actual",
            "self-agreement",
            rules["EC2-PUBLIC-IP-001"],
            actual,
            INSTANCE_ID,
            _ec2_actual(public=True),
            _actual_evidence(ec2, INSTANCE_ID),
            fail,
        ),
        case(
            "ec2-private-actual",
            "expected-transition",
            rules["EC2-PUBLIC-IP-001"],
            actual,
            INSTANCE_ID,
            _ec2_actual(public=False),
            _actual_evidence(ec2, INSTANCE_ID),
            ok,
            transition_from="ec2-public-ip-actual",
        ),
        # --- 부분 준수: 등급화 여부와 False Negative를 함께 드러낸다 ---
        # Rule이 "모두"(S3 네 플래그) 또는 "만"(ALB HTTPS 전용, RDS 필요한 네트워크만)을
        # 요구하는데 일부만 충족한 상태다. 기대 status는 Rule 문언이 정한다.
        case(
            "s3-two-of-four-actual",
            "partial-compliance",
            rules["S3-PUBLIC-001"],
            actual,
            BUCKET_ID,
            _s3_partial(
                {
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": False,
                    "RestrictPublicBuckets": False,
                }
            ),
            _actual_evidence(s3, BUCKET_ID),
            fail,
        ),
        case(
            "s3-three-of-four-actual",
            "partial-compliance",
            rules["S3-PUBLIC-001"],
            actual,
            BUCKET_ID,
            _s3_partial(
                {
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": False,
                }
            ),
            _actual_evidence(s3, BUCKET_ID),
            fail,
        ),
        case(
            "alb-https-plus-http-actual",
            "partial-compliance",
            rules["ALB-HTTPS-001"],
            actual,
            ALB_ARN,
            _alb_mixed_listeners(),
            _actual_evidence(alb, ALB_ARN),
            fail,
        ),
        case(
            "rds-private-open-sg-actual",
            "partial-compliance",
            rules["RDS-ACCESS-001"],
            actual,
            DB_ID,
            _rds_open_security_group(),
            _actual_evidence(rds, DB_ID),
            fail,
        ),
        # --- Policy phrasing invariance: same control, same document, different wording ---
        case(
            "phrasing-a-rds-public-actual",
            "invariance/policy-phrasing",
            _authored_rule(
                "CUST-RDS_NOT_PUBLIC-a1",
                "RDS 데이터베이스는 외부에서 직접 접근할 수 없어야 한다",
                "FAIL when the DB instance is publicly accessible; PASS when it is reachable "
                "only inside the VPC.",
            ),
            actual,
            DB_ID,
            _rds_actual(public=True, encrypted=True),
            _actual_evidence(rds, DB_ID),
            fail,
            phrasing_pair="phrasing-b-rds-public-actual",
        ),
        case(
            "phrasing-b-rds-public-actual",
            "invariance/policy-phrasing",
            _authored_rule(
                "CUST-RDS_NOT_PUBLIC-b2",
                "운영 데이터베이스에 대한 Public Access는 허용하지 않는다",
                "A managed database that allows public access violates this requirement; one "
                "restricted to private network access satisfies it.",
            ),
            actual,
            DB_ID,
            _rds_actual(public=True, encrypted=True),
            _actual_evidence(rds, DB_ID),
            fail,
            phrasing_pair="phrasing-a-rds-public-actual",
        ),
    ]
    return tuple(cases)


# --------------------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------------------
CUSTOMER_ID = "acme-cloud"


def evaluator_for(
    case: ConsistencyCase, client: object
) -> BedrockStructuredEvaluator | ActualBedrockEvaluator:
    """The evaluator the Worker would use for this perspective.

    AWS_ACTUAL은 `ActualBedrockEvaluator`다: Case 문서를 read-only tool의 응답으로 실어, 근거
    게이트 → 결정적 판정 → 모델 순서가 Worker와 같게 일어난다. 모델 어댑터를 직접 만들면 앞의
    두 단계가 측정에서 빠진다.
    """
    if case.perspective is EvaluationPerspective.IAC:
        return BedrockStructuredEvaluator(
            client=client,  # type: ignore[arg-type]
            perspective=case.perspective,
            resource_document=case.resource_document,
            evidence_references=case.evidence_references,
        )
    document = case.resource_document
    view = AwsResourceView(
        aws_account_id=AWS_ACCOUNT,
        resource_type=str(document["resource_type"]),
        resource_id=str(document["resource_id"]),
        attributes=dict(document["attributes"]),  # type: ignore[arg-type]
    )
    return ActualBedrockEvaluator(
        evidence_loader=ActualEvidenceLoader(
            tool=MockAwsResourceTool(
                customer_id=CUSTOMER_ID, aws_account_id=AWS_ACCOUNT, resources=(view,)
            ),
            customer_id=CUSTOMER_ID,
            aws_account_id=AWS_ACCOUNT,
            resource_type=view.resource_type,
        ),
        client=client,  # type: ignore[arg-type]
    )


def context_for(case: ConsistencyCase) -> PolicyContext:
    return PolicyContext(
        policy_profile_id="profile-consistency-measurement",
        policy_profile_version="v1",
        phase=AssessmentPhase.INITIAL,
        resource_type=case.rule.resource_types[0],
        rules=(case.rule,),
    )


def measure(
    cases: Sequence[ConsistencyCase],
    *,
    client_factory: Callable[[], object],
    profile: ModelProfile,
    repetitions: int,
    keep_rationales: bool = False,
) -> list[CaseReport]:
    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")
    reports: list[CaseReport] = []
    for case in cases:
        report = CaseReport(
            case_id=case.case_id,
            kind=case.kind,
            rule_id=case.rule.rule_id,
            perspective=case.perspective.value,
            expected_status=case.expected_status.value,
        )
        for _ in range(repetitions):
            # 매 실행마다 새 client: 어댑터 내부에 실행 간 상태가 없음을 전제하지 않는다.
            evaluator = evaluator_for(case, client_factory())
            try:
                result = evaluator.evaluate(
                    resource_id=case.resource_id,
                    rule=case.rule,
                    context=context_for(case),
                    model_profile=profile,
                )
            except (BedrockEvaluationError, ValueError, TypeError) as error:
                report.runs.append(
                    RunRecord(
                        status=None,
                        score=None,
                        evidence=(),
                        finding=None,
                        rationale_length=0,
                        contract_error=f"{type(error).__name__}: {error}",
                    )
                )
                continue
            report.runs.append(_record(result, keep_rationales))
        reports.append(report)
    return reports


def _record(result: EvaluationResult, keep_rationales: bool = False) -> RunRecord:
    return RunRecord(
        status=result.status.value,
        score=float(result.score),
        evidence=tuple(sorted(result.evidence_references)),
        finding=finding_from_result(result) is not None,
        rationale_length=len(result.rationale),
        rationale=result.rationale if keep_rationales else None,
        decided_by=result.decided_by.value,
    )


def attribute_order_invariance(case: ConsistencyCase) -> dict[str, object]:
    """Prove, without a model call, that key order cannot change the prompt."""
    permuted = _reverse_keys(case.resource_document)
    # prompt 바이트 검사이므로 모델 어댑터를 직접 만든다 — 게이트·결정적 판정은 prompt를 갖지 않는다.
    original = BedrockStructuredEvaluator(
        client=_NoCallClient(),  # type: ignore[arg-type]
        perspective=case.perspective,
        resource_document=case.resource_document,
        evidence_references=case.evidence_references,
    )._request_body(  # noqa: SLF001 - measurement
        case.resource_id, case.rule, context_for(case), case.evidence_references
    )
    reordered = BedrockStructuredEvaluator(
        client=_NoCallClient(),  # type: ignore[arg-type]
        perspective=case.perspective,
        resource_document=permuted,
        evidence_references=case.evidence_references,
    )._request_body(  # noqa: SLF001 - measurement
        case.resource_id, case.rule, context_for(case), case.evidence_references
    )
    return {
        "case_id": case.case_id,
        "kind": "invariance/attribute-order",
        "prompt_bytes_identical": original == reordered,
    }


def _reverse_keys(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _reverse_keys(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [_reverse_keys(item) for item in value]
    return value


class _NoCallClient:
    def converse(self, **kwargs: object) -> Mapping[str, object]:
        raise AssertionError("attribute-order invariance is checked without a model call")


def transitions(reports: Sequence[CaseReport], cases: Sequence[ConsistencyCase]) -> list[dict]:
    by_id = {report.case_id: report for report in reports}
    rows = []
    for case in cases:
        if case.transition_from is None or case.transition_from not in by_id:
            continue
        before, after = by_id[case.transition_from], by_id[case.case_id]
        before_mean = statistics.fmean(before.scores) if before.scores else None
        after_mean = statistics.fmean(after.scores) if after.scores else None
        rows.append(
            {
                "before": case.transition_from,
                "after": case.case_id,
                "before_mean": None if before_mean is None else round(before_mean, 2),
                "after_mean": None if after_mean is None else round(after_mean, 2),
                "before_status_mode": _mode([r.status for r in before.runs if r.status]),
                "after_status_mode": _mode([r.status for r in after.runs if r.status]),
                "direction_ok": (
                    None if before_mean is None or after_mean is None else after_mean >= before_mean
                ),
            }
        )
    return rows


def phrasing_pairs(reports: Sequence[CaseReport], cases: Sequence[ConsistencyCase]) -> list[dict]:
    by_id = {report.case_id: report for report in reports}
    seen: set[frozenset[str]] = set()
    rows = []
    for case in cases:
        if case.phrasing_pair is None:
            continue
        key = frozenset({case.case_id, case.phrasing_pair})
        if key in seen or case.phrasing_pair not in by_id:
            continue
        seen.add(key)
        a, b = by_id[case.case_id], by_id[case.phrasing_pair]
        a_mean = statistics.fmean(a.scores) if a.scores else None
        b_mean = statistics.fmean(b.scores) if b.scores else None
        rows.append(
            {
                "case_a": a.case_id,
                "case_b": b.case_id,
                "mean_a": None if a_mean is None else round(a_mean, 2),
                "mean_b": None if b_mean is None else round(b_mean, 2),
                "mean_difference": (
                    None if a_mean is None or b_mean is None else round(abs(a_mean - b_mean), 2)
                ),
                "status_mode_a": _mode([r.status for r in a.runs if r.status]),
                "status_mode_b": _mode([r.status for r in b.runs if r.status]),
            }
        )
    return rows


# --------------------------------------------------------------------------------------
# Dry-run model: deterministic, optionally jittered, never used for release evidence.
# --------------------------------------------------------------------------------------
class DryRunClient:
    """Answers from the document alone; `jitter` adds seeded noise to exercise the statistics."""

    def __init__(self, *, jitter: int = 0, seed: int = 0) -> None:
        self._random = random.Random(seed)
        self._jitter = jitter

    def converse(self, **kwargs: object) -> Mapping[str, object]:
        body = json.loads(kwargs["messages"][0]["content"][0]["text"])  # type: ignore[index]
        compliant = _dry_run_judgment(
            body["rule"]["rule_id"], body["perspective"], body["resource_document"]
        )
        allowed: list[str] = body["allowed_evidence_references"]
        if compliant is None:
            status, score = "INSUFFICIENT_EVIDENCE", 0
        else:
            base = 92 if compliant else 12
            status = "PASS" if compliant else "FAIL"
            score = max(0, min(100, base + self._random.randint(-self._jitter, self._jitter)))
        return {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": json.dumps(
                                {
                                    "status": status,
                                    "score": score,
                                    "rationale": "dry run",
                                    "evidence_references": allowed[:2],
                                }
                            )
                        }
                    ]
                }
            }
        }


def _dry_run_judgment(
    rule_id: str, perspective: str, document: Mapping[str, object]
) -> bool | None:
    files = document.get("files")
    text = "\n".join(str(f.get("content", "")) for f in files) if isinstance(files, list) else ""
    attributes = document.get("attributes", {}) if isinstance(document, Mapping) else {}
    key = rule_id.upper()
    if "RDS-PUBLIC" in key or "RDS_NOT_PUBLIC" in key:
        if perspective == "IAC":
            return "publicly_accessible = true" not in text
        return not attributes.get("db_instance", {}).get("PubliclyAccessible", True)
    if "RDS-ENCRYPT" in key:
        return bool(attributes.get("db_instance", {}).get("StorageEncrypted"))
    if "S3-PUBLIC" in key:
        if perspective == "IAC":
            return "= false" not in text
        block = attributes.get("public_access_block", {})
        return bool(block) and all(bool(v) for v in block.values())
    if "ALB-HTTPS" in key:
        if perspective == "IAC":
            return '"HTTP"' not in text
        return all(li.get("Protocol") == "HTTPS" for li in attributes.get("listeners", []))
    if "EC2-PUBLIC-IP" in key:
        if perspective == "IAC":
            return "associate_public_ip_address = true" not in text
        return "PublicIpAddress" not in attributes.get("instance", {})
    return None


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------
def render_markdown(summary: Mapping[str, object]) -> str:
    lines = [
        "# Continuous score consistency measurement",
        "",
        f"- model_profile_id: `{summary['model_profile_id']}`",
        f"- model_id: `{summary['model_id']}` · prompt_version: `{summary['prompt_version']}`"
        f" · rubric_version: `{summary['rubric_version']}`",
        f"- repetitions: {summary['repetitions']} · dry_run: {summary['dry_run']}",
        "",
        "## Self-agreement per case",
        "",
        "| case | rule | perspective | decided by | expected | runs | scores | mean | min | max "
        "| range | stdev | status agreement | expected status accuracy | finding agreement "
        "| max pairwise | contract errors | severe overestimation candidates |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- "
        "| --- | --- | --- |",
    ]
    for row in summary["cases"]:  # type: ignore[union-attr]
        lines.append(
            f"| {row['case_id']} | {row['rule_id']} | {row['perspective']} | {row['decided_by']} "
            f"| {row['expected_status']} "
            f"| {row['runs']} | {', '.join(_fmt(s) for s in row['scores'])} | {row['mean']} | {row['min']} "
            f"| {row['max']} | {row['range']} | {row['stdev']} | {row['status_agreement']} "
            f"| {row['expected_status_accuracy']} | {row['finding_agreement']} | {row['max_pairwise_diff']} "
            f"| {len(row['contract_errors'])} | {', '.join(_fmt(s) for s in row['severe_overestimation_candidates']) or '-'} |"
        )
    lines += [
        "",
        "## Expected transitions (before → after)",
        "",
        "| before | after | before mean | after mean | before status | after status | direction ok |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in summary["transitions"]:  # type: ignore[union-attr]
        lines.append(
            f"| {row['before']} | {row['after']} | {row['before_mean']} | {row['after_mean']} "
            f"| {row['before_status_mode']} | {row['after_status_mode']} | {row['direction_ok']} |"
        )
    lines += [
        "",
        "## Policy phrasing invariance",
        "",
        "| case a | case b | mean a | mean b | |Δ| | status a | status b |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in summary["phrasing_pairs"]:  # type: ignore[union-attr]
        lines.append(
            f"| {row['case_a']} | {row['case_b']} | {row['mean_a']} | {row['mean_b']} "
            f"| {row['mean_difference']} | {row['status_mode_a']} | {row['status_mode_b']} |"
        )
    lines += ["", "## Attribute-order invariance (no model call)", ""]
    for row in summary["attribute_order_invariance"]:  # type: ignore[union-attr]
        lines.append(
            f"- {row['case_id']}: prompt bytes identical = {row['prompt_bytes_identical']}"
        )
    lines += ["", "## By decision source", ""]
    sources: Mapping[str, Mapping[str, object]] = summary["by_decision_source"]  # type: ignore[assignment]
    code, model = sources["CODE"], sources["MODEL"]
    lines.append(
        f"- code-decided: {code['cases']} cases · {code['runs']} runs · expected status accuracy "
        f"{code['expected_status_accuracy']} · below full accuracy: "
        f"{', '.join(map(str, code['cases_below_full_accuracy'])) or '-'}"  # type: ignore[arg-type]
    )
    lines.append(
        f"- model-decided: {model['cases']} cases · {model['runs']} runs · expected status accuracy "
        f"{model['expected_status_accuracy']} · status agreement {model['status_agreement']} "
        f"· below full accuracy: "
        f"{', '.join(map(str, model['cases_below_full_accuracy'])) or '-'}"  # type: ignore[arg-type]
    )
    lines.append(f"- Bedrock calls: {summary['model_calls']}")
    lines += ["", "## Contract checks", ""]
    lines.append(f"- runs with contract errors: {summary['contract_error_runs']}")
    lines.append(
        "- non-judgment statuses carrying a non-zero score: "
        f"{summary['non_judgment_score_contradictions']}"
    )
    lines.append(
        f"- severe overestimation candidates (FAIL with score > {GOLDEN_FAIL_SCORE_MAX}, the Golden "
        f"violation-case ceiling): {summary['severe_overestimation_runs']}"
    )
    return "\n".join(lines) + "\n"


def _fmt(value: object) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def by_decision_source(case_rows: Sequence[Mapping[str, object]]) -> dict[str, dict[str, object]]:
    """Aggregate the per-case rows separately for code-decided and model-decided coordinates.

    코드 판정 좌표에 반복 일치·분산을 매기는 것은 정보가 아니다(항상 1.0·0). 그래서 그 쪽은 기대
    status 정확도만 싣고, 모델 쪽은 정확도와 반복 일치를 둘 다 싣는다. 두 집단의 크기와
    `model_calls`가 "N개 좌표 중 M개는 코드가 판정, Bedrock 호출 K회"라는 문장을 만든다.
    """
    groups: dict[str, dict[str, object]] = {}
    for source in ("CODE", "MODEL"):
        rows = [row for row in case_rows if row["decided_by"] == source]
        accuracies = [
            float(row["expected_status_accuracy"])  # type: ignore[arg-type]
            for row in rows
            if row["expected_status_accuracy"] is not None
        ]
        agreements = [
            float(row["status_agreement"])  # type: ignore[arg-type]
            for row in rows
            if row["status_agreement"] is not None
        ]
        entry: dict[str, object] = {
            "cases": len(rows),
            "runs": sum(int(row["runs"]) for row in rows),  # type: ignore[call-overload]
            "expected_status_accuracy": (
                round(statistics.fmean(accuracies), 4) if accuracies else None
            ),
            "cases_below_full_accuracy": [
                row["case_id"]
                for row in rows
                if row["expected_status_accuracy"] is not None
                and float(row["expected_status_accuracy"]) < 1.0  # type: ignore[arg-type]
            ],
        }
        if source == "MODEL":
            entry["status_agreement"] = (
                round(statistics.fmean(agreements), 4) if agreements else None
            )
            entry["model_calls"] = sum(int(row["model_calls"]) for row in rows)  # type: ignore[call-overload]
        groups[source] = entry
    return groups


def summarize(
    *,
    profile: ModelProfile,
    repetitions: int,
    dry_run: bool,
    cases: Sequence[ConsistencyCase],
    reports: Sequence[CaseReport],
) -> dict[str, object]:
    case_rows = [report.summary() for report in reports]
    return {
        "model_profile_id": profile.model_profile_id,
        "model_id": profile.model_id,
        "prompt_version": profile.prompt_version,
        "rubric_version": profile.rubric_version,
        "inference_config": {"temperature": 0, "maxTokens": 1024},
        "repetitions": repetitions,
        "dry_run": dry_run,
        "cases": case_rows,
        "by_decision_source": by_decision_source(case_rows),
        "model_calls": sum(int(row["model_calls"]) for row in case_rows),  # type: ignore[call-overload]
        "transitions": transitions(reports, cases),
        "phrasing_pairs": phrasing_pairs(reports, cases),
        "attribute_order_invariance": [
            attribute_order_invariance(case) for case in cases if case.kind == "self-agreement"
        ],
        "contract_error_runs": sum(len(row["contract_errors"]) for row in case_rows),
        "non_judgment_score_contradictions": sum(
            len(row["non_judgment_score_contradictions"]) for row in case_rows
        ),
        "severe_overestimation_runs": sum(
            len(row["severe_overestimation_candidates"]) for row in case_rows
        ),
    }


def load_profile(path: Path) -> ModelProfile:
    data = json.loads(path.read_text(encoding="utf-8"))
    profile = ModelProfile(
        model_profile_id=data["model_profile_id"],
        role=ModelProfileRole(data["role"]),
        region=data["region"],
        model_id=data["model_id"],
        prompt_version=data["prompt_version"],
        rubric_version=data["rubric_version"],
        golden_dataset_version=data["golden_dataset_version"],
    )
    if profile.role is not ModelProfileRole.ASSESSMENT:
        raise SystemExit("profile is not an ASSESSMENT profile")
    return profile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument(
        "--profile", type=Path, default=ROOT / "fixtures/m1/assessment_model_profile.json"
    )
    parser.add_argument("--rules", type=Path, default=ROOT / "fixtures/rules")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--jitter", type=int, default=0, help="dry-run only: ± score noise")
    parser.add_argument("--only", nargs="*", default=None, help="case ids to run")
    parser.add_argument(
        "--show-rationales",
        action="store_true",
        help="record model rationales in the JSON (diagnosis of synthetic cases only)",
    )
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--markdown", type=Path, default=None)
    args = parser.parse_args(argv)

    profile = load_profile(args.profile)
    cases = builtin_cases(args.rules)
    if args.only:
        wanted = set(args.only)
        cases = tuple(case for case in cases if case.case_id in wanted)
    if args.dry_run:
        seed = [0]

        def client_factory() -> object:
            seed[0] += 1
            return DryRunClient(jitter=args.jitter, seed=seed[0])
    else:
        import boto3

        client = boto3.client("bedrock-runtime", region_name=profile.region)

        def client_factory() -> object:
            return client

    reports = measure(
        cases,
        client_factory=client_factory,
        profile=profile,
        repetitions=args.repetitions,
        keep_rationales=args.show_rationales,
    )
    summary = summarize(
        profile=profile,
        repetitions=args.repetitions,
        dry_run=args.dry_run,
        cases=cases,
        reports=reports,
    )
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.output is not None:
        args.output.write_text(text + "\n", encoding="utf-8")
    if args.markdown is not None:
        args.markdown.write_text(render_markdown(summary), encoding="utf-8")
    print(render_markdown(summary))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
