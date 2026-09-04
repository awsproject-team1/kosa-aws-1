"""The model proposes; it never decides.

이 파일은 모델 응답을 해석하는 규칙을 고정한다. **allow-list 밖의 것은 제거하지 않고 거부한다.**
조용히 제거하면 모델이 무엇을 시도했는지가 사라지고, 남은 결과만 보면 모델이 규칙을 지킨 것처럼
보인다.
"""

import json
import unittest
from io import BytesIO

from apps.backend.policy.authoring import (
    BedrockExtractionError,
    BedrockPolicyCandidateExtractor,
    NormalizedArtifactReader,
)
from apps.backend.policy.authoring.bedrock_extractor import (
    MAX_REQUIREMENTS_PER_CHUNK,
    PROMPT_VERSION,
    ChunkAccountingError,
    PoisonedResponseError,
    _catalog_prompt_view,
    _chunks,
    _redacted,
)
from apps.backend.policy.control_catalog import MVP_CONTROL_CATALOG
from packages.contracts import ModelProfile, ModelProfileRole
from tests.authoring_fixtures import (
    UNIT_TEXTS,
    normalized_artifact_bytes,
    ready_document,
)

DOCUMENT = ready_document()
STORAGE_LOCATOR = UNIT_TEXTS[0][0]
DATABASE_LOCATOR = UNIT_TEXTS[1][0]

AUTHORING_PROFILE = ModelProfile(
    model_profile_id="policy-authoring-v1",
    role=ModelProfileRole.POLICY_AUTHORING,
    region="us-east-1",
    model_id="anthropic.claude",
    prompt_version=PROMPT_VERSION,
    rubric_version="policy-authoring-rubric/1",
    golden_dataset_version="policy-authoring-golden/1",
)

ASSESSMENT_PROFILE = ModelProfile(
    model_profile_id="assessment-v1",
    role=ModelProfileRole.ASSESSMENT,
    region="us-east-1",
    model_id="anthropic.claude",
    prompt_version="assessment/1",
    rubric_version="assessment-rubric/1",
    golden_dataset_version="assessment-golden/1",
)

VALID_REQUIREMENT = {
    "source_locators": [STORAGE_LOCATOR],
    "requirement": "Object storage must not permit public access in any form.",
    "requirement_summary": "Buckets block public access",
    "classification": "AUTOMATABLE",
    "mapping_reason": "The sentence names object storage and public access.",
    "mapped_control_key": "S3_BLOCK_PUBLIC_ACCESS",
    "resource_types": ["AWS::S3::Bucket"],
    "evaluation_type": "AWS",
    "required_evidence": ["S3.PUBLIC_ACCESS_BLOCK"],
    "evaluation_rubric": "Fail when any block-public-access flag is false.",
}


class FakeBedrock:
    def __init__(self, *payloads: object) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict[str, object]] = []

    def converse(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        payload = self.payloads[min(len(self.calls) - 1, len(self.payloads) - 1)]
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return {"output": {"message": {"content": [{"text": text}]}}}


def _units():
    reader = NormalizedArtifactReader(reader=_Source(), bucket="artifacts")  # type: ignore[arg-type]
    return reader.read(customer_id="cust-001", document=DOCUMENT)


class _Source:
    def get_object(self, **_kwargs: object) -> dict[str, object]:
        return {"Body": BytesIO(normalized_artifact_bytes())}


def _response(requirements: list[object], locators: list[str] | None = None) -> dict[str, object]:
    allowed = locators or [locator for locator, _kind, _text in UNIT_TEXTS]
    cited = {
        locator
        for requirement in requirements
        if isinstance(requirement, dict)
        for locator in requirement.get("source_locators", [])
        if isinstance(locator, str) and locator in allowed
    }
    return {
        "requirements": requirements,
        "non_requirement_locators": [locator for locator in allowed if locator not in cited],
    }


def _extract(payload: object, *, complete: bool = True, **kwargs: object):
    if (
        complete
        and isinstance(payload, dict)
        and "requirements" in payload
        and "non_requirement_locators" not in payload
    ):
        completed = _response(payload["requirements"])
        payload = {**payload, "non_requirement_locators": completed["non_requirement_locators"]}
    client = FakeBedrock(payload)
    extractor = BedrockPolicyCandidateExtractor(
        client=client,  # type: ignore[arg-type]
        model_profile=AUTHORING_PROFILE,
        **kwargs,  # type: ignore[arg-type]
    )
    return extractor.extract(document=DOCUMENT, units=_units(), catalog=MVP_CONTROL_CATALOG), client


class ApprovedProfileTest(unittest.TestCase):
    def test_an_assessment_profile_may_not_extract_policy(self) -> None:
        """역할이 없으면 승인 경계가 역할별로 존재하지 않게 된다."""
        with self.assertRaisesRegex(BedrockExtractionError, "not approved for policy authoring"):
            BedrockPolicyCandidateExtractor(
                client=FakeBedrock(),  # type: ignore[arg-type]
                model_profile=ASSESSMENT_PROFILE,
            )

    def test_the_identity_records_the_approved_model_and_prompt(self) -> None:
        extractor = BedrockPolicyCandidateExtractor(
            client=FakeBedrock(),  # type: ignore[arg-type]
            model_profile=AUTHORING_PROFILE,
        )

        identity = extractor.identity

        self.assertEqual(identity.model_id, "anthropic.claude")
        self.assertEqual(identity.prompt_version, PROMPT_VERSION)

    def test_a_stale_prompt_profile_is_refused(self) -> None:
        stale = ModelProfile(
            model_profile_id="policy-authoring-stale",
            role=ModelProfileRole.POLICY_AUTHORING,
            region="us-east-1",
            model_id="anthropic.claude",
            prompt_version="policy-authoring/2026-09-03",
            rubric_version="policy-authoring-rubric/1",
            golden_dataset_version="policy-authoring-golden/1",
        )

        with self.assertRaisesRegex(BedrockExtractionError, "prompt version"):
            BedrockPolicyCandidateExtractor(
                client=FakeBedrock(),  # type: ignore[arg-type]
                model_profile=stale,
            )


class RequestTest(unittest.TestCase):
    def test_the_request_is_deterministic_and_temperature_zero(self) -> None:
        _requirements, client = _extract({"requirements": []})

        request = client.calls[0]
        self.assertEqual(request["inferenceConfig"], {"temperature": 0, "maxTokens": 8192})
        self.assertEqual(request["modelId"], "anthropic.claude")

    def test_known_unsupported_controls_are_not_offered_to_the_model(self) -> None:
        """제시하면 모델이 그것을 자동 평가 가능한 선택지로 취급하고 실행 경로 없는 Rule을 낸다."""
        view = _catalog_prompt_view(MVP_CONTROL_CATALOG)

        keys = {entry["control_key"] for entry in view}
        self.assertNotIn("EC2_SNAPSHOT_NOT_PUBLIC", keys)
        self.assertIn("S3_BLOCK_PUBLIC_ACCESS", keys)
        self.assertIn("ORGANIZATIONAL_CONTROL_MANUAL_REVIEW", keys)

    def test_iac_hints_reach_the_prompt_but_document_paths_do_not(self) -> None:
        """hint는 prompt 경계 설명이다. AWS document path는 모델이 알 필요가 없다."""
        view = _catalog_prompt_view(MVP_CONTROL_CATALOG)

        for control in view:
            for capability in control["evidence_capabilities"]:  # type: ignore[index]
                with self.subTest(capability=capability["capability_key"]):
                    self.assertNotIn("document_paths", capability)


class ResponseGateTest(unittest.TestCase):
    def test_a_valid_response_becomes_one_requirement(self) -> None:
        requirements, _client = _extract({"requirements": [VALID_REQUIREMENT]})

        self.assertEqual(len(requirements), 1)
        self.assertEqual(requirements[0].mapped_control_key, "S3_BLOCK_PUBLIC_ACCESS")

    def test_a_json_response_wrapped_in_a_code_fence_is_accepted(self) -> None:
        """Nova는 완결된 JSON을 ```json ... ``` 펜스로 감싸 반환한다. 감싼 펜스만 벗겨 파싱한다."""
        body = json.dumps(_response([VALID_REQUIREMENT]))
        fenced = f"```json\n{body}\n```"
        requirements, _client = _extract(fenced)

        self.assertEqual(len(requirements), 1)
        self.assertEqual(requirements[0].mapped_control_key, "S3_BLOCK_PUBLIC_ACCESS")

    def test_a_non_json_response_is_refused(self) -> None:
        """자유 텍스트에서 값을 캐내지 않는다. JSON이 아니면 응답 전체가 신뢰할 수 없다."""
        with self.assertRaisesRegex(BedrockExtractionError, "not JSON"):
            _extract("Here are the requirements I found: ...")

    def test_an_unexpected_top_level_key_is_refused(self) -> None:
        with self.assertRaisesRegex(BedrockExtractionError, "fields are invalid"):
            _extract({"requirements": [], "notes": "extra"})

    def test_the_locator_accounting_field_is_required(self) -> None:
        with self.assertRaisesRegex(BedrockExtractionError, "fields are invalid"):
            _extract({"requirements": []}, complete=False)

    def test_an_evaluation_outcome_field_rejects_the_whole_response(self) -> None:
        """조용히 버리면 모델이 판정을 시도했다는 사실 자체가 사라진다."""
        for forbidden in ("judgment", "severity", "score", "source_score", "anchor"):
            with self.subTest(field=forbidden):
                entry = {**VALID_REQUIREMENT, forbidden: "HIGH"}
                with self.assertRaisesRegex(
                    BedrockExtractionError, "returned an evaluation outcome field"
                ):
                    _extract({"requirements": [entry]})

    def test_an_unknown_requirement_field_fails_the_run(self) -> None:
        bad = {**VALID_REQUIREMENT, "confidence": 0.9, "source_locators": [DATABASE_LOCATOR]}
        with self.assertRaisesRegex(BedrockExtractionError, "fields are invalid"):
            _extract({"requirements": [VALID_REQUIREMENT, bad]})

    def test_an_invented_locator_fails_the_run(self) -> None:
        bad = {**VALID_REQUIREMENT, "source_locators": ["heading/invented/item/1"]}
        with self.assertRaisesRegex(BedrockExtractionError, "outside this chunk"):
            _extract({"requirements": [VALID_REQUIREMENT, bad]})

    def test_a_requirement_outside_the_catalog_survives_extraction_for_the_builder_to_reject(
        self,
    ) -> None:
        """Catalog 경계는 `build_candidate`가 판정한다. 추출기는 그것을 응답 오류로 다루지 않는다.

        카탈로그에 없는 통제를 지목한 요구사항은 **평가할 수 없는 요구사항**이지 믿을 수 없는
        응답이 아니다. 추출기가 이것을 예외로 올리면 그 판정이 청크 전체를 죽이고, 같은 청크에
        들어 있던 멀쩡한 요구사항까지 사라진다 — 라이브 193 unit 문서의 39 청크 중 13개가 오직
        이 이유로 실패했다. 경계 자체는 그대로다: 그런 후보는 승인 가능한 Rule이 되지 못하고,
        사유 코드와 함께 보존된다(`test_policy_authoring_pipeline`).
        """
        outside = [
            {
                **VALID_REQUIREMENT,
                "mapped_control_key": "NOT_A_CONTROL",
                "source_locators": [DATABASE_LOCATOR],
            },
            {
                **VALID_REQUIREMENT,
                "required_evidence": ["S3.PUBLIC_ACCESS_BLOCK", "S3.INVENTED"],
                "source_locators": [DATABASE_LOCATOR],
            },
            {
                **VALID_REQUIREMENT,
                "resource_types": ["AWS::RDS::DBInstance"],
                "source_locators": [DATABASE_LOCATOR],
            },
        ]
        for bad in outside:
            with self.subTest(field=sorted(set(bad) - set(VALID_REQUIREMENT)) or "overridden"):
                requirements, _client = _extract({"requirements": [VALID_REQUIREMENT, bad]})

                # 두 요구사항 모두 남는다. 하나가 카탈로그 밖이라고 다른 하나를 잃지 않는다.
                self.assertEqual(len(requirements), 2)

    def test_an_overlong_field_fails_the_run(self) -> None:
        bad = {
            **VALID_REQUIREMENT,
            "requirement": "x" * 5000,
            "source_locators": [DATABASE_LOCATOR],
        }
        with self.assertRaisesRegex(BedrockExtractionError, "longer than"):
            _extract({"requirements": [VALID_REQUIREMENT, bad]})

    def test_too_many_requirements_in_one_chunk_are_refused(self) -> None:
        entries = [
            {**VALID_REQUIREMENT, "requirement": f"Requirement number {index}."}
            for index in range(MAX_REQUIREMENTS_PER_CHUNK + 1)
        ]
        with self.assertRaisesRegex(BedrockExtractionError, "more requirements"):
            _extract({"requirements": entries})

    def test_a_shape_the_classification_invariants_reject_is_refused(self) -> None:
        """분류가 말하는 것과 채워진 필드가 어긋나면 부분 결과를 남기지 않는다."""
        bad = {
            **VALID_REQUIREMENT,
            "classification": "UNSUPPORTED",
            "source_locators": [DATABASE_LOCATOR],
        }
        with self.assertRaisesRegex(BedrockExtractionError, "invalid requirement shape"):
            _extract({"requirements": [VALID_REQUIREMENT, bad]})

    def test_every_policy_unit_must_be_classified(self) -> None:
        payload = {
            "requirements": [VALID_REQUIREMENT],
            "non_requirement_locators": [DATABASE_LOCATOR],
        }

        with self.assertRaisesRegex(BedrockExtractionError, "classify every policy unit"):
            _extract(payload, complete=False)

    def test_a_locator_cannot_be_requirement_and_non_requirement(self) -> None:
        payload = _response([VALID_REQUIREMENT])
        payload["non_requirement_locators"] = [
            *payload["non_requirement_locators"],  # type: ignore[list-item]
            STORAGE_LOCATOR,
        ]

        with self.assertRaisesRegex(BedrockExtractionError, "both a requirement"):
            _extract(payload, complete=False)

    def test_non_requirement_locators_cannot_repeat_or_be_invented(self) -> None:
        all_locators = [locator for locator, _kind, _text in UNIT_TEXTS]
        for invalid, message in (
            ([*all_locators, all_locators[0]], "must not repeat"),
            ([*all_locators, "heading/invented/item/1"], "outside this chunk"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(BedrockExtractionError, message):
                    _extract(
                        {"requirements": [], "non_requirement_locators": invalid},
                        complete=False,
                    )


class ChunkingTest(unittest.TestCase):
    def test_chunks_overlap_and_cover_every_unit(self) -> None:
        units = _units()

        windows = _chunks(units, 2, 1)

        covered = {unit.locator for window in windows for unit in window}
        self.assertEqual(covered, {unit.locator for unit in units})
        self.assertGreater(len(windows), 1)

    def test_a_document_smaller_than_one_chunk_is_one_window(self) -> None:
        units = _units()

        self.assertEqual(_chunks(units, 40, 4), (units,))

    def test_the_same_requirement_seen_in_two_windows_is_merged_once(self) -> None:
        """겹치는 unit에서 같은 Requirement가 두 번 나올 수 있다. digest가 같으면 같은 것이다.

        window는 [u0,u1], [u1,u2], [u2,u3]이므로 u1을 인용한 Requirement는 앞의 두 window에서
        모두 유효하다. 병합하지 않으면 같은 후보가 두 개의 Rule이 된다.
        """
        overlapping = {
            **VALID_REQUIREMENT,
            "source_locators": [DATABASE_LOCATOR],
            "requirement": "Managed database storage must be encrypted at rest.",
            "requirement_summary": "Databases encrypt storage",
            "mapped_control_key": "RDS_ENCRYPTION_AT_REST",
            "resource_types": ["AWS::RDS::DBInstance"],
            "required_evidence": ["RDS.STORAGE_ENCRYPTED"],
        }
        client = FakeBedrock(
            _response([overlapping], [UNIT_TEXTS[0][0], UNIT_TEXTS[1][0]]),
            _response([overlapping], [UNIT_TEXTS[1][0], UNIT_TEXTS[2][0]]),
            _response([], [UNIT_TEXTS[2][0], UNIT_TEXTS[3][0]]),
        )
        extractor = BedrockPolicyCandidateExtractor(
            client=client,  # type: ignore[arg-type]
            model_profile=AUTHORING_PROFILE,
            units_per_chunk=2,
            chunk_overlap=1,
        )

        requirements = extractor.extract(
            document=DOCUMENT, units=_units(), catalog=MVP_CONTROL_CATALOG
        )

        self.assertEqual(len(client.calls), 3)
        self.assertEqual(len(requirements), 1)

    def test_one_failed_middle_chunk_fails_the_whole_document(self) -> None:
        """청크 하나가 끝내 실패하면 문서 전체가 실패한다. 재시도가 그 규칙을 바꾸지 않는다."""
        client = FakeBedrock(
            _response([], [UNIT_TEXTS[0][0], UNIT_TEXTS[1][0]]),
            "truncated response",
        )
        extractor = BedrockPolicyCandidateExtractor(
            client=client,  # type: ignore[arg-type]
            model_profile=AUTHORING_PROFILE,
            units_per_chunk=2,
            chunk_overlap=1,
        )

        with self.assertRaisesRegex(BedrockExtractionError, "not JSON"):
            extractor.extract(document=DOCUMENT, units=_units(), catalog=MVP_CONTROL_CATALOG)
        # 첫 청크 1회 + 두 번째 청크 3회. 세 번 다 쓸 수 없는 응답이라 문서가 실패한다.
        self.assertEqual(len(client.calls), 4)

    def test_the_merged_order_does_not_depend_on_the_model_output_order(self) -> None:
        second = {
            **VALID_REQUIREMENT,
            "requirement": "Managed database storage must be encrypted at rest.",
            "requirement_summary": "Databases encrypt storage",
            "mapped_control_key": "RDS_ENCRYPTION_AT_REST",
            "resource_types": ["AWS::RDS::DBInstance"],
            "required_evidence": ["RDS.STORAGE_ENCRYPTED"],
        }

        forward, _client = _extract({"requirements": [VALID_REQUIREMENT, second]})
        reverse, _client = _extract({"requirements": [second, VALID_REQUIREMENT]})

        self.assertEqual([entry.digest for entry in forward], [entry.digest for entry in reverse])


class RejectionLoggingTest(unittest.TestCase):
    """A rejected model response is logged without exposing customer policy text."""

    def test_the_rejection_reason_is_logged_without_the_policy_sentence(self) -> None:
        bad = {**VALID_REQUIREMENT, "classification": "UNSUPPORTED"}
        with self.assertLogs("governance.authoring", level="WARNING") as logs:
            with self.assertRaises(BedrockExtractionError):
                _extract({"requirements": [bad, VALID_REQUIREMENT]})
        rejection = next(line for line in logs.output if "requirement rejected" in line)
        self.assertIn("must not carry rule semantics", rejection)
        self.assertNotIn(VALID_REQUIREMENT["requirement"], rejection)

    def test_redaction_keeps_the_rule_text_and_drops_the_value(self) -> None:
        self.assertEqual(
            _redacted("source_locators must not repeat 'heading/tenant-secret/item/3'"),
            "source_locators must not repeat '<redacted>'",
        )
        self.assertEqual(
            _redacted("an AUTOMATABLE requirement must map to a control"),
            "an AUTOMATABLE requirement must map to a control",
        )


class ChunkRepairTest(unittest.TestCase):
    """누락된 locator를 이름으로 알려 한 번 더 물어본다.

    완결성 게이트는 청크마다 걸리고 문서는 청크가 하나라도 실패하면 실패한다. 그래서 청크 실패
    확률이 조금만 있어도 긴 문서는 거의 확실히 실패했다 — 라이브의 193 unit 문서는 39 청크가
    되고 3/3 실패했다. 재시도는 게이트를 무르게 하지 않는다. 같은 게이트가 그대로 걸리고,
    부분 결과는 여전히 저장되지 않으며, 마지막 시도까지 누락이 남으면 예전과 똑같이 실패한다.
    """

    @staticmethod
    def _incomplete() -> dict[str, object]:
        """한 locator를 두 목록 어디에도 넣지 않은 응답."""
        allowed = [locator for locator, _kind, _text in UNIT_TEXTS]
        return {
            "requirements": [VALID_REQUIREMENT],
            "non_requirement_locators": [
                locator for locator in allowed if locator not in {STORAGE_LOCATOR, DATABASE_LOCATOR}
            ],
        }

    def _extractor(self, client: "FakeBedrock", **kwargs: object):
        return BedrockPolicyCandidateExtractor(
            client=client,  # type: ignore[arg-type]
            model_profile=AUTHORING_PROFILE,
            **kwargs,  # type: ignore[arg-type]
        )

    def test_an_omitted_locator_is_re_asked_and_the_chunk_succeeds(self) -> None:
        client = FakeBedrock(self._incomplete(), _response([VALID_REQUIREMENT]))

        requirements = self._extractor(client).extract(
            document=DOCUMENT, units=_units(), catalog=MVP_CONTROL_CATALOG
        )

        self.assertEqual(len(requirements), 1)
        self.assertEqual(len(client.calls), 2)

    def test_the_repair_request_names_exactly_the_locators_that_were_left_out(self) -> None:
        """게이트가 실제로 본 사실을 그대로 옮긴다 — 추측한 목록이 아니다."""
        client = FakeBedrock(self._incomplete(), _response([VALID_REQUIREMENT]))

        self._extractor(client).extract(
            document=DOCUMENT, units=_units(), catalog=MVP_CONTROL_CATALOG
        )

        first = json.loads(client.calls[0]["messages"][0]["content"][0]["text"])
        repair = json.loads(client.calls[1]["messages"][0]["content"][0]["text"])
        self.assertNotIn("unclassified_locators", first)
        self.assertEqual(repair["unclassified_locators"], [DATABASE_LOCATOR])
        self.assertEqual(repair["policy_units"], first["policy_units"])

    def test_a_chunk_that_keeps_omitting_still_fails_the_document(self) -> None:
        """마지막 시도까지 누락이 남으면 예전과 똑같이 실패한다. 게이트는 그대로다."""
        client = FakeBedrock(self._incomplete())

        with self.assertRaises(ChunkAccountingError):
            self._extractor(client).extract(
                document=DOCUMENT, units=_units(), catalog=MVP_CONTROL_CATALOG
            )

        self.assertEqual(len(client.calls), 3)

    def test_a_single_attempt_restores_the_old_behaviour(self) -> None:
        client = FakeBedrock(self._incomplete())

        with self.assertRaises(ChunkAccountingError):
            self._extractor(client, max_chunk_attempts=1).extract(
                document=DOCUMENT, units=_units(), catalog=MVP_CONTROL_CATALOG
            )

        self.assertEqual(len(client.calls), 1)

    def test_a_locator_in_both_lists_is_named_back_and_repaired(self) -> None:
        """누락과 같은 성격의 회계 오류다. 어느 locator가 겹쳤는지 그대로 알려준다."""
        allowed = [locator for locator, _kind, _text in UNIT_TEXTS]
        both = {
            "requirements": [VALID_REQUIREMENT],
            "non_requirement_locators": allowed,
        }
        client = FakeBedrock(both, _response([VALID_REQUIREMENT]))

        requirements = self._extractor(client).extract(
            document=DOCUMENT, units=_units(), catalog=MVP_CONTROL_CATALOG
        )

        self.assertEqual(len(requirements), 1)
        repair = json.loads(client.calls[1]["messages"][0]["content"][0]["text"])
        self.assertEqual(repair["double_classified_locators"], [STORAGE_LOCATOR])

    def test_an_unusable_response_is_re_asked_without_a_hint(self) -> None:
        """잘린 JSON에는 알려줄 내용이 없다. 실패가 생성 쪽이므로 같은 요청을 다시 보낸다."""
        client = FakeBedrock("truncated", _response([VALID_REQUIREMENT]))

        requirements = self._extractor(client).extract(
            document=DOCUMENT, units=_units(), catalog=MVP_CONTROL_CATALOG
        )

        self.assertEqual(len(requirements), 1)
        second = json.loads(client.calls[1]["messages"][0]["content"][0]["text"])
        self.assertNotIn("unclassified_locators", second)
        self.assertNotIn("double_classified_locators", second)

    def test_a_response_that_attempted_an_evaluation_is_never_re_asked(self) -> None:
        """확률적 실수가 아니라 경계 위반이다. 다시 물어 통과시키면 그 사실이 사라진다."""
        poisoned = {**VALID_REQUIREMENT, "score": 100}
        client = FakeBedrock(_response([poisoned]), _response([VALID_REQUIREMENT]))

        with self.assertRaises(PoisonedResponseError):
            self._extractor(client).extract(
                document=DOCUMENT, units=_units(), catalog=MVP_CONTROL_CATALOG
            )

        self.assertEqual(len(client.calls), 1)

    def test_zero_attempts_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self._extractor(FakeBedrock(), max_chunk_attempts=0)


if __name__ == "__main__":
    unittest.main()
