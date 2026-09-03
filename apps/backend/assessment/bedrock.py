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
                "rule": _rule_prompt_view(rule),
                "allowed_evidence_references": list(allowed_evidence),
            },
            separators=(",", ":"),
            sort_keys=True,
        )


_SYSTEM_PROMPT = (
    "Evaluate exactly the supplied resource against the supplied approved rule. "
    "When the rule carries an evaluation_rubric, that rubric is the criterion; the title "
    "alone is not. Judge only from the supplied resource_document: if it does not carry "
    "the evidence the rule requires, return status INSUFFICIENT_EVIDENCE instead of "
    "inferring the missing state. "
    "Return one JSON object only, with exactly status, score, rationale, and "
    "evidence_references. status must be one of PASS, FAIL, MANUAL_REVIEW, "
    "INSUFFICIENT_EVIDENCE, or OUT_OF_SCOPE; score must be 0 through 100; and every "
    "evidence reference must come from allowed_evidence_references."
)

#: 모델이 돌려줄 수 없는 status. `EXECUTION_ERROR`는 "평가가 실행되지 못했다"는 Code의 사실이지
#: 판정이 아니다. 모델이 그 값을 쓰면 Coverage 분모에 남아 재시도 대상처럼 보이고, 실제로는
#: 모델이 판정을 회피한 것과 구별되지 않는다.
_MODEL_FORBIDDEN_STATUSES = frozenset({EvaluationStatus.EXECUTION_ERROR})


def _rule_prompt_view(rule: PolicyRule) -> dict[str, object]:
    """The Rule as the model sees it: identity, severity, sources, and execution semantics.

    authoring이 만든 Rule은 사람이 검토·승인한 `evaluation_rubric`·`applicability_semantics`·
    evidence capability를 갖는다. 그것을 빼고 title만 보내면 승인된 rubric이 판정에 아무 영향을
    주지 않는다 — 정책 → Rule → Assessment 연결이 형식에 그친다. legacy Rule
    (`evaluation_type is None`)은 그 필드가 없으므로 이전과 같은 view가 나온다.
    """
    view: dict[str, object] = {
        "rule_id": rule.rule_id,
        "version": rule.version,
        "title": rule.title,
        "severity": rule.severity.value,
        "source_references": [reference.to_dict() for reference in rule.source_references],
    }
    if rule.evaluation_type is not None:
        view["evaluation_type"] = rule.evaluation_type.value
    for name in (
        "applicability_semantics",
        "evaluation_rubric",
        "exception_semantics",
        "compensating_control_semantics",
    ):
        value = getattr(rule, name)
        if value is not None:
            view[name] = value
    for name in ("required_evidence", "optional_evidence"):
        value = getattr(rule, name)
        if value:
            view[name] = list(value)
    return view


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
        value = json.loads(text)
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
        status = EvaluationStatus(value)
    except ValueError as error:
        raise BedrockEvaluationError("status is invalid") from error
    if status in _MODEL_FORBIDDEN_STATUSES:
        raise BedrockEvaluationError("status is reserved for the runtime, not the model")
    return status


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
