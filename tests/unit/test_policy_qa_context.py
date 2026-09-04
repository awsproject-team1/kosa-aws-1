"""Code, not the model, decides which policy text grounds a Policy Q&A answer.

The Parent answered "what is our S3 policy" with a textbook paragraph because it was given the
question and nothing else. These tests pin the retrieval that now sits in front of it: it reads
only the caller customer's READY documents, ranks units by lexical overlap (Korean included),
always carries the outline so "list our policies" has a list to walk, stays inside a budget, and
degrades to "no material" instead of raising.
"""

import unittest
from hashlib import sha256
from types import SimpleNamespace

from apps.backend.policy.authoring import ExtractionUnit
from apps.backend.policy.qa_context import PolicyQaContext, PolicyQaContextBuilder
from packages.contracts import DocumentUnitKind


def unit(locator: str, text: str, kind: DocumentUnitKind = DocumentUnitKind.PARAGRAPH):
    return ExtractionUnit(
        locator=locator,
        kind=kind,
        origin=locator,
        text=text,
        text_sha256=sha256(text.encode("utf-8")).hexdigest(),
    )


S3_UNITS = (
    unit("heading/1", "1. 사내 S3 정책", DocumentUnitKind.SECTION),
    unit(
        "heading/1/item/1",
        "모든 S3 버킷은 퍼블릭 액세스 차단 설정을 활성화해야 한다.",
        DocumentUnitKind.LIST_ITEM,
    ),
    unit(
        "heading/1/item/2",
        "S3 버킷의 서버 측 암호화는 SSE-KMS를 기본으로 한다.",
        DocumentUnitKind.LIST_ITEM,
    ),
    unit("heading/2", "2. IAM 정책", DocumentUnitKind.SECTION),
    unit("heading/2/item/1", "루트 계정 액세스 키는 발급하지 않는다.", DocumentUnitKind.LIST_ITEM),
    unit("heading/3", "3. 로깅", DocumentUnitKind.SECTION),
    unit("heading/3/para/1", "CloudTrail은 모든 리전에서 활성화한다.", DocumentUnitKind.PARAGRAPH),
)


class Catalog:
    def __init__(self, sources, documents):
        self.sources = sources
        self.documents = documents
        self.calls: list[tuple[str, str]] = []

    def list_sources(self, *, customer_id):
        self.calls.append(("list_sources", customer_id))
        return self.sources

    def get_document(self, *, customer_id, source_id, source_version):
        self.calls.append(("get_document", customer_id))
        return self.documents[(source_id, source_version)]


class Reader:
    def __init__(self, units_by_digest, error: Exception | None = None):
        self.units_by_digest = units_by_digest
        self.error = error
        self.customers: list[str] = []

    def read(self, *, customer_id, document):
        self.customers.append(customer_id)
        if self.error is not None:
            raise self.error
        return self.units_by_digest[document.normalized_sha256]


def source(source_id="src-1", version="v1", status="READY", filename="정책.md"):
    return {
        "source_id": source_id,
        "source_version": version,
        "status": status,
        "filename": filename,
    }


def document(source_id="src-1", version="v1", digest="d1", filename="정책.md"):
    return SimpleNamespace(
        source_id=source_id, source_version=version, normalized_sha256=digest, filename=filename
    )


def builder(sources, documents, units_by_digest, **kwargs):
    return PolicyQaContextBuilder(
        catalog=Catalog(sources, documents), reader=Reader(units_by_digest), **kwargs
    )


class GroundingSelectionTest(unittest.TestCase):
    def test_the_question_selects_matching_units_and_cites_their_locators(self) -> None:
        context = builder([source()], {("src-1", "v1"): document()}, {"d1": S3_UNITS}).build(
            customer_id="cust-a", question="사내 S3 정책 설명해줘"
        )

        self.assertTrue(context.available)
        self.assertIn("[heading/1/item/1]", context.prompt_text)
        self.assertIn("퍼블릭 액세스 차단", context.prompt_text)
        self.assertIn("[heading/1/item/2]", context.prompt_text)
        # Unrelated units are not excerpts — the budget is for what the question is about.
        self.assertNotIn("루트 계정 액세스 키", context.prompt_text)
        self.assertNotIn("CloudTrail", context.prompt_text.split("관련된 발췌")[1])

    def test_korean_particles_do_not_defeat_matching(self) -> None:
        """'정책은' in the question must still meet '정책' in the text — bigrams do that."""
        context = builder([source()], {("src-1", "v1"): document()}, {"d1": S3_UNITS}).build(
            customer_id="cust-a", question="암호화는 어떻게 하나요"
        )
        self.assertIn("SSE-KMS", context.prompt_text)

    def test_the_outline_is_always_present_so_a_list_question_has_a_list(self) -> None:
        context = builder([source()], {("src-1", "v1"): document()}, {"d1": S3_UNITS}).build(
            customer_id="cust-a", question="정책 나열해줘"
        )
        for heading in ("1. 사내 S3 정책", "2. IAM 정책", "3. 로깅"):
            self.assertIn(heading, context.prompt_text)
        self.assertIn("(heading/2)", context.prompt_text)

    def test_ranking_is_deterministic_for_the_same_inputs(self) -> None:
        make = lambda: builder(  # noqa: E731
            [source()], {("src-1", "v1"): document()}, {"d1": S3_UNITS}
        ).build(customer_id="cust-a", question="S3 버킷")
        self.assertEqual(make().prompt_text, make().prompt_text)


class RankingSignalsTest(unittest.TestCase):
    """What makes the S3 section beat the introduction for an S3 question."""

    INTRO = unit(
        "heading/intro/para/1",
        "이 문서는 사내 클라우드 보안 정책을 설명한다. 정책은 사내 기준을 정리한 것이다.",
    )
    S3_ITEM = unit(
        "heading/part-2/5-1-s3-security/item/2",
        "버킷은 기본 암호화를 켜고 버전 관리를 사용한다.",
        DocumentUnitKind.LIST_ITEM,
    )

    def _first_excerpt(self, question: str, *units):
        context = builder([source()], {("src-1", "v1"): document()}, {"d1": tuple(units)}).build(
            customer_id="cust-a", question=question
        )
        excerpts = context.prompt_text.split("관련된 발췌")[1]
        return excerpts.strip().splitlines()[0]

    def test_a_section_heading_in_the_locator_counts_as_a_match(self) -> None:
        """The item never says 'S3' itself; its section does, and that is enough."""
        first = self._first_excerpt("사내 S3 정책 설명해줘", self.INTRO, self.S3_ITEM)
        self.assertIn("5-1-s3-security", first)

    def test_question_filler_words_do_not_score(self) -> None:
        """'사내', '정책', '설명해줘' are the question's grammar, not its subject."""
        context = builder([source()], {("src-1", "v1"): document()}, {"d1": (self.INTRO,)}).build(
            customer_id="cust-a", question="사내 정책 설명해줘"
        )
        self.assertEqual(context.excerpt_count, 0)

    def test_a_service_identifier_outweighs_common_korean_words(self) -> None:
        iam_item = unit(
            "heading/x/item/1", "IAM 사용자에게 MFA를 강제한다.", DocumentUnitKind.LIST_ITEM
        )
        chatty = unit("heading/y/para/1", "암호화 암호화 암호화 관련 안내 문단이다.")
        first = self._first_excerpt("IAM MFA 암호화", chatty, iam_item)
        self.assertIn("heading/x/item/1", first)


class ScopeAndBudgetTest(unittest.TestCase):
    def test_only_ready_documents_are_read(self) -> None:
        sources = [source("src-1", "v1", "READY"), source("src-2", "v1", "FAILED")]
        documents = {
            ("src-1", "v1"): document("src-1"),
            ("src-2", "v1"): document("src-2", digest="d2"),
        }
        b = builder(sources, documents, {"d1": S3_UNITS, "d2": S3_UNITS})
        context = b.build(customer_id="cust-a", question="S3")
        self.assertEqual(context.document_count, 1)

    def test_identical_uploads_count_once(self) -> None:
        """The same file uploaded three times is one document to the reader, not three."""
        sources = [source(f"src-{i}", "v1") for i in range(3)]
        documents = {(f"src-{i}", "v1"): document(f"src-{i}", digest="same") for i in range(3)}
        context = builder(sources, documents, {"same": S3_UNITS}).build(
            customer_id="cust-a", question="S3"
        )
        self.assertEqual(context.document_count, 1)
        self.assertEqual(context.prompt_text.count("## 문서"), 1)

    def test_every_read_is_keyed_by_the_callers_customer(self) -> None:
        catalog = Catalog([source()], {("src-1", "v1"): document()})
        reader = Reader({"d1": S3_UNITS})
        PolicyQaContextBuilder(catalog=catalog, reader=reader).build(
            customer_id="cust-a", question="S3"
        )
        self.assertEqual({c for _n, c in catalog.calls}, {"cust-a"})
        self.assertEqual(reader.customers, ["cust-a"])

    def test_the_excerpt_cap_and_character_budget_hold(self) -> None:
        many = tuple(
            unit(
                f"heading/1/item/{i}", f"S3 버킷 규칙 {i}: " + "x" * 200, DocumentUnitKind.LIST_ITEM
            )
            for i in range(50)
        )
        context = builder(
            [source()], {("src-1", "v1"): document()}, {"d1": many}, max_excerpts=5
        ).build(customer_id="cust-a", question="S3 버킷 규칙")
        self.assertEqual(context.excerpt_count, 5)

        tight = builder(
            [source()], {("src-1", "v1"): document()}, {"d1": many}, max_chars=900
        ).build(customer_id="cust-a", question="S3 버킷 규칙")
        self.assertLessEqual(len(tight.prompt_text), 900 + 200)  # header + one clipped line

    def test_long_units_are_clipped(self) -> None:
        long = (unit("heading/1/para/1", "S3 " + "가" * 2000),)
        context = builder([source()], {("src-1", "v1"): document()}, {"d1": long}).build(
            customer_id="cust-a", question="S3"
        )
        self.assertNotIn("가" * 700, context.prompt_text)
        self.assertIn("…", context.prompt_text)


class FailSoftTest(unittest.TestCase):
    def test_no_ready_document_is_unavailable_not_an_error(self) -> None:
        context = builder([source(status="UPLOADED")], {}, {}).build(
            customer_id="cust-a", question="S3"
        )
        self.assertFalse(context.available)
        self.assertEqual(context.unavailable_reason, "NO_READY_DOCUMENT")

    def test_a_reader_failure_is_reported_as_a_code_never_raised(self) -> None:
        catalog = Catalog([source()], {("src-1", "v1"): document()})
        reader = Reader({}, error=RuntimeError("s3 down: bucket customers/cust-a/..."))
        context = PolicyQaContextBuilder(catalog=catalog, reader=reader).build(
            customer_id="cust-a", question="S3"
        )
        self.assertFalse(context.available)
        self.assertEqual(context.unavailable_reason, "RuntimeError")
        self.assertNotIn("customers/", context.unavailable_reason or "")

    def test_the_context_has_no_serializer(self) -> None:
        """Policy text goes to the prompt and nowhere else; a to_dict would be the first leak."""
        self.assertFalse(hasattr(PolicyQaContext, "to_dict"))

    def test_a_blank_customer_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            builder([], {}, {}).build(customer_id=" ", question="S3")


if __name__ == "__main__":
    unittest.main()
