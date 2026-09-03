"""Extraction, validation, rule building, and the pipeline that joins them.

이 파일이 지키는 규칙은 네 가지다.

1. **Evidence를 조용히 제거하지 않는다.** Catalog 밖 capability를 요구하면 그 항목을 빼고 Rule을
   만드는 것이 아니라 후보를 거절한다.
2. **AUTOMATABLE이 실패해도 MANUAL로 바꾸지 않는다.** 검증 실패로부터 승인 가능한 Rule을
   만들어내면 안 된다.
3. **severity와 `SourceReference`는 AI가 정하지 않는다.** 전자는 Catalog가, 후자는 서버가
   정규화 문서에서 만든다.
4. **가짜 Extractor는 정책 문장을 읽지 않는다.** 읽으면 통과 이유가 "파이프라인이 옳다"가
   아니라 "가짜가 그 문장을 알아봤다"가 된다.
"""

import unittest
from io import BytesIO

from apps.backend.policy.authoring import (
    APPLICABLE_PHASES,
    DuplicateRequirementError,
    ExtractorIdentity,
    FakePolicyCandidateExtractor,
    NormalizedArtifactReader,
    build_candidate,
    extract_policy_candidates,
)
from apps.backend.policy.control_catalog import (
    CONTROL_CATALOG_VERSION,
    GOVERNANCE_ASSESSMENT_RESOURCE_TYPE,
    MANUAL_CONTROL_KEY,
    MVP_CONTROL_CATALOG,
)
from packages.contracts import (
    AcceptedRequirement,
    AssessmentPhase,
    CandidateClassification,
    CandidateRejectionCode,
    ExtractedRequirement,
    RejectedRequirement,
    RuleEvaluationType,
    RuleSeverity,
)
from tests.authoring_fixtures import (
    UNIT_TEXTS,
    normalized_artifact_bytes,
    ready_document,
)

CUSTOMER = "cust-001"
BUCKET = "policy-artifacts"
DOCUMENT = ready_document()
STORAGE_LOCATOR = UNIT_TEXTS[0][0]
DATABASE_LOCATOR = UNIT_TEXTS[1][0]
GOVERNANCE_LOCATOR = UNIT_TEXTS[2][0]
FACILITIES_LOCATOR = UNIT_TEXTS[3][0]


class _Source:
    def get_object(self, **_kwargs: object) -> dict[str, object]:
        return {"Body": BytesIO(normalized_artifact_bytes())}


def artifact_reader() -> NormalizedArtifactReader:
    return NormalizedArtifactReader(reader=_Source(), bucket=BUCKET)  # type: ignore[arg-type]


def automatable(**overrides: object) -> ExtractedRequirement:
    fields: dict[str, object] = {
        "source_locators": (STORAGE_LOCATOR,),
        "requirement": "Object storage must not permit public access in any form.",
        "requirement_summary": "Buckets block public access",
        "classification": CandidateClassification.AUTOMATABLE,
        "mapping_reason": "The sentence names object storage and public access.",
        "mapped_control_key": "S3_BLOCK_PUBLIC_ACCESS",
        "resource_types": ("AWS::S3::Bucket",),
        "evaluation_type": RuleEvaluationType.AWS,
        "required_evidence": ("S3.PUBLIC_ACCESS_BLOCK",),
        "evaluation_rubric": "Fail when any block-public-access flag is false.",
    }
    fields.update(overrides)
    return ExtractedRequirement(**fields)  # type: ignore[arg-type]


def manual(**overrides: object) -> ExtractedRequirement:
    fields: dict[str, object] = {
        "source_locators": (GOVERNANCE_LOCATOR,),
        "requirement": "A named owner must review external processor agreements annually.",
        "requirement_summary": "Annual processor agreement review",
        "classification": CandidateClassification.MANUAL,
        "mapping_reason": "No tool in this product observes contract review.",
        "mapped_control_key": MANUAL_CONTROL_KEY,
        "evaluation_type": RuleEvaluationType.MANUAL,
    }
    fields.update(overrides)
    return ExtractedRequirement(**fields)  # type: ignore[arg-type]


def unsupported(**overrides: object) -> ExtractedRequirement:
    fields: dict[str, object] = {
        "source_locators": (FACILITIES_LOCATOR,),
        "requirement": "Physical access to facilities must be recorded and reviewed.",
        "requirement_summary": "Data centre entry logging",
        "classification": CandidateClassification.UNSUPPORTED,
        "mapping_reason": "Physical security is outside this product's evaluation boundary.",
    }
    fields.update(overrides)
    return ExtractedRequirement(**fields)  # type: ignore[arg-type]


def _build(requirement: ExtractedRequirement) -> AcceptedRequirement | RejectedRequirement:
    return build_candidate(requirement=requirement, document=DOCUMENT, catalog=MVP_CONTROL_CATALOG)


class RuleBuildTest(unittest.TestCase):
    def test_an_accepted_automatable_requirement_becomes_a_pinned_rule(self) -> None:
        outcome = _build(automatable())

        assert isinstance(outcome, AcceptedRequirement)
        rule = outcome.candidate.rule
        self.assertEqual(rule.control_key, "S3_BLOCK_PUBLIC_ACCESS")
        self.assertEqual(rule.control_catalog_version, CONTROL_CATALOG_VERSION)
        self.assertEqual(rule.evaluation_type, RuleEvaluationType.AWS)
        self.assertEqual(rule.version, DOCUMENT.source_version)
        self.assertTrue(rule.rule_id.startswith("CUST-S3_BLOCK_PUBLIC_ACCESS-"))

    def test_severity_comes_from_the_catalog_not_the_model(self) -> None:
        """AI는 `severity_guidance` 텍스트만 쓴다. 등급은 Catalog가 정한다."""
        outcome = _build(
            automatable(severity_guidance="This is only a minor style issue, treat it as LOW.")
        )

        assert isinstance(outcome, AcceptedRequirement)
        self.assertEqual(outcome.candidate.rule.severity, RuleSeverity.CRITICAL)
        self.assertEqual(outcome.proposed_severity, RuleSeverity.CRITICAL)

    def test_source_references_are_derived_from_the_document_not_the_model(self) -> None:
        """모델은 locator만 준다. digest는 서버가 정규화 문서에서 조회한다."""
        outcome = _build(automatable())

        assert isinstance(outcome, AcceptedRequirement)
        reference = outcome.candidate.rule.source_references[0]
        unit = DOCUMENT.unit(STORAGE_LOCATOR)
        assert unit is not None
        self.assertEqual(reference.content_sha256, unit.text_sha256)
        self.assertEqual(reference.source_id, DOCUMENT.source_id)
        self.assertEqual(reference.source_version, DOCUMENT.source_version)

    def test_effective_evidence_is_the_union_of_baseline_and_request(self) -> None:
        outcome = _build(
            automatable(
                evaluation_type=RuleEvaluationType.HYBRID,
                required_evidence=("S3.IAC_PUBLIC_ACCESS_BLOCK",),
            )
        )

        assert isinstance(outcome, AcceptedRequirement)
        rule = outcome.candidate.rule
        self.assertEqual(
            rule.required_evidence, ("S3.PUBLIC_ACCESS_BLOCK", "S3.IAC_PUBLIC_ACCESS_BLOCK")
        )
        # baseline optional과 겹치는 항목은 required가 이긴다 — 더 강한 요구를 낮추지 않는다.
        self.assertNotIn("S3.IAC_PUBLIC_ACCESS_BLOCK", rule.optional_evidence)

    def test_phases_follow_the_evaluation_type(self) -> None:
        aws = _build(automatable())
        iac = _build(
            automatable(
                mapped_control_key="S3_TLS_ONLY",
                evaluation_type=RuleEvaluationType.IAC,
                required_evidence=("S3.IAC_TLS_ONLY_POLICY",),
            )
        )

        assert isinstance(aws, AcceptedRequirement) and isinstance(iac, AcceptedRequirement)
        self.assertNotIn(AssessmentPhase.DEPLOYMENT_READINESS, aws.candidate.rule.applicable_phases)
        self.assertIn(AssessmentPhase.DEPLOYMENT_READINESS, iac.candidate.rule.applicable_phases)
        for evaluation_type, phases in APPLICABLE_PHASES.items():
            with self.subTest(evaluation_type=evaluation_type):
                self.assertIn(AssessmentPhase.INITIAL, phases)
                self.assertIn(AssessmentPhase.POST_DEPLOY_VERIFICATION, phases)

    def test_the_rule_id_is_stable_across_identical_extractions(self) -> None:
        """worker 재시도가 새 Rule ID를 만들면 같은 내용의 후보가 둘 생긴다."""
        first = _build(automatable())
        again = _build(automatable(requirement_summary="A different summary"))

        assert isinstance(first, AcceptedRequirement) and isinstance(again, AcceptedRequirement)
        self.assertEqual(first.candidate.rule.rule_id, again.candidate.rule.rule_id)

    def test_a_different_requirement_gets_a_different_rule_id(self) -> None:
        first = _build(automatable())
        other = _build(
            automatable(
                source_locators=(DATABASE_LOCATOR,),
                requirement="Managed database storage must be encrypted at rest.",
                mapped_control_key="RDS_ENCRYPTION_AT_REST",
                resource_types=("AWS::RDS::DBInstance",),
                required_evidence=("RDS.STORAGE_ENCRYPTED",),
            )
        )

        assert isinstance(first, AcceptedRequirement) and isinstance(other, AcceptedRequirement)
        self.assertNotEqual(first.candidate.rule.rule_id, other.candidate.rule.rule_id)


class ManualRuleBuildTest(unittest.TestCase):
    def test_a_manual_rule_binds_the_stable_governance_coordinate(self) -> None:
        """Assessment ID를 좌표로 쓰지 않는다.

        Initial과 Post-Deploy Verification이 같은 Repository에 대해 같은 좌표를 가져야
        비교가 성립한다.
        """
        outcome = _build(manual())

        assert isinstance(outcome, AcceptedRequirement)
        rule = outcome.candidate.rule
        self.assertEqual(rule.resource_types, (GOVERNANCE_ASSESSMENT_RESOURCE_TYPE,))
        self.assertEqual(rule.required_evidence, ())
        self.assertEqual(rule.optional_evidence, ())
        self.assertIsNone(rule.evaluation_rubric)

    def test_a_manual_requirement_mapped_to_an_automatable_control_is_refused(self) -> None:
        """사람이 검토해야 할 것이 자동 평가로 계획되면 안 된다."""
        outcome = _build(manual(mapped_control_key="S3_BLOCK_PUBLIC_ACCESS"))

        assert isinstance(outcome, RejectedRequirement)
        self.assertIn(
            CandidateRejectionCode.CLASSIFICATION_MAPPING_CONFLICT, outcome.rejection_codes
        )


class RejectionTest(unittest.TestCase):
    def test_an_invented_locator_is_refused(self) -> None:
        outcome = _build(automatable(source_locators=("heading/invented/item/9",)))

        assert isinstance(outcome, RejectedRequirement)
        self.assertIn(CandidateRejectionCode.UNKNOWN_LOCATOR, outcome.rejection_codes)

    def test_an_unknown_control_key_is_refused(self) -> None:
        outcome = _build(automatable(mapped_control_key="NOT_A_CONTROL"))

        assert isinstance(outcome, RejectedRequirement)
        self.assertEqual(outcome.rejection_codes, (CandidateRejectionCode.UNKNOWN_CONTROL_KEY,))

    def test_a_resource_type_the_control_does_not_support_is_refused(self) -> None:
        outcome = _build(automatable(resource_types=("AWS::RDS::DBInstance",)))

        assert isinstance(outcome, RejectedRequirement)
        self.assertIn(CandidateRejectionCode.UNSUPPORTED_RESOURCE_TYPE, outcome.rejection_codes)

    def test_an_evaluation_type_the_control_does_not_support_is_refused(self) -> None:
        """`S3_TLS_ONLY`는 IaC 전용이다. AWS로 요청하면 실행 경로가 없다."""
        outcome = _build(
            automatable(
                mapped_control_key="S3_TLS_ONLY",
                evaluation_type=RuleEvaluationType.AWS,
                required_evidence=("S3.IAC_TLS_ONLY_POLICY",),
            )
        )

        assert isinstance(outcome, RejectedRequirement)
        self.assertIn(CandidateRejectionCode.UNSUPPORTED_EVALUATION_TYPE, outcome.rejection_codes)

    def test_evidence_outside_the_catalog_is_refused_not_dropped(self) -> None:
        """조용히 제거하면 승인된 Rule과 AI가 제안한 Rule이 달라지고 그 차이가 남지 않는다."""
        outcome = _build(
            automatable(required_evidence=("S3.PUBLIC_ACCESS_BLOCK", "S3.INVENTED_CAPABILITY"))
        )

        assert isinstance(outcome, RejectedRequirement)
        self.assertIn(
            CandidateRejectionCode.EVIDENCE_CAPABILITY_NOT_AVAILABLE, outcome.rejection_codes
        )

    def test_a_known_unsupported_control_cannot_produce_a_rule(self) -> None:
        outcome = _build(
            automatable(
                mapped_control_key="EC2_SNAPSHOT_NOT_PUBLIC",
                resource_types=("AWS::EC2::Snapshot",),
                required_evidence=("EC2.SNAPSHOT_PERMISSIONS",),
            )
        )

        assert isinstance(outcome, RejectedRequirement)
        self.assertIn(
            CandidateRejectionCode.CLASSIFICATION_MAPPING_CONFLICT, outcome.rejection_codes
        )

    def test_a_rejected_automatable_candidate_is_not_turned_into_a_manual_rule(self) -> None:
        outcome = _build(automatable(mapped_control_key="NOT_A_CONTROL"))

        assert isinstance(outcome, RejectedRequirement)
        self.assertIs(outcome.requirement.classification, CandidateClassification.AUTOMATABLE)


class FakeExtractorTest(unittest.TestCase):
    def test_the_fake_returns_only_what_it_was_given(self) -> None:
        """정책 문장을 읽고 분기하는 가짜는 파이프라인을 검증하지 못한다."""
        injected = (automatable(), manual())
        extractor = FakePolicyCandidateExtractor(injected)

        result = extractor.extract(
            document=DOCUMENT,
            units=artifact_reader().read(customer_id=CUSTOMER, document=DOCUMENT),
            catalog=MVP_CONTROL_CATALOG,
        )

        self.assertEqual(result, injected)
        self.assertEqual(
            extractor.calls,
            [(DOCUMENT.source_id, DOCUMENT.source_version, 4, CONTROL_CATALOG_VERSION)],
        )

    def test_the_extractor_identity_composes_full_provenance(self) -> None:
        identity = ExtractorIdentity(
            extractor_id="fake",
            extractor_version="1.0.0",
            model_id="fake",
            model_version="1",
            prompt_version="policy-authoring/fake",
        )

        provenance = identity.provenance(
            catalog=MVP_CONTROL_CATALOG,
            authoring_run_id="run-1",
            requested_at="2026-09-03T00:00:00+00:00",
        )

        self.assertEqual(provenance.control_catalog_version, CONTROL_CATALOG_VERSION)
        self.assertEqual(provenance.candidate_schema_version, "policy-candidate/2026-09-03")


class PipelineTest(unittest.TestCase):
    def _run(self, *requirements: ExtractedRequirement):
        return extract_policy_candidates(
            customer_id=CUSTOMER,
            document=DOCUMENT,
            artifact_reader=artifact_reader(),
            extractor=FakePolicyCandidateExtractor(requirements),
            catalog=MVP_CONTROL_CATALOG,
            authoring_run_id="run-1",
            requested_at="2026-09-03T00:00:00+00:00",
        )

    def test_a_run_sorts_requirements_into_the_four_outcomes(self) -> None:
        result = self._run(
            automatable(),
            manual(),
            unsupported(),
            automatable(
                source_locators=(DATABASE_LOCATOR,),
                requirement="Managed database storage must be encrypted.",
                mapped_control_key="NOT_A_CONTROL",
            ),
        )

        self.assertEqual(
            result.counts, {"accepted": 1, "manual": 1, "unsupported": 1, "rejected": 1}
        )

    def test_only_automatable_and_manual_reach_the_approvable_candidate_set(self) -> None:
        result = self._run(automatable(), manual(), unsupported())

        control_keys = sorted(candidate.rule.control_key or "" for candidate in result.candidates)
        self.assertEqual(
            control_keys, ["ORGANIZATIONAL_CONTROL_MANUAL_REVIEW", "S3_BLOCK_PUBLIC_ACCESS"]
        )

    def test_the_result_carries_the_run_provenance(self) -> None:
        result = self._run(automatable())

        self.assertEqual(result.provenance.authoring_run_id, "run-1")
        self.assertEqual(result.provenance.control_catalog_version, CONTROL_CATALOG_VERSION)

    def test_a_repeated_requirement_fails_the_whole_run(self) -> None:
        """조용히 하나를 버리면 어느 쪽을 버렸는지 아무 데도 남지 않는다."""
        with self.assertRaises(DuplicateRequirementError):
            self._run(automatable(), automatable(requirement_summary="Restated"))

    def test_the_pipeline_persists_nothing(self) -> None:
        """같은 입력에 같은 출력을 낸다. 저장 계층이 재시도를 식별하는 근거다."""
        first = self._run(automatable(), manual())
        second = self._run(automatable(), manual())

        self.assertEqual(first.to_dict(), second.to_dict())


if __name__ == "__main__":
    unittest.main()
