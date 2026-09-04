"""Constrained Bedrock adapter that proposes Requirement candidates, and nothing else.

이 어댑터가 지키는 것은 하나다: **모델은 제안만 하고 결정은 하지 않는다.** 그래서 응답은
allow-list로만 해석된다.

- 정확한 출력 key 집합. 모르는 key가 하나라도 있으면 거부한다.
- 비-JSON 거부. 자유 텍스트에서 값을 캐내는 파싱을 하지 않는다.
- 금지 필드(`judgment`/`severity`/`score`/`source_score`/`anchor`)가 있으면 응답 전체를 거부한다.
  그 필드를 조용히 버리면, 모델이 판정을 시도했다는 사실 자체가 사라진다.
- locator·Control·resource type·evaluation type·evidence는 전부 넘겨준 allow-list 안이어야 한다.
- field별 길이 상한과 후보 개수 상한.
- application log에 정책 text를 남기지 않는다.

**대용량 문서.** 구조 기반 deterministic chunk로 나눈다. 인접 unit overlap을 두어 문장이 경계에서
잘려 의미를 잃는 경우를 줄이고, 고정 크기로 나눠 같은 문서가 같은 경계를 갖게 한다. 고정 batch
크기는 결과의 **재현성을 높일 뿐 결과 불변을 보장하지 않는다** — 모델은 temperature 0에서도
동일 출력을 보장하지 않는다.

IaC 관련 Catalog hint는 prompt 경계로만 쓴다. 이 단계에서 HCL을 분석하지 않는다.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Protocol

from apps.backend.policy.authoring.artifact_reader import ExtractionUnit
from apps.backend.policy.authoring.extractor import ExtractorIdentity
from packages.contracts import (
    FORBIDDEN_EXTRACTION_FIELDS,
    CandidateClassification,
    ControlAutomationSupport,
    ExtractedRequirement,
    GovernanceControlCatalog,
    ModelProfile,
    ModelProfileRole,
    NormalizedPolicyDocument,
    RuleEvaluationType,
)
from packages.contracts.policy_authoring import (
    MAX_LOCATORS_PER_REQUIREMENT,
    MAX_MAPPING_REASON_LENGTH,
    MAX_REQUIREMENT_LENGTH,
    MAX_REQUIREMENT_SUMMARY_LENGTH,
)

#: 한 chunk가 담는 unit 수와 인접 chunk가 겹치는 unit 수. 구조 기반 고정 크기라 같은 문서는
#: 항상 같은 경계로 나뉜다. 한 chunk의 출력(requirement마다 원문·요약·근거 문자열)이 모델의
#: maxTokens 안에 들어가도록 작게 잡는다. 40이면 한국어 정책 문서에서 응답이 잘려
#: (`Unterminated string`) JSON 파싱이 실패한다.
UNITS_PER_CHUNK = 6
CHUNK_OVERLAP_UNITS = 1

#: 한 청크를 몇 번까지 물어볼 것인가. 완결성 게이트는 청크마다 걸리고 문서는 청크가 하나라도
#: 실패하면 실패하므로, 청크 실패 확률이 조금만 있어도 긴 문서는 거의 확실히 실패한다 —
#: 193 unit 문서는 39 청크가 되고, 청크 성공률 0.95라도 문서 성공률은 0.95^39 ≈ 0.14다.
#: 라이브에서 그 문서는 3/3 실패했다.
#:
#: 재시도는 게이트를 무르게 하지 않는다. 같은 게이트가 그대로 걸리고, 문서는 여전히 모든
#: 청크가 성공해야만 성공하며, 부분 결과는 저장되지 않는다. 바뀌는 것은 실패한 청크를 한 번
#: 더 물어본다는 것뿐이다.
MAX_CHUNK_ATTEMPTS = 3

#: 한 문서 전체가 만들 수 있는 후보 수의 상한. 모델이 문장마다 후보를 만들어도 저장 계층의
#: 상한 안에 머문다.
MAX_REQUIREMENTS_PER_DOCUMENT = 150
MAX_REQUIREMENTS_PER_CHUNK = 40

_RESPONSE_KEYS = frozenset({"requirements", "non_requirement_locators"})
_REQUIRED_FIELDS = frozenset(
    {"source_locators", "requirement", "requirement_summary", "classification", "mapping_reason"}
)
_OPTIONAL_FIELDS = frozenset(
    {
        "mapped_control_key",
        "resource_types",
        "evaluation_type",
        "applicability_semantics",
        "required_evidence",
        "optional_evidence",
        "evaluation_rubric",
        "severity_guidance",
        "exception_semantics",
        "compensating_control_semantics",
    }
)

#: prompt 본문이 바뀌면 이 값도 바뀐다. 배포된 Model Profile의 `prompt_version`이 정확히 같아야
#: 추출기가 생성되므로(생성자에서 fail-closed), 두 값은 같은 변경에서 함께 움직인다.
#:
#: `.2`: AUTOMATABLE에 필요한 다섯 필드를 prompt가 모두 요구하도록 고쳤다. 이전 prompt는 규칙과
#: 예시 어디에서도 `required_evidence`와 `evaluation_rubric`을 말하지 않았는데, Contract는 그 둘이
#: 없는 AUTOMATABLE을 거부한다. 그래서 모델이 낸 AUTOMATABLE은 하나도 남지 못했고 저장된 실행
#: 세 건이 모두 `accepted: 0`이었다 — 업로드한 정책이 자동 평가 Rule을 만들지 못한 직접 원인이다.
PROMPT_VERSION = "policy-authoring/2026-09-04.5"

_SYSTEM_PROMPT = (
    "You extract compliance requirements from policy text and map each to a governance control "
    "catalog. You propose rules; you never evaluate anything.\n"
    "\n"
    "Return ONE JSON object only, no prose and no code fence, with exactly two keys: "
    '"requirements" (a list of requirement objects) and "non_requirement_locators" (a list of '
    "locator strings for headings, context, or other units that state no requirement). Every "
    "locator supplied in policy_units MUST appear either in one or more requirements or in "
    "non_requirement_locators. Never put the same locator in both places and never omit a "
    "locator. Each requirement object MUST "
    "have these fields:\n"
    '- "source_locators": array of one or more locator strings, each copied verbatim from the '
    "supplied policy_units in THIS request (never invent a locator and never cite a locator that "
    "is not in the policy_units list you were given).\n"
    '- "requirement": the full requirement text, in the policy\'s own words.\n'
    '- "requirement_summary": one concise sentence restating the requirement.\n'
    '- "mapping_reason": one sentence explaining the classification and any control mapping.\n'
    '- "classification": exactly one of "AUTOMATABLE", "MANUAL", or "UNSUPPORTED".\n'
    "\n"
    'Keep "requirement" under 200 characters and "requirement_summary" and "mapping_reason" '
    "under 120 characters each. Be concise so the JSON stays small.\n"
    "\n"
    "Classification rules — follow exactly, or the requirement is rejected:\n"
    '- "AUTOMATABLE": the requirement maps to a control whose automation_support is AVAILABLE. '
    "You MUST set all five of these, or the requirement is discarded: "
    '"mapped_control_key" (that control_key); "evaluation_type" (one of "IAC", "AWS", "HYBRID" '
    "that the control lists in supported_evaluation_types); "
    '"resource_types" (only values from the control\'s supported_resource_types); '
    '"required_evidence" (one or more capability_key values taken from that control\'s '
    'evidence_capabilities); and "evaluation_rubric" (one sentence stating what makes a '
    "resource fail or pass this requirement).\n"
    '- "MANUAL": the requirement needs human review. You MUST set "mapped_control_key" to the '
    'catalog control whose automation_support is MANUAL, and set "evaluation_type" to "MANUAL".\n'
    '- "UNSUPPORTED": no catalog control applies. Omit "mapped_control_key" and '
    '"evaluation_type".\n'
    "Use only values the mapped control declares; never invent a control_key, resource_type, or "
    "evidence key. Never output judgment, severity, score, source_score, or anchor.\n"
    "\n"
    "Examples:\n"
    'AUTOMATABLE: {"source_locators":["heading/access-control/item/3"],'
    '"requirement":"S3 buckets must block all public access.",'
    '"requirement_summary":"S3 buckets block public access.",'
    '"mapping_reason":"Maps to the S3 public access block control.",'
    '"classification":"AUTOMATABLE","mapped_control_key":"S3_BLOCK_PUBLIC_ACCESS",'
    '"evaluation_type":"AWS","resource_types":["AWS::S3::Bucket"],'
    '"required_evidence":["S3.PUBLIC_ACCESS_BLOCK"],'
    '"evaluation_rubric":"Fail when any block-public-access setting is disabled."}\n'
    'MANUAL: {"source_locators":["heading/policy/item/1"],'
    '"requirement":"A security officer must approve external AI service adoption.",'
    '"requirement_summary":"Officer approves external AI adoption.",'
    '"mapping_reason":"An organizational control requiring human review.",'
    '"classification":"MANUAL","mapped_control_key":"ORGANIZATIONAL_CONTROL_MANUAL_REVIEW",'
    '"evaluation_type":"MANUAL"}\n'
    "\n"
    'A request may carry "unclassified_locators" or "double_classified_locators": a previous '
    "attempt on these same policy_units left those locators out of both lists, or put them in "
    "both. Fix exactly that in this response — every locator appears in exactly one place — and "
    "classify each on its own merits together with the rest of the policy_units. Do not invent a "
    "requirement to cover a locator that states none: a heading or a context sentence belongs "
    "in non_requirement_locators.\n"
    "\n"
    'UNSUPPORTED: {"source_locators":["heading/misc/item/9"],'
    '"requirement":"Vendor contracts must be retained for five years.",'
    '"requirement_summary":"Retain vendor contracts five years.",'
    '"mapping_reason":"No cloud-resource control evaluates contract retention.",'
    '"classification":"UNSUPPORTED"}'
)


class BedrockExtractionError(ValueError):
    """Raised when a model response is not a safe structured extraction."""


class ChunkAccountingError(BedrockExtractionError):
    """The response's locator accounting is wrong, and the gate knows exactly how.

    누락(어느 목록에도 없음)과 중복(두 목록 모두에 있음) 두 가지다. 둘 다 재시도할 때 **무엇이
    틀렸는지 이름으로** 알려줄 수 있다 — 입력이 실제로 달라지므로 재시도가 의미를 갖는다.
    """

    def __init__(
        self,
        message: str,
        *,
        missing: frozenset[str] = frozenset(),
        overlapping: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(message)
        self.missing = missing
        self.overlapping = overlapping


#: 이전 이름. 누락만 담던 시절의 계약을 쓰는 호출자를 위해 남긴다.
IncompleteChunkError = ChunkAccountingError


class PoisonedResponseError(BedrockExtractionError):
    """The model attempted an evaluation outcome instead of proposing a requirement."""


class BedrockConverseClient(Protocol):
    def converse(self, **kwargs: object) -> Mapping[str, object]: ...


class BedrockPolicyCandidateExtractor:
    """Ask an approved model for Requirement candidates inside the Catalog boundary."""

    def __init__(
        self,
        *,
        client: BedrockConverseClient,
        model_profile: ModelProfile,
        units_per_chunk: int = UNITS_PER_CHUNK,
        chunk_overlap: int = CHUNK_OVERLAP_UNITS,
        max_chunk_attempts: int = MAX_CHUNK_ATTEMPTS,
    ) -> None:
        if client is None:
            raise TypeError("client is required")
        if not isinstance(model_profile, ModelProfile):
            raise TypeError("model_profile must be a ModelProfile")
        if model_profile.role is not ModelProfileRole.POLICY_AUTHORING:
            # Assessment용으로 승인된 모델이 정책 추출에도 쓰이면, 승인 경계가 역할별로
            # 존재하지 않게 된다.
            raise BedrockExtractionError("model profile is not approved for policy authoring")
        if model_profile.prompt_version != PROMPT_VERSION:
            raise BedrockExtractionError(
                "model profile prompt version does not match the deployed extractor"
            )
        if units_per_chunk <= 0 or chunk_overlap < 0 or chunk_overlap >= units_per_chunk:
            raise ValueError("chunk sizing must be positive with a smaller overlap")
        if max_chunk_attempts < 1:
            raise ValueError("max_chunk_attempts must be at least one")
        self._client = client
        self._model_profile = model_profile
        self._units_per_chunk = units_per_chunk
        self._chunk_overlap = chunk_overlap
        self._max_chunk_attempts = max_chunk_attempts

    @property
    def identity(self) -> ExtractorIdentity:
        return ExtractorIdentity(
            extractor_id="bedrock-policy-candidate-extractor",
            extractor_version="1.0.0",
            model_id=self._model_profile.model_id,
            model_version=self._model_profile.model_profile_id,
            prompt_version=self._model_profile.prompt_version,
        )

    def extract(
        self,
        *,
        document: NormalizedPolicyDocument,
        units: tuple[ExtractionUnit, ...],
        catalog: GovernanceControlCatalog,
    ) -> tuple[ExtractedRequirement, ...]:
        if not isinstance(document, NormalizedPolicyDocument):
            raise TypeError("document must be a NormalizedPolicyDocument")
        if not isinstance(catalog, GovernanceControlCatalog):
            raise TypeError("catalog must be a GovernanceControlCatalog")
        if not units:
            raise ValueError("units must not be empty")

        merged: dict[str, ExtractedRequirement] = {}
        chunks = list(_chunks(units, self._units_per_chunk, self._chunk_overlap))
        for chunk in chunks:
            # 한 청크라도 누락되면 문서 전체를 추출했다고 말할 수 없다. 호출자가 재시도할 수
            # 있도록 오류를 그대로 올리고, 부분 결과를 READY로 저장하지 않는다.
            chunk_requirements = self._extract_chunk(chunk, catalog)
            for requirement in chunk_requirements:
                # deterministic merge: 겹치는 unit에서 같은 Requirement가 두 번 나올 수 있다.
                # digest가 같으면 같은 것이므로 먼저 본 것을 유지한다.
                merged.setdefault(requirement.digest, requirement)
                if len(merged) > MAX_REQUIREMENTS_PER_DOCUMENT:
                    raise BedrockExtractionError(
                        "the model proposed more requirements than one document may carry"
                    )
        # canonical order: digest 순. 모델의 출력 순서에 의존하지 않는다.
        return tuple(merged[digest] for digest in sorted(merged))

    def _extract_chunk(
        self, chunk: tuple[ExtractionUnit, ...], catalog: GovernanceControlCatalog
    ) -> tuple[ExtractedRequirement, ...]:
        """Ask for this chunk, re-asking about the locators a response left out.

        완결성 게이트는 그대로다. 달라지는 것은 누락을 처음 한 번의 답으로 확정하지 않는다는
        것뿐이며, 마지막 시도까지 누락이 남으면 예전과 똑같이 실패한다.
        """
        hint: ChunkAccountingError | None = None
        for attempt in range(self._max_chunk_attempts - 1):
            try:
                return self._ask_chunk(chunk, catalog, hint=hint)
            except PoisonedResponseError:
                # 평가 결과를 내놓으려 한 응답은 재시도하지 않는다. 그것은 확률적 실수가 아니라
                # 모델이 경계를 넘으려 한 사실이고, 다시 물어 통과시키면 그 사실이 사라진다.
                raise
            except ChunkAccountingError as error:
                hint = error
                self._log_retry(attempt, error)
            except BedrockExtractionError as error:
                # 응답 자체가 쓸 수 없었다(잘린 JSON 등). 알려줄 내용은 없지만 실패가 생성
                # 쪽에 있으므로 같은 요청이 쓸 만한 응답을 낼 수 있다.
                hint = None
                self._log_retry(attempt, error)
        # 마지막 시도는 예외를 그대로 올린다. 여기서 실패하면 문서 전체가 실패하며, 그것이
        # 이 게이트의 의도다 — 부분 결과를 완전한 추출로 저장하지 않는다.
        return self._ask_chunk(chunk, catalog, hint=hint)

    @staticmethod
    def _log_retry(attempt: int, error: BedrockExtractionError) -> None:
        """Record every discarded attempt. 재시도가 실패를 지우지는 않게 한다."""
        logging.getLogger("governance.authoring").warning(
            "chunk attempt %d discarded: %s: %s", attempt + 1, type(error).__name__, error
        )

    def _ask_chunk(
        self,
        chunk: tuple[ExtractionUnit, ...],
        catalog: GovernanceControlCatalog,
        *,
        hint: ChunkAccountingError | None = None,
    ) -> tuple[ExtractedRequirement, ...]:
        response = self._client.converse(
            modelId=self._model_profile.model_id,
            system=[{"text": _SYSTEM_PROMPT}],
            messages=[
                {
                    "role": "user",
                    "content": [{"text": self._request_body(chunk, catalog, hint)}],
                }
            ],
            inferenceConfig={"temperature": 0, "maxTokens": 8192},
        )
        payload = _response_object(response)
        entries = payload["requirements"]
        if not isinstance(entries, list):
            raise BedrockExtractionError("requirements must be a list")
        if len(entries) > MAX_REQUIREMENTS_PER_CHUNK:
            raise BedrockExtractionError("the model proposed more requirements than one chunk may")
        allowed_locators = frozenset(unit.locator for unit in chunk)
        non_requirement_locators = _non_requirement_locators(
            payload["non_requirement_locators"], allowed_locators
        )
        kept: list[ExtractedRequirement] = []
        for entry in entries:
            # 후보 하나가 잘못돼도 해당 locator의 요구사항이 사라질 수 있으므로 청크 전체를
            # 실패시킨다. 부분 후보를 저장하는 fail-soft 경로는 허용하지 않는다.
            kept.append(_requirement_from_response(entry, allowed_locators, catalog))

        requirement_locators = {
            locator for requirement in kept for locator in requirement.source_locators
        }
        overlap = requirement_locators & non_requirement_locators
        if overlap:
            raise ChunkAccountingError(
                "a locator cannot be both a requirement and a non-requirement",
                overlapping=frozenset(overlap),
            )
        classified = requirement_locators | non_requirement_locators
        if classified != allowed_locators:
            raise ChunkAccountingError(
                "the model did not classify every policy unit",
                missing=frozenset(allowed_locators - classified),
            )
        return tuple(kept)

    def _request_body(
        self,
        chunk: tuple[ExtractionUnit, ...],
        catalog: GovernanceControlCatalog,
        hint: ChunkAccountingError | None = None,
    ) -> str:
        """Build the chunk request; a repair attempt names what the accounting got wrong.

        첫 시도의 본문은 그대로다. 두 힌트는 재시도에만 들어가며, 게이트가 실제로 본 사실을
        그대로 옮긴다 — 어느 locator가 두 목록 어디에도 없었는지, 어느 locator가 양쪽에 모두
        있었는지. 추측이나 유도가 아니라 판정 결과의 인용이다.
        """
        body: dict[str, object] = {
            "policy_units": [
                {"locator": unit.locator, "kind": unit.kind.value, "text": unit.text}
                for unit in chunk
            ],
            "control_catalog": _catalog_prompt_view(catalog),
            "classifications": [value.value for value in CandidateClassification],
        }
        if hint is not None and hint.missing:
            body["unclassified_locators"] = sorted(hint.missing)
        if hint is not None and hint.overlapping:
            body["double_classified_locators"] = sorted(hint.overlapping)
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _catalog_prompt_view(catalog: GovernanceControlCatalog) -> list[dict[str, object]]:
    """The boundary the model may map into — supported controls only.

    `KNOWN_UNSUPPORTED` Control은 prompt에 넣지 않는다. 넣으면 모델이 그것을 자동 평가 가능한
    선택지로 취급하고, 실행 경로가 없는 Rule을 제안한다.
    """
    view: list[dict[str, object]] = []
    for control in catalog.controls:
        if control.automation_support is ControlAutomationSupport.KNOWN_UNSUPPORTED:
            continue
        view.append(
            {
                "control_key": control.control_key,
                "title": control.title,
                "description": control.description,
                "automation_support": control.automation_support.value,
                "supported_resource_types": list(control.supported_resource_types),
                "supported_evaluation_types": [
                    value.value for value in control.supported_evaluation_types
                ],
                "evidence_capabilities": [
                    {
                        "capability_key": binding.capability_key,
                        "perspective": binding.perspective.value,
                        "resource_type": binding.resource_type,
                        # IaC hint는 prompt 경계 설명일 뿐이며 증거가 아니다.
                        "terraform_resource_types": list(binding.terraform_resource_types),
                        "terraform_attribute_names": list(binding.terraform_attribute_names),
                    }
                    for binding in control.available_evidence_capabilities
                ],
            }
        )
    return view


def _chunks(
    units: tuple[ExtractionUnit, ...], size: int, overlap: int
) -> tuple[tuple[ExtractionUnit, ...], ...]:
    """Split by structure into fixed, overlapping windows.

    같은 문서가 항상 같은 경계로 나뉘어야 재추출 결과를 비교할 수 있다. overlap은 요구사항이
    경계에서 잘려 문맥을 잃는 경우를 줄인다.
    """
    if len(units) <= size:
        return (units,)
    step = size - overlap
    windows: list[tuple[ExtractionUnit, ...]] = []
    start = 0
    while start < len(units):
        windows.append(units[start : start + size])
        if start + size >= len(units):
            break
        start += step
    return tuple(windows)


def _response_object(response: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(response, Mapping):
        raise BedrockExtractionError("Bedrock response is invalid")
    output = response.get("output")
    if not isinstance(output, Mapping):
        raise BedrockExtractionError("Bedrock response output is missing")
    message = output.get("message")
    if not isinstance(message, Mapping):
        raise BedrockExtractionError("Bedrock response message is missing")
    content = message.get("content")
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], Mapping):
        raise BedrockExtractionError("Bedrock response must contain one text block")
    text = content[0].get("text")
    if not isinstance(text, str):
        raise BedrockExtractionError("Bedrock response text is missing")
    try:
        value = json.loads(_strip_json_code_fence(text))
    except json.JSONDecodeError as error:
        # 자유 텍스트에서 값을 캐내지 않는다. JSON이 아니면 응답 전체가 신뢰할 수 없다.
        raise BedrockExtractionError("Bedrock response is not JSON") from error
    if not isinstance(value, dict) or set(value) != _RESPONSE_KEYS:
        raise BedrockExtractionError("Bedrock response fields are invalid")
    return value


#: 따옴표로 감싼 구간. Contract의 불변식 메시지는 대부분 규칙 문구지만, 일부는 위반한 값을
#: `!r`로 끼워 넣는다(예: 중복 locator). locator는 고객 문서의 heading에서 파생되므로 정책 문구
#: 조각이 로그로 새어 나갈 수 있다. 규칙 문구만 남기고 값은 지운다.
_QUOTED_SPAN = re.compile("'[^']*'|\"[^\"]*\"", re.DOTALL)


def _redacted(message: str) -> str:
    """Drop quoted spans from a validation message so only its rule text is logged."""
    return _QUOTED_SPAN.sub("'<redacted>'", message)


def _strip_json_code_fence(text: str) -> str:
    """Remove a Markdown code fence that fully wraps the JSON, if present.

    Some models (e.g. Nova) return the JSON inside a ```json ... ``` fence. Only a fence that
    encloses the entire response is removed; any other surrounding prose still makes json.loads
    fail, so this does not mine values out of free text — it only undoes the one formatting the
    model reliably applies to an otherwise-complete JSON object.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    lines = stripped.splitlines()
    # Drop the opening fence line (``` or ```json) and a trailing closing fence line.
    if len(lines) >= 2 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1])
    return text


def _non_requirement_locators(value: object, allowed_locators: frozenset[str]) -> frozenset[str]:
    """Validate the model's explicit accounting for context-only units."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise BedrockExtractionError("non_requirement_locators must be a list")
    locators: list[str] = []
    for locator in value:
        if not isinstance(locator, str) or not locator.strip():
            raise BedrockExtractionError("non_requirement_locators items must be non-empty strings")
        if locator in locators:
            raise BedrockExtractionError("non_requirement_locators must not repeat a locator")
        locators.append(locator)
    if set(locators) - allowed_locators:
        raise BedrockExtractionError("non_requirement_locators cite units outside this chunk")
    return frozenset(locators)


def _requirement_from_response(
    entry: object, allowed_locators: frozenset[str], catalog: GovernanceControlCatalog
) -> ExtractedRequirement:
    if not isinstance(entry, Mapping):
        raise BedrockExtractionError("requirement entry must be an object")
    present = set(entry)
    forbidden = sorted(present & FORBIDDEN_EXTRACTION_FIELDS)
    if forbidden:
        # 조용히 버리지 않는다. 버리면 모델이 판정을 시도했다는 사실 자체가 사라진다. 이건
        # 품질 문제가 아니라 경계 위반이므로 응답(청크) 전체를 거부한다 (fail-soft 대상 아님).
        raise PoisonedResponseError(
            "the model returned an evaluation outcome field: " + ", ".join(forbidden)
        )
    if not _REQUIRED_FIELDS <= present or not present <= (_REQUIRED_FIELDS | _OPTIONAL_FIELDS):
        raise BedrockExtractionError("requirement fields are invalid")

    locators = _string_tuple(entry.get("source_locators"), "source_locators")
    if not locators or len(locators) > MAX_LOCATORS_PER_REQUIREMENT:
        raise BedrockExtractionError("source_locators must cite between one and the unit limit")
    outside = sorted(set(locators) - allowed_locators)
    if outside:
        # 모델이 지어낸 locator는 그 문서에 없다. Evidence가 모델의 주장이 되게 두지 않는다.
        raise BedrockExtractionError("source_locators cite units outside this chunk")

    classification = _enum(entry.get("classification"), CandidateClassification, "classification")
    control_key = _optional_string(entry.get("mapped_control_key"), "mapped_control_key")
    evaluation_type = (
        None
        if entry.get("evaluation_type") is None
        else _enum(entry.get("evaluation_type"), RuleEvaluationType, "evaluation_type")
    )

    resource_types = _string_tuple(entry.get("resource_types"), "resource_types")
    required_evidence = _string_tuple(entry.get("required_evidence"), "required_evidence")
    optional_evidence = _string_tuple(entry.get("optional_evidence"), "optional_evidence")
    # Catalog 경계는 여기서 판정하지 않는다. `build_candidate`가 이미 같은 검사를 하고 위반마다
    # 코드를 붙여 후보 **하나**를 거절한다(`UNKNOWN_CONTROL_KEY`, `UNSUPPORTED_RESOURCE_TYPE`,
    # `UNSUPPORTED_EVALUATION_TYPE`, `EVIDENCE_CAPABILITY_NOT_AVAILABLE`).
    #
    # 여기서 같은 것을 예외로 올리면 그 판정이 청크 전체를 죽인다. 라이브 측정에서 그 결과가
    # 드러났다 — 193 unit 문서의 39 청크 중 13개가 오직 이 이유로 실패했고, 그 청크에 함께 들어
    # 있던 멀쩡한 요구사항까지 사라졌다. 카탈로그에 없는 통제를 지목한 요구사항은 "평가할 수 없는
    # 요구사항"이지 "믿을 수 없는 응답"이 아니다.
    #
    # 경계 자체는 그대로다. 그런 후보는 거절되어 승인 가능한 Rule이 되지 못하며, 이제는 사라지는
    # 대신 사유 코드와 함께 보존된다.

    try:
        return ExtractedRequirement(
            source_locators=locators,
            requirement=_bounded(entry.get("requirement"), "requirement", MAX_REQUIREMENT_LENGTH),
            requirement_summary=_bounded(
                entry.get("requirement_summary"),
                "requirement_summary",
                MAX_REQUIREMENT_SUMMARY_LENGTH,
            ),
            classification=classification,
            mapping_reason=_bounded(
                entry.get("mapping_reason"), "mapping_reason", MAX_MAPPING_REASON_LENGTH
            ),
            mapped_control_key=control_key,
            resource_types=resource_types,
            evaluation_type=evaluation_type,
            applicability_semantics=_optional_string(
                entry.get("applicability_semantics"), "applicability_semantics"
            ),
            required_evidence=required_evidence,
            optional_evidence=optional_evidence,
            evaluation_rubric=_optional_string(entry.get("evaluation_rubric"), "evaluation_rubric"),
            severity_guidance=_optional_string(entry.get("severity_guidance"), "severity_guidance"),
            exception_semantics=_optional_string(
                entry.get("exception_semantics"), "exception_semantics"
            ),
            compensating_control_semantics=_optional_string(
                entry.get("compensating_control_semantics"), "compensating_control_semantics"
            ),
        )
    except BedrockExtractionError:
        # 이미 구체적인 사유를 가진 거부다. 일반 메시지로 덮으면 무엇이 규칙을 어겼는지 사라진다.
        raise
    except (TypeError, ValueError) as error:
        # Contract의 분류 불변식을 만족하지 못하는 응답도 여기서 거부한다. 메시지에 정책 문장을
        # 넣지 않기 위해 원인 텍스트는 그대로 전달하지 않는다. 불변식 위반의 사유(필드명·규칙
        # 문구)는 진단을 위해 로그에 남기되, 값을 끼워 넣는 메시지가 섞여 있으므로 인용 구간은
        # 지우고 남긴다.
        logging.getLogger("governance.authoring").warning(
            "requirement rejected: %s: %s", type(error).__name__, _redacted(str(error))
        )
        raise BedrockExtractionError("the model returned an invalid requirement shape") from error


def _require_subset(values: tuple[str, ...], allowed: tuple[str, ...], field_name: str) -> None:
    outside = sorted(set(values) - set(allowed))
    if outside:
        raise BedrockExtractionError(f"{field_name} is outside the control catalog boundary")


def _enum[T](value: object, enum_type: type[T], field_name: str) -> T:
    if not isinstance(value, str):
        raise BedrockExtractionError(f"{field_name} is invalid")
    try:
        return enum_type(value)  # type: ignore[call-arg]
    except ValueError as error:
        raise BedrockExtractionError(f"{field_name} is invalid") from error


def _bounded(value: object, field_name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BedrockExtractionError(f"{field_name} must be a non-empty string")
    if len(value) > limit:
        raise BedrockExtractionError(f"{field_name} is longer than the allowed limit")
    return value


def _optional_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise BedrockExtractionError(f"{field_name} must be a non-empty string when present")
    return value


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise BedrockExtractionError(f"{field_name} must be a list")
    result: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise BedrockExtractionError(f"{field_name} items must be non-empty strings")
        if entry not in result:
            result.append(entry)
    return tuple(result)
