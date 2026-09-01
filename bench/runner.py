"""Bedrock Converse execution, response validation, and sanitized result reporting."""

from __future__ import annotations

import hashlib
import json
import re
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bench.cases import BenchmarkCase
from bench.config import ROLE_NAMES, ModelCandidate
from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    DeploymentApproval,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    RemediationPatch,
    ScoringMode,
    TerraformPlan,
)


@dataclass(frozen=True, slots=True)
class InvocationResult:
    role: str
    case_id: str
    model_label: str
    model_id: str
    run_number: int
    valid: bool
    checks: dict[str, bool]
    latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    stop_reason: str | None
    output_sha256: str | None
    decision_sha256: str | None
    assessment_status: str | None
    assessment_score: float | None
    error_kind: str | None


def discover_text_models(region: str) -> tuple[ModelCandidate, ...]:
    """Return active, on-demand Text-to-Text foundation models visible to the account."""
    import boto3

    client = boto3.client("bedrock", region_name=region)
    response = client.list_foundation_models(byOutputModality="TEXT")
    candidates: list[ModelCandidate] = []
    for summary in response.get("modelSummaries", []):
        lifecycle = summary.get("modelLifecycle", {})
        if lifecycle.get("status") != "ACTIVE":
            continue
        if "TEXT" not in summary.get("inputModalities", []):
            continue
        if "ON_DEMAND" not in summary.get("inferenceTypesSupported", []):
            continue
        model_id = summary.get("modelId")
        if not isinstance(model_id, str) or not model_id:
            continue
        provider = summary.get("providerName", "Unknown")
        model_name = summary.get("modelName", model_id)
        candidates.append(ModelCandidate(f"{provider} {model_name}", model_id))
    return tuple(sorted(candidates, key=lambda candidate: candidate.model_id))


class BedrockConverseClient:
    """Small adapter that relies on the standard boto3 credential provider chain."""

    def __init__(self, region: str) -> None:
        import boto3

        self._client = boto3.client("bedrock-runtime", region_name=region)

    def invoke(self, case: BenchmarkCase, model_id: str) -> tuple[str, dict[str, Any], int]:
        started = time.perf_counter()
        response = self._client.converse(
            modelId=model_id,
            system=[{"text": case.system_prompt}],
            messages=[{"role": "user", "content": [{"text": case.user_prompt}]}],
            inferenceConfig={"maxTokens": case.max_tokens, "temperature": 0},
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        blocks = response.get("output", {}).get("message", {}).get("content", [])
        text = "".join(block["text"] for block in blocks if "text" in block)
        return text, response, latency_ms


def execute_case(
    client: BedrockConverseClient,
    role: str,
    case: BenchmarkCase,
    candidate: ModelCandidate,
    run_number: int,
) -> InvocationResult:
    """Call one model and retain only hashed output plus validation metadata."""
    try:
        raw_output, response, latency_ms = client.invoke(case, candidate.model_id)
    except Exception as error:  # Provider errors are measured, never re-raised mid-run.
        return InvocationResult(
            role=role,
            case_id=case.case_id,
            model_label=candidate.label,
            model_id=candidate.model_id,
            run_number=run_number,
            valid=False,
            checks={"invocation_succeeded": False},
            latency_ms=None,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            stop_reason=None,
            output_sha256=None,
            decision_sha256=None,
            assessment_status=None,
            assessment_score=None,
            error_kind=error_kind(error),
        )

    usage = response.get("usage", {})
    input_tokens = as_optional_int(usage.get("inputTokens"))
    output_tokens = as_optional_int(usage.get("outputTokens"))
    total_tokens = as_optional_int(usage.get("totalTokens"))
    stop_reason = as_optional_string(response.get("stopReason"))
    output_sha256 = hashlib.sha256(raw_output.encode("utf-8")).hexdigest()

    try:
        parsed = parse_json_output(raw_output)
        checks = validate_response(role, parsed, case.expected)
        decision = decision_payload(role, parsed)
        assessment_status = parsed.get("status") if role == "assessment" else None
        assessment_score = parsed.get("score") if role == "assessment" else None
        return InvocationResult(
            role=role,
            case_id=case.case_id,
            model_label=candidate.label,
            model_id=candidate.model_id,
            run_number=run_number,
            valid=all(checks.values()),
            checks=checks,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            stop_reason=stop_reason,
            output_sha256=output_sha256,
            decision_sha256=hashlib.sha256(
                json.dumps(decision, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest(),
            assessment_status=assessment_status if isinstance(assessment_status, str) else None,
            assessment_score=as_optional_number(assessment_score),
            error_kind=None,
        )
    except Exception as error:  # Parse and validation failures retain invocation metadata.
        return InvocationResult(
            role=role,
            case_id=case.case_id,
            model_label=candidate.label,
            model_id=candidate.model_id,
            run_number=run_number,
            valid=False,
            checks={"invocation_succeeded": True, "response_processed": False},
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            stop_reason=stop_reason,
            output_sha256=output_sha256,
            decision_sha256=None,
            assessment_status=None,
            assessment_score=None,
            error_kind=error_kind(error),
        )


def parse_json_output(raw_output: str) -> dict[str, Any]:
    """Accept a JSON object, with an optional plain or JSON Markdown fence."""
    stripped = raw_output.strip()
    lines = stripped.splitlines()
    if lines and lines[0] in {"```", "```json"} and lines[-1] == "```":
        stripped = "\n".join(lines[1:-1]).strip()
    parsed = json.loads(stripped)
    if not isinstance(parsed, dict):
        raise ValueError("model output must be a JSON object")
    return parsed


def decision_payload(role: str, response: dict[str, Any]) -> dict[str, Any]:
    """Return the non-sensitive semantic decision used for repeat-agreement checks."""
    if role == "parent":
        return {
            "next_agent": response.get("next_agent"),
            "async_job": response.get("async_job"),
        }
    if role == "policy_qa":
        return {
            "required": response.get("required"),
            "rule_id": response.get("rule_id"),
            "rule_version": response.get("rule_version"),
            "evidence_references": sorted_string_list(response.get("evidence_references")),
        }
    if role == "assessment":
        return {
            "resource_id": response.get("resource_id"),
            "rule_id": response.get("rule_id"),
            "status": response.get("status"),
            "severity": response.get("severity"),
            "evidence_references": sorted_string_list(response.get("evidence_references")),
        }
    if role == "remediation_deployment":
        return {
            "finding_id": response.get("finding_id"),
            "base_commit_sha": response.get("base_commit_sha"),
            "changed_paths": sorted_string_list(response.get("changed_paths")),
            "patch": response.get("patch"),
            "deployment_id": response.get("deployment_id"),
            "commit_sha": response.get("commit_sha"),
            "plan_hash": response.get("plan_hash"),
            "approval": response.get("approval"),
            "requires_human_approval": response.get("requires_human_approval"),
            "apply_mechanism": response.get("apply_mechanism"),
        }
    raise ValueError(f"unsupported role: {role}")


def sorted_string_list(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return []
    return sorted(value)


def validate_response(
    role: str, response: dict[str, Any], expected: dict[str, Any]
) -> dict[str, bool]:
    """Evaluate only role-specific safety and correctness requirements."""
    if role == "parent":
        return {
            "next_agent": response.get("next_agent") == expected["next_agent"],
            "async_job": response.get("async_job") is expected["async_job"],
            "reason": isinstance(response.get("reason"), str) and bool(response["reason"].strip()),
        }
    if role == "policy_qa":
        references = response.get("evidence_references")
        return {
            "required": response.get("required") is expected["required"],
            "rule_id": response.get("rule_id") == expected["rule_id"],
            "rule_version": response.get("rule_version") == expected["rule_version"],
            "evidence_references": isinstance(references, list)
            and set(references) == expected["evidence_references"],
        }
    if role == "assessment":
        return validate_assessment(response, expected)
    if role == "remediation_deployment":
        return validate_remediation(response, expected)
    raise ValueError(f"unsupported role: {role}")


def validate_assessment(response: dict[str, Any], expected: dict[str, Any]) -> dict[str, bool]:
    """Validate the contract shape and the repository Golden-case expectations."""
    try:
        evaluation = EvaluationResult(
            resource_id=response["resource_id"],
            rule_id=response["rule_id"],
            perspective=EvaluationPerspective(expected["perspective"]),
            status=EvaluationStatus(response["status"]),
            severity=response["severity"],
            score=response["score"],
            rationale=response["rationale"],
            evidence_references=tuple(response["evidence_references"]),
            rule_version=response["rule_version"],
            rubric_version=response["rubric_version"],
            model_profile_id=expected["model_profile_id"],
            scoring_mode=ScoringMode(response["scoring_mode"]),
        )
    except (KeyError, TypeError, ValueError):
        return {"evaluation_contract": False}

    return {
        "evaluation_contract": True,
        "resource_id": evaluation.resource_id == expected["resource_id"],
        "rule_id": evaluation.rule_id == expected["rule_id"],
        "perspective": evaluation.perspective.value == expected["perspective"],
        "status": evaluation.status.value == expected["status"],
        "severity": evaluation.severity == expected["severity"],
        "score_range": expected["score_min"] <= evaluation.score <= expected["score_max"],
        "evidence_references": expected["evidence_references"].issubset(
            set(evaluation.evidence_references)
        ),
        "rule_version": evaluation.rule_version == expected["rule_version"],
        "rubric_version": evaluation.rubric_version == expected["rubric_version"],
        "model_profile_id": evaluation.model_profile_id == expected["model_profile_id"],
        "scoring_mode": evaluation.scoring_mode.value == expected["scoring_mode"],
    }


def validate_remediation(response: dict[str, Any], expected: dict[str, Any]) -> dict[str, bool]:
    """Require an exact, applicable diff and an approval bound to the proposed plan."""
    patch = response.get("patch")
    changed_paths = response.get("changed_paths")
    expected_path = next(iter(expected["changed_paths"]))
    expected_added = {
        "block_public_acls       = true",
        "block_public_policy     = true",
        "ignore_public_acls      = true",
        "restrict_public_buckets = true",
    }
    expected_removed = {
        "block_public_acls       = false",
        "block_public_policy     = false",
        "ignore_public_acls      = false",
        "restrict_public_buckets = false",
    }
    if not isinstance(patch, str):
        minimal_diff = False
        diff_path = False
        patch_applies = False
    else:
        lines = patch.splitlines()
        added = [
            line[1:].strip()
            for line in lines
            if line.startswith("+") and not line.startswith("+++")
        ]
        removed = [
            line[1:].strip()
            for line in lines
            if line.startswith("-") and not line.startswith("---")
        ]
        old_headers = [normalize_diff_path(line[4:]) for line in lines if line.startswith("--- ")]
        new_headers = [normalize_diff_path(line[4:]) for line in lines if line.startswith("+++ ")]
        diff_path = old_headers == [expected_path] and new_headers == [expected_path]
        minimal_diff = (
            len(added) == len(expected_added)
            and set(added) == expected_added
            and len(removed) == len(expected_removed)
            and set(removed) == expected_removed
            and any(line.startswith("@@") for line in lines)
        )
        patch_applies = (
            apply_unified_diff(patch, expected["base_content"]) == expected["remediated_content"]
        )

    contracts_valid = False
    approval_matches = False
    approval = response.get("approval")
    try:
        patch_reference = ArtifactReference(
            artifact_id="benchmark-patch",
            artifact_type=ArtifactType.REMEDIATION_PATCH,
            content_sha256="benchmark-patch-sha256",
            customer_id="benchmark-customer",
            repository_id="benchmark-repository",
        )
        RemediationPatch(
            finding_id=response["finding_id"],
            base_commit_sha=response["base_commit_sha"],
            artifact=patch_reference,
            changed_paths=tuple(response["changed_paths"]),
        )
        plan_reference = ArtifactReference(
            artifact_id="benchmark-plan",
            artifact_type=ArtifactType.TERRAFORM_PLAN,
            content_sha256=response["plan_hash"],
            customer_id="benchmark-customer",
            repository_id="benchmark-repository",
        )
        plan = TerraformPlan(
            deployment_id=response["deployment_id"],
            commit_sha=response["commit_sha"],
            plan_hash=response["plan_hash"],
            artifact=plan_reference,
        )
        parsed_approval = DeploymentApproval(
            deployment_id=approval["deployment_id"],
            approved_by=approval["approved_by"],
            commit_sha=approval["commit_sha"],
            plan_hash=approval["plan_hash"],
        )
        contracts_valid = True
        approval_matches = parsed_approval.matches(plan)
    except (KeyError, TypeError, ValueError):
        pass

    changed_paths_valid = (
        isinstance(changed_paths, list)
        and len(changed_paths) == len(expected["changed_paths"])
        and set(changed_paths) == expected["changed_paths"]
    )
    expected_approval = {
        "deployment_id": expected["deployment_id"],
        "approved_by": expected["approved_by"],
        "commit_sha": expected["commit_sha"],
        "plan_hash": expected["plan_hash"],
    }
    return {
        "finding_id": response.get("finding_id") == expected["finding_id"],
        "base_commit_sha": response.get("base_commit_sha") == expected["base_commit_sha"],
        "changed_paths": changed_paths_valid,
        "diff_path": diff_path,
        "minimal_diff": minimal_diff,
        "patch_applies": patch_applies,
        "remediation_and_plan_contracts": contracts_valid,
        "deployment_id": response.get("deployment_id") == expected["deployment_id"],
        "commit_sha": response.get("commit_sha") == expected["commit_sha"],
        "plan_hash": response.get("plan_hash") == expected["plan_hash"],
        "approval_binding": approval_matches and approval == expected_approval,
        "human_approval": response.get("requires_human_approval") is True,
        "apply_mechanism": response.get("apply_mechanism") == expected["apply_mechanism"],
    }


def normalize_diff_path(value: str) -> str:
    """Normalize a unified-diff path while retaining repository-relative identity."""
    path = value.split("\t", 1)[0].strip()
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def apply_unified_diff(patch: str, base_content: str) -> str | None:
    """Apply one strict unified-diff hunk to synthetic base content."""
    lines = patch.splitlines()
    hunk_indexes = [index for index, line in enumerate(lines) if line.startswith("@@")]
    if len(hunk_indexes) != 1:
        return None
    hunk_index = hunk_indexes[0]
    match = re.fullmatch(
        r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?",
        lines[hunk_index],
    )
    if match is None:
        return None

    old_start = int(match.group(1))
    old_count = int(match.group(2) or 1)
    new_start = int(match.group(3))
    new_count = int(match.group(4) or 1)
    if old_start < 1 or new_start != old_start:
        return None

    base_lines = base_content.splitlines()
    cursor = old_start - 1
    if cursor > len(base_lines):
        return None
    result = list(base_lines[:cursor])
    old_seen = 0
    new_seen = 0
    for line in lines[hunk_index + 1 :]:
        if line == "\\ No newline at end of file":
            continue
        if not line or line[0] not in {" ", "+", "-"}:
            return None
        value = line[1:]
        if line[0] in {" ", "-"}:
            if cursor >= len(base_lines) or base_lines[cursor] != value:
                return None
            cursor += 1
            old_seen += 1
        if line[0] in {" ", "+"}:
            result.append(value)
            new_seen += 1
    if old_seen != old_count or new_seen != new_count:
        return None
    result.extend(base_lines[cursor:])
    suffix = "\n" if base_content.endswith("\n") else ""
    return "\n".join(result) + suffix


def write_reports(results: list[InvocationResult], output_dir: Path) -> tuple[Path, Path]:
    """Write only sanitized JSON and Markdown aggregates; never raw prompts or responses."""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    summary = summarize(results)
    json_path = output_dir / f"bedrock-model-evaluation-{timestamp}.json"
    markdown_path = output_dir / f"bedrock-model-evaluation-{timestamp}.md"
    json_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "invocations": [asdict(result) for result in results],
                "summary": summary,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(summary), encoding="utf-8")
    return json_path, markdown_path


def summarize(results: list[InvocationResult]) -> dict[str, Any]:
    """Apply quality gates before ranking eligible candidates by latency and token usage."""
    grouped: dict[tuple[str, str, str], list[InvocationResult]] = defaultdict(list)
    for result in results:
        grouped[(result.role, result.model_label, result.model_id)].append(result)

    by_role: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (role, label, model_id), entries in grouped.items():
        valid_count = sum(entry.valid for entry in entries)
        valid_rate = valid_count / len(entries)
        latency_values = [entry.latency_ms for entry in entries if entry.latency_ms is not None]
        token_values = [entry.total_tokens for entry in entries if entry.total_tokens is not None]
        entries_by_case: dict[str, list[InvocationResult]] = defaultdict(list)
        for entry in entries:
            entries_by_case[entry.case_id].append(entry)

        agreement_values = [
            decision_agreement(case_entries) for case_entries in entries_by_case.values()
        ]
        min_decision_agreement = min(agreement_values, default=0.0)
        score_spreads = [
            score_spread(case_entries)
            for case_entries in entries_by_case.values()
            if any(entry.assessment_score is not None for entry in case_entries)
        ]
        max_score_spread = max(score_spreads, default=None)
        quality_gate = (
            valid_rate >= 0.9
            and min_decision_agreement >= 0.9
            and (role != "assessment" or (max_score_spread is not None and max_score_spread <= 10))
        )
        by_role[role].append(
            {
                "label": label,
                "model_id": model_id,
                "attempts": len(entries),
                "valid_runs": valid_count,
                "valid_rate": valid_rate,
                "min_decision_agreement": min_decision_agreement,
                "max_score_spread": max_score_spread,
                "quality_gate": quality_gate,
                "median_latency_ms": int(statistics.median(latency_values))
                if latency_values
                else None,
                "median_total_tokens": int(statistics.median(token_values))
                if token_values
                else None,
                "errors": sorted({entry.error_kind for entry in entries if entry.error_kind}),
            }
        )

    role_summaries: dict[str, dict[str, Any]] = {}
    for role, candidates in by_role.items():
        candidates.sort(
            key=lambda item: (
                not item["quality_gate"],
                -item["valid_rate"],
                -item["min_decision_agreement"],
                item["max_score_spread"] is None,
                item["max_score_spread"] or 0,
                item["median_latency_ms"] is None,
                item["median_latency_ms"] or 0,
                item["median_total_tokens"] is None,
                item["median_total_tokens"] or 0,
            )
        )
        winner = candidates[0] if candidates and candidates[0]["quality_gate"] else None
        role_summaries[role] = {"candidates": candidates, "winner": winner}
    return {"roles": role_summaries}


def decision_agreement(entries: list[InvocationResult]) -> float:
    """Return majority semantic-decision agreement among valid outputs for one Case."""
    decisions = [
        entry.decision_sha256 for entry in entries if entry.valid and entry.decision_sha256
    ]
    if not decisions:
        return 0.0
    return Counter(decisions).most_common(1)[0][1] / len(decisions)


def score_spread(entries: list[InvocationResult]) -> float:
    """Return max-minus-min score for one repeated ASSESSMENT Case."""
    scores = [
        entry.assessment_score
        for entry in entries
        if entry.valid and entry.assessment_score is not None
    ]
    if not scores:
        return float("inf")
    return max(scores) - min(scores)


def render_markdown(summary: dict[str, Any]) -> str:
    """Render selection rationale solely from this run's sanitized measurements."""
    lines = [
        "# Bedrock 모델 실측 평가",
        "",
        (
            "외부 벤치마크·단가가 아닌 이 실행의 유효성, 유효 출력 내 최소 Case 결정 "
            "일치율, Assessment score 편차, 지연시간, 토큰 사용량만 사용했습니다. "
            "결정 일치율은 유효 출력만 분모로 계산하며, invalid 실행은 유효율에 별도로 "
            "반영했습니다."
        ),
        "",
    ]
    for role, role_summary in summary["roles"].items():
        lines.extend(
            [
                f"## {ROLE_NAMES[role]}",
                "",
                (
                    "| 후보 모델 | 품질 Gate | 유효 실행 | 유효 출력 내 최소 Case 결정 "
                    "일치율 | Score 범위 | 중앙 지연 | 중앙 토큰 | 오류 |"
                ),
                "|---|---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        for candidate in role_summary["candidates"]:
            latency = candidate["median_latency_ms"]
            tokens = candidate["median_total_tokens"]
            spread = candidate["max_score_spread"]
            errors = ", ".join(candidate["errors"]) or "-"
            lines.append(
                f"| {candidate['label']} (`{candidate['model_id']}`) | "
                f"{'PASS' if candidate['quality_gate'] else 'FAIL'} | "
                f"{candidate['valid_runs']}/{candidate['attempts']} "
                f"({candidate['valid_rate']:.0%}) | "
                f"{candidate['min_decision_agreement']:.0%} | "
                f"{spread if spread is not None else '-'} | "
                f"{latency if latency is not None else '-'} ms | "
                f"{tokens if tokens is not None else '-'} | {errors} |"
            )
        winner = role_summary["winner"]
        if winner is None:
            lines.extend(["", "**선정 보류:** 품질 Gate를 통과한 후보가 없습니다.", ""])
        else:
            lines.extend(
                [
                    "",
                    f"**선정:** {winner['label']} (`{winner['model_id']}`)",
                    "",
                    (
                        "선정 이유: 품질 Gate 통과 후보를 유효율, 유효 출력 내 최소 Case "
                        "결정 일치율"
                        + (", score 편차" if role == "assessment" else "")
                        + ", 중앙 지연, 중앙 토큰 순으로 정렬했습니다."
                    ),
                    "",
                ]
            )
    return "\n".join(lines)


def as_optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def as_optional_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def as_optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def error_kind(error: Exception) -> str:
    """Keep only a stable error class/code; provider messages can contain sensitive context."""
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        if isinstance(code, str):
            return f"{type(error).__name__}:{code}"
    return type(error).__name__
