"""Deterministic policy excerpts for the Parent's Policy Q&A (ADR-0012).

The Parent used to see one thing: the user's sentence. Asked "list our policies" it had no list;
asked about "our S3 policy" it recited a textbook. This module is the missing half — *code*
chooses which of the customer's own normalized policy documents, and which units of them, reach
the model, under a fixed budget, keyed by the caller's ``customer_id``. The model may then only
phrase an answer from that material and must cite the unit locators it used.

Two boundaries are deliberate:

- Tenant. Every read here is keyed by the customer the JWT proved. There is no way to name a
  partition the caller does not own, so the material can never be another customer's.
- Policy text. It leaves this module in exactly one direction — into the Bedrock prompt, the same
  direction the authoring extractor already sends it — and comes back only as the model's answer
  to that customer's own user. It is not logged, not stored, and the context object has no
  ``to_dict`` for the same reason ``ExtractionUnit`` has none.

Retrieval is lexical on purpose. There is no vector store in this platform, the documents are
short (hundreds of units), and a deterministic ranking is auditable: the same question against
the same documents always yields the same excerpts.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from apps.backend.policy.authoring import ExtractionUnit
from packages.contracts import DocumentUnitKind, IngestionStatus

#: Bedrock Converse input is cheap relative to a bad answer, but latency is not: the whole
#: material stays under a few thousand characters so a chat turn answers in seconds.
DEFAULT_MAX_DOCUMENTS = 6
DEFAULT_MAX_EXCERPTS = 24
DEFAULT_MAX_CHARS = 7000
DEFAULT_MAX_OUTLINE_ENTRIES = 40
#: One unit can be a long paragraph; the answer needs its gist, not all of it.
MAX_EXCERPT_CHARS = 600
#: The outline is context, the excerpts are the answer: the outline may take at most this share
#: of a document's character budget so a deep table of contents cannot crowd the excerpts out.
OUTLINE_BUDGET_SHARE = 0.35
#: A whole token the user typed ("s3", "cloudtrail", "암호화") is a much stronger signal than a
#: bigram derived from one ("정책" inside "정책은"), which matches half the document.
WHOLE_TOKEN_WEIGHT = 3
BIGRAM_WEIGHT = 1
#: A Latin/digit token in a governance question is almost always a service or control name
#: (S3, IAM, MFA, KMS, CloudTrail). It is the most specific thing the user said; a unit that
#: carries it is about the thing they asked about, whatever else it also mentions.
IDENTIFIER_WEIGHT = 9
#: Words a question is made of rather than about. Matching them rewards long introductory
#: paragraphs that happen to say "정책" and "설명" a lot, over the section the user meant.
QUESTION_STOPWORDS = frozenset(
    {
        "설명",
        "설명해줘",
        "설명해",
        "알려줘",
        "알려",
        "해줘",
        "해주세요",
        "주세요",
        "나열",
        "나열해줘",
        "목록",
        "정리",
        "정리해줘",
        "요약",
        "요약해줘",
        "보여줘",
        "무엇",
        "뭐야",
        "어떻게",
        "하나요",
        "인가요",
        "있나요",
        "대해",
        "대해서",
        "관련",
        "관해",
        "관해서",
        "우리",
        "사내",
        "회사",
        "정책",
        "규정",
        "기준",
        "내용",
        "질문",
        "please",
        "explain",
        "list",
        "show",
        "what",
        "how",
        "policy",
        "policies",
        "our",
        "the",
        "about",
    }
)

_TOKEN = re.compile(r"[0-9a-z]+|[가-힣]+")


class SourceCatalog(Protocol):
    def list_sources(self, *, customer_id: str) -> Sequence[dict[str, object]]: ...

    def get_document(self, *, customer_id: str, source_id: str, source_version: str) -> object: ...


class UnitReader(Protocol):
    def read(self, *, customer_id: str, document: object) -> Sequence[ExtractionUnit]: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyQaContext:
    """What the Parent may ground a POLICY_QA answer in.

    **No ``to_dict`` on purpose** — ``prompt_text`` carries the customer's policy wording and must
    reach only the model prompt. ``available`` is False when nothing usable exists (no READY
    document, or a read failed); the Parent then says so instead of inventing policy.
    """

    prompt_text: str
    document_count: int
    excerpt_count: int
    unavailable_reason: str | None = None

    @property
    def available(self) -> bool:
        return self.document_count > 0 and bool(self.prompt_text)


class PolicyQaContextBuilder:
    """Assemble one customer's policy material for one question, deterministically."""

    def __init__(
        self,
        *,
        catalog: SourceCatalog,
        reader: UnitReader,
        max_documents: int = DEFAULT_MAX_DOCUMENTS,
        max_excerpts: int = DEFAULT_MAX_EXCERPTS,
        max_chars: int = DEFAULT_MAX_CHARS,
        max_outline_entries: int = DEFAULT_MAX_OUTLINE_ENTRIES,
    ) -> None:
        if catalog is None or reader is None:
            raise TypeError("catalog and reader are required")
        for name, value in (
            ("max_documents", max_documents),
            ("max_excerpts", max_excerpts),
            ("max_chars", max_chars),
            ("max_outline_entries", max_outline_entries),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self._catalog = catalog
        self._reader = reader
        self._max_documents = max_documents
        self._max_excerpts = max_excerpts
        self._max_chars = max_chars
        self._max_outline_entries = max_outline_entries

    def build(self, *, customer_id: str, question: str) -> PolicyQaContext:
        """Never raises: a chat turn must not 500 because retrieval hiccupped.

        A failure is reported through ``unavailable_reason`` (a code, never document content) so
        the Parent can say "no material" honestly rather than answering from nothing.
        """
        if not isinstance(customer_id, str) or not customer_id.strip():
            raise ValueError("customer_id must be a non-empty string")
        if not isinstance(question, str):
            question = ""
        try:
            return self._build(customer_id, question)
        except Exception as error:  # noqa: BLE001 - fail-soft by design; the code, not the text, is kept
            return PolicyQaContext(
                prompt_text="",
                document_count=0,
                excerpt_count=0,
                unavailable_reason=type(error).__name__,
            )

    def _build(self, customer_id: str, question: str) -> PolicyQaContext:
        terms = _terms(question)
        seen_digests: set[str] = set()
        sections: list[str] = []
        document_count = 0
        excerpt_count = 0
        budget = self._max_chars

        for source in self._catalog.list_sources(customer_id=customer_id):
            if document_count >= self._max_documents:
                break
            if source.get("status") != IngestionStatus.READY.value:
                continue
            source_id, version = source.get("source_id"), source.get("source_version")
            if not isinstance(source_id, str) or not isinstance(version, str):
                continue
            document = self._catalog.get_document(
                customer_id=customer_id, source_id=source_id, source_version=version
            )
            # The same file uploaded twice is one document to the reader; repeating it would
            # spend the budget saying the same thing three times.
            digest = getattr(document, "normalized_sha256", None)
            if isinstance(digest, str):
                if digest in seen_digests:
                    continue
                seen_digests.add(digest)
            units = self._reader.read(customer_id=customer_id, document=document)
            document_count += 1
            filename = getattr(document, "filename", None) or source.get("filename") or source_id

            outline = [u for u in units if u.kind is DocumentUnitKind.SECTION]
            ranked = _rank(units, terms)
            block, used, taken = _format_document(
                index=document_count,
                filename=str(filename),
                source_id=source_id,
                version=version,
                outline=outline[: self._max_outline_entries],
                excerpts=ranked,
                max_excerpts=self._max_excerpts - excerpt_count,
                budget=budget,
            )
            sections.append(block)
            budget -= used
            excerpt_count += taken
            if budget <= 0:
                break

        if document_count == 0:
            return PolicyQaContext(
                prompt_text="",
                document_count=0,
                excerpt_count=0,
                unavailable_reason="NO_READY_DOCUMENT",
            )
        header = (
            f"고객 정책 자료 — 문서 {document_count}개, 관련 발췌 {excerpt_count}개. "
            "이 자료는 질문한 사용자 소속 고객이 업로드한 정책 문서에서 코드가 고른 것이다.\n"
        )
        return PolicyQaContext(
            prompt_text=header + "\n".join(sections),
            document_count=document_count,
            excerpt_count=excerpt_count,
        )


def _terms(question: str) -> dict[str, int]:
    """Lower-cased tokens plus Hangul bigrams, each with a weight.

    Korean has no spaces between a noun and its particle, so whole-token matching misses
    "정책은" against "정책". Bigrams of each Hangul token recover that without a morphological
    analyzer — but a bigram is a weak witness (it matches half the document), so it weighs less
    than the whole token the user actually typed. Latin/digit tokens (service and control names
    such as "s3") are whole tokens only.
    """
    weights: dict[str, int] = {}
    for token in _TOKEN.findall(question.lower()):
        if len(token) < 2 or token in QUESTION_STOPWORDS:
            continue
        hangul = "가" <= token[0] <= "힣"
        base = WHOLE_TOKEN_WEIGHT if hangul else IDENTIFIER_WEIGHT
        weights[token] = max(weights.get(token, 0), base * len(token))
        if hangul and len(token) > 2:
            for i in range(len(token) - 1):
                bigram = token[i : i + 2]
                if bigram not in QUESTION_STOPWORDS:
                    weights.setdefault(bigram, BIGRAM_WEIGHT * len(bigram))
    return weights


def _haystack(unit: ExtractionUnit) -> str:
    """The unit's text plus the section path it lives under.

    A list item under "5-1 S3 Security" may not repeat "S3" in its own sentence; the heading
    already said it. Locators carry that heading path as slugs, so folding them in lets every
    item of a section answer a question about that section.
    """
    path = unit.locator.lower().replace("/", " ").replace("-", " ").replace("_", " ")
    return unit.text.lower() + " " + path


def _rank(units: Sequence[ExtractionUnit], terms: dict[str, int]) -> list[ExtractionUnit]:
    """Weighted overlap, ties by document order. Zero-score units are not excerpts."""
    scored: list[tuple[int, int, ExtractionUnit]] = []
    for position, unit in enumerate(units):
        if unit.kind is DocumentUnitKind.SECTION:
            continue
        haystack = _haystack(unit)
        score = sum(weight for term, weight in terms.items() if term in haystack)
        if score > 0:
            scored.append((score, position, unit))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [unit for _score, _position, unit in scored]


def _format_document(
    *,
    index: int,
    filename: str,
    source_id: str,
    version: str,
    outline: Sequence[ExtractionUnit],
    excerpts: Sequence[ExtractionUnit],
    max_excerpts: int,
    budget: int,
) -> tuple[str, int, int]:
    lines = [f"## 문서 {index}: {filename}  [source {source_id} @ {version}]", "### 목차"]
    used = sum(len(line) + 1 for line in lines)
    outline_budget = int(budget * OUTLINE_BUDGET_SHARE)
    outline_used = 0
    for unit in outline:
        line = f"- {_clip(unit.text, 100)}  ({unit.locator})"
        if outline_used + len(line) + 1 > outline_budget:
            lines.append("- …")
            outline_used += 4
            break
        lines.append(line)
        outline_used += len(line) + 1
    if not outline:
        lines.append("- (제목 단위 없음)")
    used += outline_used
    lines.append("### 질문과 관련된 발췌")
    used += len(lines[-1]) + 1
    taken = 0
    for unit in excerpts:
        if taken >= max_excerpts:
            break
        line = f"[{unit.locator}] {_clip(unit.text, MAX_EXCERPT_CHARS)}"
        if used + len(line) + 1 > budget:
            break
        lines.append(line)
        used += len(line) + 1
        taken += 1
    if taken == 0:
        lines.append("(이 문서에서 질문과 직접 겹치는 단위를 찾지 못함 — 목차만 참고)")
    return "\n".join(lines), used, taken


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
