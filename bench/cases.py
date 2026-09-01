"""Synthetic, non-customer benchmark cases for the four agent roles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    """One deterministic prompt and its machine-checkable expectation."""

    case_id: str
    system_prompt: str
    user_prompt: str
    expected: dict[str, Any]
    max_tokens: int


PARENT_CASES: tuple[BenchmarkCase, ...] = (
    BenchmarkCase(
        case_id="parent-policy-question",
        system_prompt=(
            "You are the Parent router of a cloud-governance workflow. Return JSON only with "
            "next_agent (POLICY_QA, ASSESSMENT, or REMEDIATION_DEPLOYMENT), "
            "async_job (boolean), and reason (string). Route only to the immediate next agent. "
            "Policy questions are synchronous POLICY_QA."
        ),
        user_prompt="우리 회사의 S3 public access 차단 규칙과 근거를 알려줘.",
        expected={"next_agent": "POLICY_QA", "async_job": False},
        max_tokens=160,
    ),
    BenchmarkCase(
        case_id="parent-assessment-request",
        system_prompt=(
            "You are the Parent router of a cloud-governance workflow. Return JSON only with "
            "next_agent (POLICY_QA, ASSESSMENT, or REMEDIATION_DEPLOYMENT), "
            "async_job (boolean), and reason (string). Route only to the immediate next agent. "
            "Assessment and remediation are asynchronous."
        ),
        user_prompt="현재 Terraform과 AWS 상태를 평가해서 S3 공개 설정 위반 Finding을 찾아줘.",
        expected={"next_agent": "ASSESSMENT", "async_job": True},
        max_tokens=160,
    ),
    BenchmarkCase(
        case_id="parent-remediation-request",
        system_prompt=(
            "You are the Parent router of a cloud-governance workflow. Return JSON only with "
            "next_agent (POLICY_QA, ASSESSMENT, or REMEDIATION_DEPLOYMENT), "
            "async_job (boolean), and reason (string). Route only to the immediate next agent. "
            "Assessment and remediation are asynchronous."
        ),
        user_prompt=(
            "확정된 finding-s3-public-001을 수정하는 Terraform patch를 만들고 "
            "PR과 plan 준비를 진행해줘."
        ),
        expected={"next_agent": "REMEDIATION_DEPLOYMENT", "async_job": True},
        max_tokens=160,
    ),
)

POLICY_QA_CASES: tuple[BenchmarkCase, ...] = (
    BenchmarkCase(
        case_id="policy-s3-public-rule",
        system_prompt=(
            "You are POLICY_QA. Use only this approved policy context: rule_id=S3-PUBLIC-001; "
            "rule_version=2026-08-01; title=S3 buckets must block public access; "
            "severity=HIGH; source=isms-p-2023#control/5.2.1. Return JSON only with "
            "required (boolean), rule_id, rule_version, and evidence_references. "
            "required must be true because the approved rule requires blocking public access. "
            "Do not invent other rules."
        ),
        user_prompt="S3 버킷의 public access를 차단해야 하나요? 적용 규칙과 근거를 알려줘.",
        expected={
            "required": True,
            "rule_id": "S3-PUBLIC-001",
            "rule_version": "2026-08-01",
            "evidence_references": {"isms-p-2023#control/5.2.1"},
        },
        max_tokens=240,
    ),
)

ASSESSMENT_CASES: tuple[BenchmarkCase, ...] = (
    BenchmarkCase(
        case_id="golden-s3-public-001",
        system_prompt=(
            "You are ASSESSMENT. Evaluate exactly one resource against exactly one approved rule. "
            "Return JSON only with resource_id, rule_id, status, severity, score, rationale, "
            "evidence_references, rule_version, rubric_version, and scoring_mode. "
            "status must be PASS, FAIL, MANUAL_REVIEW, INSUFFICIENT_EVIDENCE, OUT_OF_SCOPE, "
            "or EXECUTION_ERROR. score is 0-100."
        ),
        user_prompt=(
            "Phase=INITIAL; rubric_version=mvp-v1; scoring_mode=CONTINUOUS. "
            "Resource: resource_id=arn:aws:s3:::benchmark-public-bucket; "
            "type=AWS::S3::Bucket; public access block has block_public_acls=false, "
            "block_public_policy=false, ignore_public_acls=false, restrict_public_buckets=false. "
            "Approved rule: rule_id=S3-PUBLIC-001; rule_version=2026-08-01; severity=HIGH; "
            "require all S3 public-access controls to be enabled. Required evidence references: "
            "aws:s3:public-access-block and isms-p-2023#control/5.2.1."
        ),
        expected={
            "resource_id": "arn:aws:s3:::benchmark-public-bucket",
            "rule_id": "S3-PUBLIC-001",
            "perspective": "IAC",
            "status": "FAIL",
            "severity": "HIGH",
            "score_min": 0,
            "score_max": 30,
            "evidence_references": {
                "aws:s3:public-access-block",
                "isms-p-2023#control/5.2.1",
            },
            "rule_version": "2026-08-01",
            "rubric_version": "mvp-v1",
            "model_profile_id": "benchmark-assessment-profile-v1",
            "scoring_mode": "CONTINUOUS",
        },
        max_tokens=400,
    ),
)

TERRAFORM_BASE_CONTENT = """resource "aws_s3_bucket_public_access_block" "example" {
  bucket                  = aws_s3_bucket.example.id
  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}
"""
TERRAFORM_REMEDIATED_CONTENT = TERRAFORM_BASE_CONTENT.replace("= false", "= true")


REMEDIATION_DEPLOYMENT_CASES: tuple[BenchmarkCase, ...] = (
    BenchmarkCase(
        case_id="remediation-s3-public-minimal-diff",
        system_prompt=(
            "You are REMEDIATION_DEPLOYMENT. Produce a minimal Terraform remediation only; do not "
            "apply infrastructure changes. Return JSON only with finding_id, base_commit_sha, "
            "changed_paths, patch, deployment_id, commit_sha, plan_hash, approval, "
            "requires_human_approval, and apply_mechanism. approval must contain deployment_id, "
            "approved_by, commit_sha, and plan_hash, exactly bound to the proposed plan. "
            "apply_mechanism must be GITHUB_ACTIONS_OIDC_ONLY. The patch must be a unified diff."
        ),
        user_prompt=(
            "Finding finding-s3-public-001: S3 public access block is disabled. The only allowed "
            "repository-relative file is modules/s3/main.tf. Produce the minimum patch for this "
            "synthetic Terraform block:\n"
            'resource "aws_s3_bucket_public_access_block" "example" {\n'
            "  bucket                  = aws_s3_bucket.example.id\n"
            "  block_public_acls       = false\n"
            "  block_public_policy     = false\n"
            "  ignore_public_acls      = false\n"
            "  restrict_public_buckets = false\n"
            "}\n"
            "No new resources, KMS keys, aliases, modules, or unrelated changes are allowed. "
            "Use base_commit_sha=commit-before-remediation, deployment_id=deployment-001, "
            "commit_sha=commit-remediation-001, plan_hash=plan-sha256-001, and "
            "approval approved_by=user-approver-001 with the same deployment, commit, and plan."
        ),
        expected={
            "finding_id": "finding-s3-public-001",
            "base_commit_sha": "commit-before-remediation",
            "changed_paths": {"modules/s3/main.tf"},
            "deployment_id": "deployment-001",
            "commit_sha": "commit-remediation-001",
            "plan_hash": "plan-sha256-001",
            "approved_by": "user-approver-001",
            "apply_mechanism": "GITHUB_ACTIONS_OIDC_ONLY",
            "base_content": TERRAFORM_BASE_CONTENT,
            "remediated_content": TERRAFORM_REMEDIATED_CONTENT,
        },
        max_tokens=700,
    ),
)

CASES_BY_ROLE: dict[str, tuple[BenchmarkCase, ...]] = {
    "parent": PARENT_CASES,
    "policy_qa": POLICY_QA_CASES,
    "assessment": ASSESSMENT_CASES,
    "remediation_deployment": REMEDIATION_DEPLOYMENT_CASES,
}
