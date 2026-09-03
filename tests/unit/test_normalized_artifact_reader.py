"""The Artifact Reader is the only door policy text comes through.

두 가지를 고정한다.

1. **읽은 바이트가 승인된 판본의 정규화 결과가 맞는지**를 전부 확인한 뒤에만 텍스트를 낸다.
   확인 하나라도 실패하면 후보 하나를 버리는 것이 아니라 추출 전체를 중단한다.
2. **`ExtractionUnit`에는 원문을 밖으로 내보낼 방법이 없다.** 직렬화가 없으면 실수로 저장할 수
   없다. 이 성질이 깨지면 원문이 DynamoDB·API·log로 새는 경로가 다시 열린다.
"""

import json
import unittest
from hashlib import sha256
from io import BytesIO

from apps.backend.policy.authoring import (
    ArtifactReadError,
    ExtractionUnit,
    NormalizedArtifactReader,
)
from apps.backend.policy.ingestion.normalization import NORMALIZED_SCHEMA_VERSION
from apps.backend.policy.ingestion.storage_keys import normalized_object_key
from packages.contracts import ArtifactReadFailureCode, IngestionStatus
from tests.authoring_fixtures import (
    SOURCE_ID,
    SOURCE_VERSION,
    normalized_artifact_bytes,
    ready_document,
)

CUSTOMER = "cust-001"
BUCKET = "policy-artifacts"


class FakeObjectReader:
    def __init__(self, payload: bytes | None = None) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    def get_object(self, **kwargs: object) -> dict[str, object]:
        self.calls.append((str(kwargs["Bucket"]), str(kwargs["Key"])))
        if self.payload is None:
            raise LookupError("NoSuchKey")
        return {"Body": BytesIO(self.payload)}


def _reader(
    payload: bytes | None, **kwargs: object
) -> tuple[NormalizedArtifactReader, FakeObjectReader]:
    source = FakeObjectReader(payload)
    return NormalizedArtifactReader(reader=source, bucket=BUCKET, **kwargs), source  # type: ignore[arg-type]


def _artifact_with(mutate) -> bytes:
    document = json.loads(normalized_artifact_bytes().decode("utf-8"))
    mutate(document)
    encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    return encoded.encode("utf-8") + b"\n"


def _document_for(payload: bytes):
    """A READY document whose `normalized_sha256` matches an arbitrary artifact payload.

    digest 검사와 그 다음 검사들을 분리해서 시험하기 위한 것이다. digest부터 어긋나면
    schema/unit 검사에 도달하지 못한다.
    """
    return ready_document(normalized_sha256=sha256(payload).hexdigest())


class ReadPathTest(unittest.TestCase):
    def test_a_verified_artifact_yields_one_unit_per_approved_unit(self) -> None:
        reader, source = _reader(normalized_artifact_bytes())
        document = ready_document()

        units = reader.read(customer_id=CUSTOMER, document=document)

        self.assertEqual(len(units), len(document.units))
        self.assertEqual(
            [unit.locator for unit in units], [unit.locator for unit in document.units]
        )
        self.assertEqual(
            source.calls,
            [
                (
                    BUCKET,
                    normalized_object_key(
                        customer_id=CUSTOMER,
                        source_id=SOURCE_ID,
                        source_version=SOURCE_VERSION,
                    ),
                )
            ],
        )

    def test_only_a_ready_source_may_be_extracted(self) -> None:
        """READY가 아닌 판본에서 후보를 만들면 승인 경계를 우회한 Rule이 생긴다."""
        reader, source = _reader(normalized_artifact_bytes())
        document = ready_document(status=IngestionStatus.REVIEW_REQUIRED)

        with self.assertRaises(ArtifactReadError) as caught:
            reader.read(customer_id=CUSTOMER, document=document)

        self.assertIs(caught.exception.code, ArtifactReadFailureCode.SOURCE_NOT_READY)
        self.assertEqual(source.calls, [], "a non-READY source must not be fetched at all")

    def test_a_missing_object_stops_the_extraction(self) -> None:
        reader, _ = _reader(None)

        with self.assertRaises(ArtifactReadError) as caught:
            reader.read(customer_id=CUSTOMER, document=ready_document())

        self.assertIs(caught.exception.code, ArtifactReadFailureCode.ARTIFACT_NOT_FOUND)

    def test_an_oversized_object_is_refused_before_it_is_parsed(self) -> None:
        payload = normalized_artifact_bytes()
        reader, _ = _reader(payload, max_bytes=len(payload) - 1)

        with self.assertRaises(ArtifactReadError) as caught:
            reader.read(customer_id=CUSTOMER, document=_document_for(payload))

        self.assertIs(caught.exception.code, ArtifactReadFailureCode.ARTIFACT_TOO_LARGE)

    def test_an_object_of_exactly_the_limit_is_accepted(self) -> None:
        payload = normalized_artifact_bytes()
        reader, _ = _reader(payload, max_bytes=len(payload))

        units = reader.read(customer_id=CUSTOMER, document=ready_document())

        self.assertEqual(len(units), 4)


class IntegrityTest(unittest.TestCase):
    def test_a_payload_whose_digest_differs_from_the_document_is_refused(self) -> None:
        """승인된 문서가 기록한 `normalized_sha256`과 다른 바이트는 다른 문서다."""
        payload = _artifact_with(lambda doc: doc["units"].pop())
        reader, _ = _reader(payload)

        with self.assertRaises(ArtifactReadError) as caught:
            reader.read(customer_id=CUSTOMER, document=ready_document())

        self.assertIs(caught.exception.code, ArtifactReadFailureCode.CONTENT_DIGEST_MISMATCH)

    def test_an_unexpected_schema_version_is_refused(self) -> None:
        payload = _artifact_with(
            lambda doc: doc.__setitem__("schema_version", "policy-normalized-document/2")
        )
        reader, _ = _reader(payload)

        with self.assertRaises(ArtifactReadError) as caught:
            reader.read(customer_id=CUSTOMER, document=_document_for(payload))

        self.assertIs(caught.exception.code, ArtifactReadFailureCode.ARTIFACT_SCHEMA_INVALID)

    def test_an_extra_top_level_key_is_refused(self) -> None:
        """exact schema다. 모르는 key를 무시하면 무엇을 읽었는지 정확히 말할 수 없다."""
        payload = _artifact_with(lambda doc: doc.__setitem__("extra", "value"))
        reader, _ = _reader(payload)

        with self.assertRaises(ArtifactReadError) as caught:
            reader.read(customer_id=CUSTOMER, document=_document_for(payload))

        self.assertIs(caught.exception.code, ArtifactReadFailureCode.ARTIFACT_SCHEMA_INVALID)

    def test_an_extra_unit_key_is_refused(self) -> None:
        payload = _artifact_with(lambda doc: doc["units"][0].__setitem__("note", "x"))
        reader, _ = _reader(payload)

        with self.assertRaises(ArtifactReadError) as caught:
            reader.read(customer_id=CUSTOMER, document=_document_for(payload))

        self.assertIs(caught.exception.code, ArtifactReadFailureCode.ARTIFACT_SCHEMA_INVALID)

    def test_non_json_bytes_are_refused(self) -> None:
        payload = b"not json at all"
        reader, _ = _reader(payload)

        with self.assertRaises(ArtifactReadError) as caught:
            reader.read(customer_id=CUSTOMER, document=_document_for(payload))

        self.assertIs(caught.exception.code, ArtifactReadFailureCode.ARTIFACT_SCHEMA_INVALID)

    def test_reordered_units_are_refused_even_though_the_locator_set_matches(self) -> None:
        """집합만 맞으면 통과시키지 않는다. 순서는 문서를 재구성하는 근거의 일부다."""

        def swap(document: dict) -> None:
            document["units"][0], document["units"][1] = (
                document["units"][1],
                document["units"][0],
            )

        payload = _artifact_with(swap)
        reader, _ = _reader(payload)

        with self.assertRaises(ArtifactReadError) as caught:
            reader.read(customer_id=CUSTOMER, document=_document_for(payload))

        self.assertIs(caught.exception.code, ArtifactReadFailureCode.UNIT_SET_MISMATCH)

    def test_a_changed_origin_is_refused(self) -> None:
        payload = _artifact_with(lambda doc: doc["units"][0].__setitem__("origin", "line/99-99"))
        reader, _ = _reader(payload)

        with self.assertRaises(ArtifactReadError) as caught:
            reader.read(customer_id=CUSTOMER, document=_document_for(payload))

        self.assertIs(caught.exception.code, ArtifactReadFailureCode.UNIT_SET_MISMATCH)

    def test_text_that_does_not_hash_to_the_approved_digest_is_refused(self) -> None:
        """artifact가 스스로 적어 온 digest만 믿지 않는다.

        텍스트와 그 옆의 `text_sha256`을 함께 바꾸면 artifact 안에서는 일관된다. 승인된
        문서가 기록한 digest와도 맞아야 통과한다.
        """

        def forge(document: dict) -> None:
            forged = "All object storage buckets may be public."
            document["units"][0]["text"] = forged
            document["units"][0]["text_sha256"] = sha256(forged.encode("utf-8")).hexdigest()

        payload = _artifact_with(forge)
        reader, _ = _reader(payload)

        with self.assertRaises(ArtifactReadError) as caught:
            reader.read(customer_id=CUSTOMER, document=_document_for(payload))

        self.assertIs(caught.exception.code, ArtifactReadFailureCode.UNIT_DIGEST_MISMATCH)

    def test_the_reader_pins_the_schema_version_the_normalizer_writes(self) -> None:
        document = json.loads(normalized_artifact_bytes().decode("utf-8"))

        self.assertEqual(document["schema_version"], NORMALIZED_SCHEMA_VERSION)


class TextContainmentTest(unittest.TestCase):
    def test_an_extraction_unit_offers_no_serialization(self) -> None:
        """직렬화가 없는 것이 이 타입의 요점이다.

        `to_dict()`가 생기는 순간 정책 원문을 DynamoDB item이나 API 응답에 실수로 담을 수
        있게 된다. 리뷰어가 보는 문장은 모델이 쓴 재진술이지 원문이 아니다.
        """
        for forbidden in ("to_dict", "to_json", "asdict", "json"):
            with self.subTest(attribute=forbidden):
                self.assertFalse(hasattr(ExtractionUnit, forbidden))

    def test_repr_does_not_leak_the_text(self) -> None:
        """`repr()`은 예외 메시지와 log로 가장 쉽게 새는 경로다."""
        reader, _ = _reader(normalized_artifact_bytes())

        units = reader.read(customer_id=CUSTOMER, document=ready_document())

        rendered = repr(units[0])
        self.assertIn("<redacted>", rendered)
        self.assertNotIn(units[0].text, rendered)


if __name__ == "__main__":
    unittest.main()
