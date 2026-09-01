"""Unit tests for Bedrock benchmark parsing, validation, and aggregation."""

import json
import unittest
from copy import deepcopy
from typing import Any

from bench.cases import (
    ASSESSMENT_CASES,
    REMEDIATION_DEPLOYMENT_CASES,
    TERRAFORM_BASE_CONTENT,
    TERRAFORM_REMEDIATED_CONTENT,
)
from bench.runner import (
    InvocationResult,
    apply_unified_diff,
    decision_agreement,
    parse_json_output,
    render_markdown,
    summarize,
    validate_assessment,
    validate_remediation,
)

VALID_PATCH = """--- a/modules/s3/main.tf
+++ b/modules/s3/main.tf
@@ -1,7 +1,7 @@
 resource "aws_s3_bucket_public_access_block" "example" {
   bucket                  = aws_s3_bucket.example.id
-  block_public_acls       = false
+  block_public_acls       = true
-  block_public_policy     = false
+  block_public_policy     = true
-  ignore_public_acls      = false
+  ignore_public_acls      = true
-  restrict_public_buckets = false
+  restrict_public_buckets = true
 }
"""


def assessment_response() -> dict[str, Any]:
    expected = ASSESSMENT_CASES[0].expected
    return {
        "resource_id": expected["resource_id"],
        "rule_id": expected["rule_id"],
        "status": expected["status"],
        "severity": expected["severity"],
        "score": 20,
        "rationale": "Public access block is disabled",
        "evidence_references": sorted(expected["evidence_references"]),
        "rule_version": expected["rule_version"],
        "rubric_version": expected["rubric_version"],
        "scoring_mode": expected["scoring_mode"],
    }


def remediation_response() -> dict[str, Any]:
    expected = REMEDIATION_DEPLOYMENT_CASES[0].expected
    return {
        "finding_id": expected["finding_id"],
        "base_commit_sha": expected["base_commit_sha"],
        "changed_paths": sorted(expected["changed_paths"]),
        "patch": VALID_PATCH,
        "deployment_id": expected["deployment_id"],
        "commit_sha": expected["commit_sha"],
        "plan_hash": expected["plan_hash"],
        "approval": {
            "deployment_id": expected["deployment_id"],
            "approved_by": expected["approved_by"],
            "commit_sha": expected["commit_sha"],
            "plan_hash": expected["plan_hash"],
        },
        "requires_human_approval": True,
        "apply_mechanism": expected["apply_mechanism"],
    }


def invocation(
    *,
    role: str = "parent",
    case_id: str = "case-1",
    model_label: str = "Candidate",
    model_id: str = "model-1",
    run_number: int = 1,
    valid: bool = True,
    decision: str | None = "decision-a",
    score: float | None = None,
    latency_ms: int | None = 100,
    total_tokens: int | None = 50,
    error: str | None = None,
) -> InvocationResult:
    return InvocationResult(
        role=role,
        case_id=case_id,
        model_label=model_label,
        model_id=model_id,
        run_number=run_number,
        valid=valid,
        checks={"ok": valid},
        latency_ms=latency_ms,
        input_tokens=20,
        output_tokens=30,
        total_tokens=total_tokens,
        stop_reason="end_turn",
        output_sha256="output-hash",
        decision_sha256=decision,
        assessment_status="FAIL" if role == "assessment" else None,
        assessment_score=score,
        error_kind=error,
    )


class ParseJsonOutputTest(unittest.TestCase):
    def test_accepts_plain_and_fenced_json_objects(self) -> None:
        expected = {"answer": True}
        samples = (
            '{"answer": true}',
            '```\n{"answer": true}\n```',
            '```json\n{"answer": true}\n```',
        )

        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(parse_json_output(sample), expected)

    def test_empty_or_lone_fences_raise_json_decode_error(self) -> None:
        for sample in ("```", "```\n```", "```json\n```"):
            with self.subTest(sample=sample):
                with self.assertRaises(json.JSONDecodeError):
                    parse_json_output(sample)

    def test_malformed_json_raises_json_decode_error(self) -> None:
        with self.assertRaises(json.JSONDecodeError):
            parse_json_output('{"answer":')

    def test_non_object_json_raises_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "JSON object"):
            parse_json_output("[1, 2, 3]")


class ApplyUnifiedDiffTest(unittest.TestCase):
    def test_applies_valid_single_hunk_patch(self) -> None:
        self.assertEqual(
            apply_unified_diff(VALID_PATCH, TERRAFORM_BASE_CONTENT),
            TERRAFORM_REMEDIATED_CONTENT,
        )

    def test_rejects_malformed_or_multiple_hunks(self) -> None:
        malformed = VALID_PATCH.replace("@@ -1,7 +1,7 @@", "@@ malformed @@")
        multiple = VALID_PATCH + "@@ -1 +1 @@\n unchanged\n"

        self.assertIsNone(apply_unified_diff(malformed, TERRAFORM_BASE_CONTENT))
        self.assertIsNone(apply_unified_diff(multiple, TERRAFORM_BASE_CONTENT))

    def test_rejects_wrong_counts_and_context(self) -> None:
        wrong_count = VALID_PATCH.replace("@@ -1,7 +1,7 @@", "@@ -1,6 +1,7 @@")
        wrong_context = VALID_PATCH.replace(
            ' resource "aws_s3_bucket_public_access_block" "example" {',
            ' resource "aws_s3_bucket_public_access_block" "other" {',
        )

        self.assertIsNone(apply_unified_diff(wrong_count, TERRAFORM_BASE_CONTENT))
        self.assertIsNone(apply_unified_diff(wrong_context, TERRAFORM_BASE_CONTENT))


class ValidateAssessmentTest(unittest.TestCase):
    def test_accepts_contract_and_expected_semantics(self) -> None:
        checks = validate_assessment(
            assessment_response(),
            ASSESSMENT_CASES[0].expected,
        )

        self.assertTrue(all(checks.values()), checks)

    def test_contract_failure_fails_closed(self) -> None:
        response = assessment_response()
        del response["rationale"]

        self.assertEqual(
            validate_assessment(response, ASSESSMENT_CASES[0].expected),
            {"evaluation_contract": False},
        )

    def test_reports_semantic_mismatches(self) -> None:
        response = assessment_response()
        response["resource_id"] = "arn:aws:s3:::different-bucket"
        response["score"] = 31
        response["evidence_references"] = []

        checks = validate_assessment(response, ASSESSMENT_CASES[0].expected)

        self.assertTrue(checks["evaluation_contract"])
        self.assertFalse(checks["resource_id"])
        self.assertFalse(checks["score_range"])
        self.assertFalse(checks["evidence_references"])

    def test_uses_authoritative_expected_metadata(self) -> None:
        response = assessment_response()
        response["perspective"] = "AWS_ACTUAL"
        response["model_profile_id"] = "model-supplied-profile"

        checks = validate_assessment(response, ASSESSMENT_CASES[0].expected)

        self.assertTrue(checks["perspective"])
        self.assertTrue(checks["model_profile_id"])
        self.assertTrue(all(checks.values()), checks)


class ValidateRemediationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.expected = REMEDIATION_DEPLOYMENT_CASES[0].expected

    def test_accepts_exact_applicable_approved_remediation(self) -> None:
        checks = validate_remediation(remediation_response(), self.expected)

        self.assertTrue(all(checks.values()), checks)

    def test_rejects_changed_path_and_diff_path_mismatches(self) -> None:
        response = remediation_response()
        response["changed_paths"] = ["modules/other/main.tf"]
        response["patch"] = VALID_PATCH.replace(
            "modules/s3/main.tf",
            "modules/other/main.tf",
        )

        checks = validate_remediation(response, self.expected)

        self.assertFalse(checks["changed_paths"])
        self.assertFalse(checks["diff_path"])

    def test_rejects_non_minimal_patch_even_when_it_applies(self) -> None:
        response = remediation_response()
        response["patch"] = VALID_PATCH.replace(
            "   bucket                  = aws_s3_bucket.example.id\n",
            "-  bucket                  = aws_s3_bucket.example.id\n"
            "+  bucket                  = aws_s3_bucket.example.id\n",
        )

        checks = validate_remediation(response, self.expected)

        self.assertFalse(checks["minimal_diff"])
        self.assertTrue(checks["patch_applies"])

    def test_rejects_patch_that_does_not_apply_even_when_minimal(self) -> None:
        response = remediation_response()
        response["patch"] = VALID_PATCH.replace(
            ' resource "aws_s3_bucket_public_access_block" "example" {',
            ' resource "aws_s3_bucket_public_access_block" "other" {',
        )

        checks = validate_remediation(response, self.expected)

        self.assertTrue(checks["minimal_diff"])
        self.assertFalse(checks["patch_applies"])

    def test_rejects_contract_failure(self) -> None:
        response = remediation_response()
        response["changed_paths"] = None

        checks = validate_remediation(response, self.expected)

        self.assertFalse(checks["remediation_and_plan_contracts"])

    def test_rejects_unbound_approval(self) -> None:
        response = remediation_response()
        approval = deepcopy(response["approval"])
        approval["plan_hash"] = "different-plan"
        response["approval"] = approval

        checks = validate_remediation(response, self.expected)

        self.assertTrue(checks["remediation_and_plan_contracts"])
        self.assertFalse(checks["approval_binding"])

    def test_requires_human_approval_and_oidc_apply_mechanism(self) -> None:
        response = remediation_response()
        response["requires_human_approval"] = False
        response["apply_mechanism"] = "DIRECT_AWS_APPLY"

        checks = validate_remediation(response, self.expected)

        self.assertFalse(checks["human_approval"])
        self.assertFalse(checks["apply_mechanism"])


class DecisionAgreementTest(unittest.TestCase):
    def test_returns_zero_for_empty_or_all_invalid_entries(self) -> None:
        self.assertEqual(decision_agreement([]), 0.0)
        self.assertEqual(
            decision_agreement([invocation(valid=False, decision="decision-a") for _ in range(3)]),
            0.0,
        )

    def test_returns_majority_share(self) -> None:
        entries = [invocation(decision="decision-a") for _ in range(3)]
        entries.extend(invocation(decision="decision-b") for _ in range(2))

        self.assertEqual(decision_agreement(entries), 0.6)

    def test_uses_only_valid_outputs_as_denominator(self) -> None:
        entries = [invocation(decision="decision-a") for _ in range(2)]
        entries.extend(invocation(valid=False, decision="decision-b") for _ in range(8))

        self.assertEqual(decision_agreement(entries), 1.0)


class SummarizeTest(unittest.TestCase):
    def test_gates_valid_rate_and_valid_output_agreement_separately(self) -> None:
        valid_rate_pass = [
            invocation(
                model_label="Valid Rate Pass",
                model_id="valid-rate-pass",
                run_number=index,
            )
            for index in range(1, 10)
        ]
        valid_rate_pass.append(
            invocation(
                model_label="Valid Rate Pass",
                model_id="valid-rate-pass",
                run_number=10,
                valid=False,
                decision="different-invalid-decision",
                error="JSONDecodeError",
            )
        )
        valid_rate_fail = [
            invocation(
                model_label="Valid Rate Fail",
                model_id="valid-rate-fail",
                run_number=index,
                valid=index <= 8,
                decision="same-valid-decision" if index <= 8 else "invalid-decision",
            )
            for index in range(1, 11)
        ]
        agreement_fail = [
            invocation(
                model_label="Agreement Fail",
                model_id="agreement-fail",
                run_number=index,
                decision="majority" if index <= 8 else "minority",
            )
            for index in range(1, 11)
        ]

        candidates = summarize(valid_rate_pass + valid_rate_fail + agreement_fail)["roles"][
            "parent"
        ]["candidates"]
        by_id = {candidate["model_id"]: candidate for candidate in candidates}

        self.assertEqual(by_id["valid-rate-pass"]["valid_rate"], 0.9)
        self.assertEqual(by_id["valid-rate-pass"]["min_decision_agreement"], 1.0)
        self.assertTrue(by_id["valid-rate-pass"]["quality_gate"])
        self.assertEqual(by_id["valid-rate-fail"]["valid_rate"], 0.8)
        self.assertEqual(by_id["valid-rate-fail"]["min_decision_agreement"], 1.0)
        self.assertFalse(by_id["valid-rate-fail"]["quality_gate"])
        self.assertEqual(by_id["agreement-fail"]["valid_rate"], 1.0)
        self.assertEqual(by_id["agreement-fail"]["min_decision_agreement"], 0.8)
        self.assertFalse(by_id["agreement-fail"]["quality_gate"])

    def test_assessment_spread_gate_accepts_10_and_rejects_11_or_none(self) -> None:
        results = []
        for model_id, scores in (
            ("spread-10", (10.0, 20.0)),
            ("spread-11", (10.0, 21.0)),
            ("spread-none", (None, None)),
        ):
            results.extend(
                invocation(
                    role="assessment",
                    model_label=model_id,
                    model_id=model_id,
                    run_number=index,
                    score=score,
                )
                for index, score in enumerate(scores, start=1)
            )

        candidates = summarize(results)["roles"]["assessment"]["candidates"]
        by_id = {candidate["model_id"]: candidate for candidate in candidates}

        self.assertEqual(by_id["spread-10"]["max_score_spread"], 10.0)
        self.assertTrue(by_id["spread-10"]["quality_gate"])
        self.assertEqual(by_id["spread-11"]["max_score_spread"], 11.0)
        self.assertFalse(by_id["spread-11"]["quality_gate"])
        self.assertIsNone(by_id["spread-none"]["max_score_spread"])
        self.assertFalse(by_id["spread-none"]["quality_gate"])

    def test_prioritizes_pass_then_latency_then_tokens(self) -> None:
        results = [
            invocation(
                model_label="Fail Fast",
                model_id="fail-fast",
                valid=False,
                decision=None,
                latency_ms=1,
                total_tokens=1,
            ),
            invocation(
                model_label="Slow",
                model_id="slow",
                latency_ms=200,
                total_tokens=1,
            ),
            invocation(
                model_label="Fast High Tokens",
                model_id="fast-high-tokens",
                latency_ms=100,
                total_tokens=100,
            ),
            invocation(
                model_label="Fast Low Tokens",
                model_id="fast-low-tokens",
                latency_ms=100,
                total_tokens=50,
            ),
        ]

        role_summary = summarize(results)["roles"]["parent"]

        self.assertEqual(role_summary["winner"]["model_id"], "fast-low-tokens")
        self.assertEqual(
            [candidate["model_id"] for candidate in role_summary["candidates"]],
            ["fast-low-tokens", "fast-high-tokens", "slow", "fail-fast"],
        )


class RenderMarkdownTest(unittest.TestCase):
    def test_explains_valid_only_agreement_and_separate_invalid_rate(self) -> None:
        results = [invocation(run_number=index) for index in range(1, 10)]
        results.append(
            invocation(
                run_number=10,
                valid=False,
                decision=None,
                error="JSONDecodeError",
            )
        )

        markdown = render_markdown(summarize(results))

        self.assertIn("유효 출력 내 최소 Case 결정 일치율", markdown)
        self.assertIn("결정 일치율은 유효 출력만 분모로 계산", markdown)
        self.assertIn("invalid 실행은 유효율에 별도로 반영", markdown)
        self.assertIn("9/10 (90%)", markdown)
        self.assertIn("**선정:** Candidate (`model-1`)", markdown)
        self.assertIn(
            "유효율, 유효 출력 내 최소 Case 결정 일치율, 중앙 지연",
            markdown,
        )


if __name__ == "__main__":
    unittest.main()
