"""Constrained Bedrock Converse adapter for structured Assessment evaluation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol

from apps.backend.policy import PolicyContext
from packages.contracts import (
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    ModelProfile,
    ModelProfileRole,
    PolicyRule,
    ScoringMode,
)


class BedrockEvaluationError(ValueError):
    """Raised when a model response is not a safe structured evaluation."""


class BedrockConverseClient(Protocol):
    """Minimal provider boundary; the Lambda runtime supplies the regional client."""

    def converse(self, **kwargs: object) -> Mapping[str, object]: ...


class BedrockStructuredEvaluator:
    """Bind Bedrock output to one approved evidence snapshot and policy rule.

    The model may choose only status, score, rationale, and a subset of supplied
    evidence locators. Resource/rule identity, perspective, severity, versions,
    and model profile are reconstructed from the authoritative inputs.
    """

    def __init__(
        self,
        *,
        client: BedrockConverseClient,
        perspective: EvaluationPerspective,
        resource_document: Mapping[str, object],
        evidence_references: tuple[str, ...],
    ) -> None:
        if client is None:
            raise TypeError("client is required")
        if not isinstance(perspective, EvaluationPerspective):
            raise TypeError("perspective must be an EvaluationPerspective")
        if not isinstance(resource_document, Mapping):
            raise TypeError("resource_document must be a mapping")
        if not evidence_references:
            raise ValueError("evidence_references must not be empty")
        self._client = client
        self._perspective = perspective
        self._resource_document = _json_value(resource_document, "resource_document")
        self._evidence_references = _unique_non_empty_strings(
            evidence_references, "evidence_references"
        )

    def evaluate(
        self,
        *,
        resource_id: str,
        rule: PolicyRule,
        context: PolicyContext,
        model_profile: ModelProfile,
    ) -> EvaluationResult:
        if not isinstance(resource_id, str) or not resource_id.strip():
            raise ValueError("resource_id must be a non-empty string")
        if not isinstance(rule, PolicyRule):
            raise TypeError("rule must be a PolicyRule")
        if not isinstance(context, PolicyContext):
            raise TypeError("context must be a PolicyContext")
        if rule not in context.rules:
            raise BedrockEvaluationError("rule is outside approved policy context")
        if not isinstance(model_profile, ModelProfile):
            raise TypeError("model_profile must be a ModelProfile")
        if model_profile.role is not ModelProfileRole.ASSESSMENT:
            raise BedrockEvaluationError("model profile is not approved for assessment")

        allowed_evidence = _unique_non_empty_strings(
            (
                *self._evidence_references,
                *(reference.evidence_reference for reference in rule.source_references),
            ),
            "allowed evidence reference",
        )
        response = self._client.converse(
            modelId=model_profile.model_id,
            system=[{"text": _SYSTEM_PROMPT}],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"text": self._request_body(resource_id, rule, context, allowed_evidence)}
                    ],
                }
            ],
            inferenceConfig={"temperature": 0, "maxTokens": 1024},
        )
        output = _response_object(response)
        status = _status(output.get("status"))
        score = _score(output.get("score"))
        rationale = _non_empty_string(output.get("rationale"), "rationale")
        evidence = _response_evidence(output.get("evidence_references"), allowed_evidence)
        return EvaluationResult(
            resource_id=resource_id,
            rule_id=rule.rule_id,
            perspective=self._perspective,
            status=status,
            severity=rule.severity.value,
            score=score,
            rationale=rationale,
            evidence_references=evidence,
            rule_version=rule.version,
            rubric_version=model_profile.rubric_version,
            model_profile_id=model_profile.model_profile_id,
            scoring_mode=ScoringMode.CONTINUOUS,
        )

    def _request_body(
        self,
        resource_id: str,
        rule: PolicyRule,
        context: PolicyContext,
        allowed_evidence: tuple[str, ...],
    ) -> str:
        return json.dumps(
            {
                "resource_id": resource_id,
                "perspective": self._perspective.value,
                "resource_document": self._resource_document,
                "policy_profile": {
                    "policy_profile_id": context.policy_profile_id,
                    "version": context.policy_profile_version,
                },
                "rule": {
                    "rule_id": rule.rule_id,
                    "version": rule.version,
                    "title": rule.title,
                    "severity": rule.severity.value,
                    "source_references": [
                        reference.to_dict() for reference in rule.source_references
                    ],
                },
                "allowed_evidence_references": list(allowed_evidence),
            },
            separators=(",", ":"),
            sort_keys=True,
        )


_SYSTEM_PROMPT = (
    "Evaluate exactly the supplied resource against the supplied approved rule. "
    "First decide whether the rule even applies to this resource. A rule applies "
    "only when its subject matches the resource under the given perspective; if the "
    "rule governs a different resource kind, attribute, or concern than what this "
    "resource exposes, it does not apply. "
    "Return one JSON object only, with exactly status, score, rationale, and "
    "evidence_references. status must be exactly one of "
    + ", ".join(status.value for status in EvaluationStatus)
    + ". Use OUT_OF_SCOPE when the rule does not apply to this resource; in that case "
    "the resource is neither compliant nor violating, so score must be 0 and the "
    "rationale must state why the rule does not apply. When the rule applies, use PASS "
    "when the resource satisfies it and FAIL when it violates it; use MANUAL_REVIEW when "
    "a human must decide and INSUFFICIENT_EVIDENCE when the supplied evidence cannot "
    "support a judgment. score must be 0 through 100, and every evidence reference must "
    "come from allowed_evidence_references. Do not wrap the JSON in code fences or add prose."
)


def _strip_json_fence(text: str) -> str:
    """Remove a Markdown code fence the model may wrap around the JSON object.

    Nova models frequently return the structured object inside a ```json ... ``` or
    ``` ... ``` fence despite a JSON-only instruction. Unwrap exactly one leading and
    trailing fence so parsing sees the object; text without a fence is returned as is,
    and any non-JSON content still fails closed in the caller's json.loads.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    # Drop the opening fence line (which may carry a language tag such as ```json).
    lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _response_object(response: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(response, Mapping):
        raise BedrockEvaluationError("Bedrock response is invalid")
    output = response.get("output")
    if not isinstance(output, Mapping):
        raise BedrockEvaluationError("Bedrock response output is missing")
    message = output.get("message")
    if not isinstance(message, Mapping):
        raise BedrockEvaluationError("Bedrock response message is missing")
    content = message.get("content")
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], Mapping):
        raise BedrockEvaluationError("Bedrock response must contain one text block")
    text = content[0].get("text")
    if not isinstance(text, str):
        raise BedrockEvaluationError("Bedrock response text is missing")
    try:
        value = json.loads(_strip_json_fence(text))
    except json.JSONDecodeError as error:
        raise BedrockEvaluationError("Bedrock response is not JSON") from error
    if not isinstance(value, dict):
        raise BedrockEvaluationError("Bedrock response JSON must be an object")
    expected_keys = {"status", "score", "rationale", "evidence_references"}
    if set(value) != expected_keys:
        raise BedrockEvaluationError("Bedrock response fields are invalid")
    return value


def _status(value: object) -> EvaluationStatus:
    if not isinstance(value, str):
        raise BedrockEvaluationError("status is invalid")
    try:
        return EvaluationStatus(value)
    except ValueError as error:
        raise BedrockEvaluationError("status is invalid") from error


def _score(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        raise BedrockEvaluationError("score must be a number from 0 through 100")
    return value


def _response_evidence(value: object, allowed: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise BedrockEvaluationError("evidence_references must be a list")
    evidence = _unique_non_empty_strings(tuple(value), "evidence_references")
    if any(reference not in allowed for reference in evidence):
        raise BedrockEvaluationError("evidence reference is outside approved evidence")
    return evidence


def _unique_non_empty_strings(values: tuple[object, ...], field_name: str) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        item = _non_empty_string(value, field_name)
        if item not in result:
            result.append(item)
    return tuple(result)


def _non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BedrockEvaluationError(f"{field_name} must be a non-empty string")
    return value


def _json_value(value: object, field_name: str) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item, field_name) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item, field_name) for item in value]
    raise TypeError(f"{field_name} must contain JSON-compatible values")
