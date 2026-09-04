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
    prompt_version="policy-authoring/2026-09-03",
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


def _extract(payload: object, **kwargs: object):
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
        self.assertEqual(identity.prompt_version, "policy-authoring/2026-09-03")


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
        body = json.dumps({"requirements": [VALID_REQUIREMENT]})
        fenced = f"```json\n{body}\n```"
        requirements, _client = _extract(fenced)

        self.assertEqual(len(requirements), 1)
        self.assertEqual(requirements[0].mapped_control_key, "S3_BLOCK_PUBLIC_ACCESS")

    def test_a_non_json_response_is_refused(self) -> None:
        """자유 텍스트에서 값을 캐내지 않는다. JSON이 아니면 응답 전체가 신뢰할 수 없다."""
        with self.assertRaisesRegex(BedrockExtractionError, "every chunk failed"):
            _extract("Here are the requirements I found: ...")

    def test_an_unexpected_top_level_key_is_refused(self) -> None:
        with self.assertRaisesRegex(BedrockExtractionError, "every chunk failed"):
            _extract({"requirements": [], "notes": "extra"})

    def test_an_evaluation_outcome_field_rejects_the_whole_response(self) -> None:
        """조용히 버리면 모델이 판정을 시도했다는 사실 자체가 사라진다."""
        for forbidden in ("judgment", "severity", "score", "source_score", "anchor"):
            with self.subTest(field=forbidden):
                entry = {**VALID_REQUIREMENT, forbidden: "HIGH"}
                with self.assertRaisesRegex(
                    BedrockExtractionError, "returned an evaluation outcome field"
                ):
                    _extract({"requirements": [entry]})

    def test_an_unknown_requirement_field_is_skipped_not_fatal(self) -> None:
        # fail-soft: 한 후보의 알 수 없는 필드는 그 후보만 건너뛰고, 같은 응답의 정상 후보는 남는다.
        bad = {**VALID_REQUIREMENT, "confidence": 0.9, "source_locators": [DATABASE_LOCATOR]}
        requirements, _client = _extract({"requirements": [VALID_REQUIREMENT, bad]})
        self.assertEqual(len(requirements), 1)

    def test_an_invented_locator_is_skipped_not_fatal(self) -> None:
        """모델이 지어낸 locator를 가진 후보만 버린다. 정상 후보는 유지한다."""
        bad = {**VALID_REQUIREMENT, "source_locators": ["heading/invented/item/1"]}
        requirements, _client = _extract({"requirements": [VALID_REQUIREMENT, bad]})
        self.assertEqual(len(requirements), 1)

    def test_a_control_key_outside_the_catalog_is_skipped_not_fatal(self) -> None:
        bad = {
            **VALID_REQUIREMENT,
            "mapped_control_key": "NOT_A_CONTROL",
            "source_locators": [DATABASE_LOCATOR],
        }
        requirements, _client = _extract({"requirements": [VALID_REQUIREMENT, bad]})
        self.assertEqual(len(requirements), 1)

    def test_evidence_outside_the_control_boundary_is_skipped_not_fatal(self) -> None:
        bad = {
            **VALID_REQUIREMENT,
            "required_evidence": ["S3.PUBLIC_ACCESS_BLOCK", "S3.INVENTED"],
            "source_locators": [DATABASE_LOCATOR],
        }
        requirements, _client = _extract({"requirements": [VALID_REQUIREMENT, bad]})
        self.assertEqual(len(requirements), 1)

    def test_a_resource_type_outside_the_control_boundary_is_skipped_not_fatal(self) -> None:
        bad = {
            **VALID_REQUIREMENT,
            "resource_types": ["AWS::RDS::DBInstance"],
            "source_locators": [DATABASE_LOCATOR],
        }
        requirements, _client = _extract({"requirements": [VALID_REQUIREMENT, bad]})
        self.assertEqual(len(requirements), 1)

    def test_an_overlong_field_is_skipped_not_fatal(self) -> None:
        bad = {
            **VALID_REQUIREMENT,
            "requirement": "x" * 5000,
            "source_locators": [DATABASE_LOCATOR],
        }
        requirements, _client = _extract({"requirements": [VALID_REQUIREMENT, bad]})
        self.assertEqual(len(requirements), 1)

    def test_too_many_requirements_in_one_chunk_are_refused(self) -> None:
        entries = [
            {**VALID_REQUIREMENT, "requirement": f"Requirement number {index}."}
            for index in range(MAX_REQUIREMENTS_PER_CHUNK + 1)
        ]
        with self.assertRaisesRegex(BedrockExtractionError, "every chunk failed"):
            _extract({"requirements": entries})

    def test_a_shape_the_classification_invariants_reject_is_refused(self) -> None:
        """분류가 말하는 것과 채워진 필드가 어긋나는 후보만 건너뛰고, 정상 후보는 남는다."""
        bad = {
            **VALID_REQUIREMENT,
            "classification": "UNSUPPORTED",
            "source_locators": [DATABASE_LOCATOR],
        }
        requirements, _client = _extract({"requirements": [VALID_REQUIREMENT, bad]})
        self.assertEqual(len(requirements), 1)


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
            {"requirements": [overlapping]},
            {"requirements": [overlapping]},
            {"requirements": []},
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
    """A skipped candidate is only visible in a log line, so that line must stay safe to keep.

    The rejection reason is what makes fail-soft debuggable. Contract invariant messages are rule
    text today, but a few embed the offending value with `!r`, and a locator carries a slug of the
    customer's own headings. The reason is logged through a redaction that keeps rule text and
    drops quoted values, so a future Contract message cannot turn this line into a leak.
    """

    def test_the_rejection_reason_is_logged_without_the_policy_sentence(self) -> None:
        bad = {**VALID_REQUIREMENT, "classification": "UNSUPPORTED"}
        with self.assertLogs("governance.authoring", level="WARNING") as logs:
            requirements, _client = _extract({"requirements": [bad, VALID_REQUIREMENT]})
        self.assertEqual(len(requirements), 1)
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


if __name__ == "__main__":
    unittest.main()
