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
        score = _normalized_score(status, _score(output.get("score")))
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
    "First decide whether the rule even applies to this resource. A rule applies "
    "only when its subject matches the resource under the given perspective; if the "
    "rule governs a different resource kind, attribute, or concern than what this "
    "resource exposes, it does not apply. "
    "When the rule carries an evaluation_rubric, that rubric is the criterion; the title "
    "alone is not. Judge only from the supplied resource_document: if it does not carry "
    "the evidence the rule requires, return status INSUFFICIENT_EVIDENCE instead of "
    "inferring the missing state. "
    "Return one JSON object only, with exactly status, score, rationale, and "
    "evidence_references. status must be exactly one of PASS, FAIL, MANUAL_REVIEW, "
    "INSUFFICIENT_EVIDENCE, or OUT_OF_SCOPE. "
    "Use OUT_OF_SCOPE when the rule does not apply to this resource; in that case "
    "the resource is neither compliant nor violating, so score must be 0 and the "
    "rationale must state why the rule does not apply. When the rule applies, use PASS "
    "when the resource satisfies it and FAIL when it violates it; use MANUAL_REVIEW when "
    "a human must decide and INSUFFICIENT_EVIDENCE when the supplied evidence cannot "
    "support a judgment. score must be 0 through 100 and grades how completely the "
    "resource satisfies the rule's criterion: reserve 0 and 100 for a resource that "
    "wholly violates or wholly satisfies it, and place a partially satisfied resource "
    "between the extremes. Every evidence reference must come from "
    "allowed_evidence_references. Do not wrap the JSON in code fences or add prose."
)

#: **이 문단은 측정으로 정해졌다.** 처음에는 판정 아닌 status 셋(MANUAL_REVIEW,
#: INSUFFICIENT_EVIDENCE, OUT_OF_SCOPE)을 prompt에서 다시 열거하고 각각 score 0을 지시했다.
#: 라이브 A/B(RDS-ACCESS-001, 퍼블릭 아님 + 3306을 0.0.0.0/0에 개방, n=8)에서 그 문구는 정확도를
#: 떨어뜨렸다 — 기존 문구 5/8 FAIL, 새 문구 0/8 FAIL(전부 OUT_OF_SCOPE 회피). status를 점수와
#: 함께 나열하자 모델이 "판정하지 않음" 선택지를 더 자주 골랐다. 점수 고정은 prose가 아니라
#: `_normalized_score()`가 하므로 prompt에서 그 열거를 지웠고, 같은 Case가 5/5 FAIL로 돌아왔다.
#: 남긴 것은 등급화 지시 한 문장뿐이다(그 문장은 정확도를 바꾸지 않았다).

#: 판정이 아닌 status의 점수. 모델이 이 status와 함께 어떤 숫자를 내더라도 Runtime이 이 값으로
#: 고정한다. 근거가 없거나 사람이 정해야 하는 좌표에 모델의 숫자를 남기면 (1) 같은 입력이
#: 실행마다 다른 점수를 갖고, (2) readiness 평균에 판정 아닌 숫자가 섞이며, (3) Code가 만드는
#: `INSUFFICIENT_EVIDENCE`(`actual_evaluator.INSUFFICIENT_EVIDENCE_SCORE`)와 `MANUAL_REVIEW`
#: (`manual_review.MANUAL_REVIEW_SCORE`)의 0.0과 같은 status가 다른 값을 갖게 된다. 새로운 anchor가
#: 아니라 이미 존재하는 Code 쪽 규약을 모델 응답에도 같게 적용하는 것이다.
NON_JUDGMENT_SCORE = 0.0
_NON_JUDGMENT_STATUSES = frozenset(
    {
        EvaluationStatus.MANUAL_REVIEW,
        EvaluationStatus.INSUFFICIENT_EVIDENCE,
        EvaluationStatus.OUT_OF_SCOPE,
    }
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
        status = EvaluationStatus(value)
    except ValueError as error:
        raise BedrockEvaluationError("status is invalid") from error
    if status in _MODEL_FORBIDDEN_STATUSES:
        raise BedrockEvaluationError("status is reserved for the runtime, not the model")
    return status


def _score(value: object) -> float:
    """Accept only a finite number in [0, 100].

    `0 <= nan <= 100`은 False이므로 NaN은 여기서 걸리고, ±inf도 마찬가지다. 문자열 "80"과 bool은
    숫자가 아니다 — JSON이 숫자를 문자열로 감싸 보낸 응답을 관대하게 받으면 같은 모델 출력이
    파서 버전에 따라 다른 결과가 된다.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        raise BedrockEvaluationError("score must be a number from 0 through 100")
    return value


def _normalized_score(status: EvaluationStatus, score: float) -> float:
    """Pin the score of a non-judgment status to the runtime's fixed value."""
    if status in _NON_JUDGMENT_STATUSES:
        return NON_JUDGMENT_SCORE
    return score


def _response_evidence(value: object, allowed: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise BedrockEvaluationError("evidence_references must be a list")
    evidence = _unique_non_empty_strings(tuple(value), "evidence_references")
    outside = [reference for reference in evidence if not _is_allowed(reference, allowed)]
    if outside:
        raise BedrockEvaluationError(
            "evidence reference is outside approved evidence: " + ", ".join(outside)
        )
    return evidence


def _is_allowed(reference: str, allowed: tuple[str, ...]) -> bool:
    """An approved locator, or a resource anchor inside an approved resource locator.

    Golden Case가 기대하는 IaC evidence는 `terraform:{path}#{resource address}` 형태다
    (`fixtures/m1/golden_dataset_cases.json`). 허용 목록에는 파일 단위 `terraform:{path}`만 있으므로
    모델이 그 파일 안의 리소스 주소를 `#`로 붙여 인용하면 이전에는 통째로 거부돼 평가가 실패했다
    (라이브 5회 반복 측정에서 IAC PASS Case 19건 전부). anchor는 허용된 파일/리소스 **안**을 더 좁게
    가리키는 것이지 새 근거가 아니다. 정책 locator(`{source}@{version}#{locator}`)는 이미 `#`를
    포함하므로 정확히 일치해야 하고, 여기서 다시 쪼개지 않는다.
    """
    if reference in allowed:
        return True
    base, separator, anchor = reference.partition("#")
    if not separator or not anchor.strip():
        return False
    return base in allowed and base.startswith(("terraform:", "aws:", "s3://"))


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
