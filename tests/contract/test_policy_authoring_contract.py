"""Contract tests for the policy authoring boundary.

지키는 것은 두 가지다.

1. **분류별 불변식.** AUTOMATABLE/MANUAL/UNSUPPORTED는 서로 다른 모양을 강제받는다. 분류가
   말하는 것과 실제로 채워진 필드가 어긋나면 승인 화면과 Runtime이 다른 것을 본다.
2. **AWS와 IaC evidence capability의 비대칭.** AWS는 attribute-level authoritative binding을
   갖고, IaC는 파일 단위 hint만 갖는다. 이 비대칭을 계약에서 무너뜨리면 HCL을 파싱하지 않은
   채로 attribute-level 자동 판정을 하게 된다.
"""

import unittest

from packages.contracts import (
    AcceptedRequirement,
    AssessmentPhase,
    AuthoringProvenance,
    CandidateClassification,
    CandidateRejectionCode,
    ControlAutomationSupport,
    EvaluationPerspective,
    EvaluationStatus,
    EvidenceCapabilityBinding,
    ExtractedRequirement,
    GovernanceControl,
    GovernanceControlCatalog,
    PolicyAuthoringResult,
    PolicyRule,
    RejectedRequirement,
    RuleCandidate,
    RuleEvaluationType,
    RuleSeverity,
    SourceReference,
)
from packages.contracts.policy_authoring import FORBIDDEN_EXTRACTION_FIELDS
from tests.authoring_fixtures import ready_document

AWS_CAPABILITY = EvidenceCapabilityBinding(
    capability_key="S3.PUBLIC_ACCESS_BLOCK",
    perspective=EvaluationPerspective.AWS_ACTUAL,
    resource_type="AWS::S3::Bucket",
    document_paths=("attributes.public_access_block.BlockPublicAcls",),
)

IAC_CAPABILITY = EvidenceCapabilityBinding(
    capability_key="S3.IAC_PUBLIC_ACCESS_BLOCK",
    perspective=EvaluationPerspective.IAC,
    resource_type="AWS::S3::Bucket",
    terraform_resource_types=("aws_s3_bucket_public_access_block",),
    terraform_attribute_names=("block_public_acls",),
)


def _automatable_control(**overrides: object) -> GovernanceControl:
    fields: dict[str, object] = {
        "control_key": "S3_BLOCK_PUBLIC_ACCESS",
        "title": "Block public access on S3 buckets",
        "description": "Buckets must block every form of public access.",
        "automation_support": ControlAutomationSupport.AVAILABLE,
        "supported_resource_types": ("AWS::S3::Bucket",),
        "supported_evaluation_types": (RuleEvaluationType.HYBRID,),
        "available_evidence_capabilities": (AWS_CAPABILITY, IAC_CAPABILITY),
        "allowed_tool_bindings": ("aws:s3:GetPublicAccessBlock",),
        "baseline_required_evidence": ("S3.PUBLIC_ACCESS_BLOCK",),
        "severity_guidance": "Public buckets expose customer data directly.",
        "default_severity": RuleSeverity.HIGH,
    }
    fields.update(overrides)
    return GovernanceControl(**fields)  # type: ignore[arg-type]


class EvidenceCapabilityAsymmetryTest(unittest.TestCase):
    def test_an_aws_capability_must_bind_real_document_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "must declare document_paths"):
            EvidenceCapabilityBinding(
                capability_key="S3.PUBLIC_ACCESS_BLOCK",
                perspective=EvaluationPerspective.AWS_ACTUAL,
                resource_type="AWS::S3::Bucket",
            )

    def test_an_iac_capability_must_not_claim_attribute_level_paths(self) -> None:
        """IaC evaluator는 raw HCL을 받고 Evidence locator는 파일 경로다.

        `document_paths`를 허용하면 존재하지 않는 projection을 근거로 pre-flight hard gate를
        걸게 된다. IaC hint는 prompt 경계일 뿐 증거가 아니다.
        """
        with self.assertRaisesRegex(ValueError, "must not declare document_paths"):
            EvidenceCapabilityBinding(
                capability_key="S3.IAC_PUBLIC_ACCESS_BLOCK",
                perspective=EvaluationPerspective.IAC,
                resource_type="AWS::S3::Bucket",
                document_paths=("attributes.public_access_block.BlockPublicAcls",),
            )

    def test_an_aws_capability_must_not_carry_terraform_hints(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not declare Terraform hints"):
            EvidenceCapabilityBinding(
                capability_key="S3.PUBLIC_ACCESS_BLOCK",
                perspective=EvaluationPerspective.AWS_ACTUAL,
                resource_type="AWS::S3::Bucket",
                document_paths=("attributes.public_access_block.BlockPublicAcls",),
                terraform_resource_types=("aws_s3_bucket_public_access_block",),
            )

    def test_only_the_aws_binding_is_authoritative(self) -> None:
        self.assertTrue(AWS_CAPABILITY.is_authoritative)
        self.assertFalse(IAC_CAPABILITY.is_authoritative)

    def test_drift_and_manual_are_not_evidence_perspectives(self) -> None:
        for perspective in (EvaluationPerspective.DRIFT,):
            with self.subTest(perspective=perspective):
                with self.assertRaisesRegex(ValueError, "must bind to IAC or AWS_ACTUAL"):
                    EvidenceCapabilityBinding(
                        capability_key="X",
                        perspective=perspective,
                        resource_type="AWS::S3::Bucket",
                    )


class GovernanceControlShapeTest(unittest.TestCase):
    def test_an_available_control_round_trips(self) -> None:
        control = _automatable_control()

        self.assertEqual(
            control.capability_keys, ("S3.PUBLIC_ACCESS_BLOCK", "S3.IAC_PUBLIC_ACCESS_BLOCK")
        )
        self.assertTrue(control.supports(RuleEvaluationType.HYBRID))
        self.assertFalse(control.supports(RuleEvaluationType.MANUAL))
        self.assertEqual(control.to_dict()["default_severity"], "HIGH")

    def test_baseline_evidence_must_be_a_declared_capability(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be declared capabilities"):
            _automatable_control(baseline_required_evidence=("S3.NOT_A_CAPABILITY",))

    def test_a_capability_must_bind_a_supported_resource_type(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported resource type"):
            _automatable_control(supported_resource_types=("AWS::RDS::DBInstance",))

    def test_an_aws_capability_needs_an_aws_evaluation_type(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not support an AWS evaluation type"):
            _automatable_control(
                supported_evaluation_types=(RuleEvaluationType.IAC,),
                available_evidence_capabilities=(AWS_CAPABILITY,),
                baseline_required_evidence=("S3.PUBLIC_ACCESS_BLOCK",),
            )

    def test_a_known_unsupported_control_exposes_no_execution_path(self) -> None:
        """Catalog에 존재하는 것과 지금 자동 평가할 수 있는 것은 같은 의미가 아니다."""
        control = GovernanceControl(
            control_key="EC2_SNAPSHOT_NOT_PUBLIC",
            title="EBS snapshots are not public",
            description="Snapshot sharing must not be public.",
            automation_support=ControlAutomationSupport.KNOWN_UNSUPPORTED,
            supported_resource_types=("AWS::EC2::Snapshot",),
            severity_guidance="A public snapshot leaks whole volumes.",
            default_severity=RuleSeverity.CRITICAL,
        )

        self.assertEqual(control.supported_evaluation_types, ())
        self.assertEqual(control.available_evidence_capabilities, ())

        with self.assertRaisesRegex(ValueError, "must not declare supported evaluation types"):
            GovernanceControl(
                control_key="EC2_SNAPSHOT_NOT_PUBLIC",
                title="EBS snapshots are not public",
                description="Snapshot sharing must not be public.",
                automation_support=ControlAutomationSupport.KNOWN_UNSUPPORTED,
                supported_resource_types=("AWS::EC2::Snapshot",),
                supported_evaluation_types=(RuleEvaluationType.AWS,),
                severity_guidance="A public snapshot leaks whole volumes.",
                default_severity=RuleSeverity.CRITICAL,
            )

    def test_a_manual_control_declares_only_the_manual_evaluation_type(self) -> None:
        control = GovernanceControl(
            control_key="ORGANIZATIONAL_CONTROL_MANUAL_REVIEW",
            title="Organizational control requiring human review",
            description="A requirement no tool in this product can observe.",
            automation_support=ControlAutomationSupport.MANUAL,
            supported_evaluation_types=(RuleEvaluationType.MANUAL,),
            severity_guidance="Severity follows the organizational policy owner.",
            default_severity=RuleSeverity.MEDIUM,
        )

        self.assertEqual(control.supported_evaluation_types, (RuleEvaluationType.MANUAL,))

        with self.assertRaisesRegex(ValueError, "must not declare evidence or tool bindings"):
            GovernanceControl(
                control_key="ORGANIZATIONAL_CONTROL_MANUAL_REVIEW",
                title="Organizational control requiring human review",
                description="A requirement no tool in this product can observe.",
                automation_support=ControlAutomationSupport.MANUAL,
                supported_resource_types=("AWS::S3::Bucket",),
                supported_evaluation_types=(RuleEvaluationType.MANUAL,),
                allowed_tool_bindings=("aws:s3:GetPublicAccessBlock",),
                severity_guidance="Severity follows the organizational policy owner.",
                default_severity=RuleSeverity.MEDIUM,
            )


class CatalogTest(unittest.TestCase):
    def test_a_catalog_rejects_duplicate_control_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate control"):
            GovernanceControlCatalog(
                version="governance-control-catalog/2026-09-03",
                controls=(_automatable_control(), _automatable_control()),
            )

    def test_a_catalog_separates_automatable_from_manual_controls(self) -> None:
        manual = GovernanceControl(
            control_key="ORGANIZATIONAL_CONTROL_MANUAL_REVIEW",
            title="Organizational control requiring human review",
            description="A requirement no tool in this product can observe.",
            automation_support=ControlAutomationSupport.MANUAL,
            supported_evaluation_types=(RuleEvaluationType.MANUAL,),
            severity_guidance="Severity follows the organizational policy owner.",
            default_severity=RuleSeverity.MEDIUM,
        )
        catalog = GovernanceControlCatalog(
            version="governance-control-catalog/2026-09-03",
            controls=(_automatable_control(), manual),
        )

        self.assertEqual(
            [control.control_key for control in catalog.automatable_controls()],
            ["S3_BLOCK_PUBLIC_ACCESS"],
        )
        self.assertEqual(
            [control.control_key for control in catalog.manual_controls()],
            ["ORGANIZATIONAL_CONTROL_MANUAL_REVIEW"],
        )
        self.assertIsNone(catalog.control("NOT_A_CONTROL"))


def _automatable_requirement(**overrides: object) -> ExtractedRequirement:
    fields: dict[str, object] = {
        "source_locators": ("section/3.1",),
        "requirement": "Every S3 bucket must block public access.",
        "requirement_summary": "S3 buckets block public access",
        "classification": CandidateClassification.AUTOMATABLE,
        "mapping_reason": "The sentence names S3 buckets and public access directly.",
        "mapped_control_key": "S3_BLOCK_PUBLIC_ACCESS",
        "resource_types": ("AWS::S3::Bucket",),
        "evaluation_type": RuleEvaluationType.AWS,
        "required_evidence": ("S3.PUBLIC_ACCESS_BLOCK",),
        "evaluation_rubric": "Fail when any block-public-access flag is false.",
    }
    fields.update(overrides)
    return ExtractedRequirement(**fields)  # type: ignore[arg-type]


class ExtractedRequirementClassificationTest(unittest.TestCase):
    def test_an_automatable_requirement_round_trips(self) -> None:
        requirement = _automatable_requirement()

        payload = requirement.to_dict()

        self.assertEqual(payload["classification"], "AUTOMATABLE")
        self.assertEqual(payload["evaluation_type"], "AWS")

    def test_an_automatable_requirement_needs_a_control_type_evidence_and_rubric(self) -> None:
        with self.assertRaisesRegex(ValueError, "must map to a control"):
            _automatable_requirement(mapped_control_key=None)
        with self.assertRaisesRegex(ValueError, "IAC, AWS, or HYBRID"):
            _automatable_requirement(evaluation_type=RuleEvaluationType.MANUAL)
        with self.assertRaisesRegex(ValueError, "must declare required evidence"):
            _automatable_requirement(required_evidence=())
        with self.assertRaisesRegex(ValueError, "must declare an evaluation rubric"):
            _automatable_requirement(evaluation_rubric=None)

    def test_a_manual_requirement_carries_no_evidence_or_resource_type(self) -> None:
        """MANUAL Requirement가 실제 resource type을 지목하면 자동 평가처럼 계획된다.

        MANUAL Rule의 대상은 Repository 단위의 안정된 governance 좌표여야 한다. 그 좌표는
        서버가 붙인다.
        """
        requirement = ExtractedRequirement(
            source_locators=("section/9.2",),
            requirement="The security officer reviews third-party processor agreements yearly.",
            requirement_summary="Annual processor agreement review",
            classification=CandidateClassification.MANUAL,
            mapping_reason="No tool in this product observes contract review.",
            mapped_control_key="ORGANIZATIONAL_CONTROL_MANUAL_REVIEW",
            evaluation_type=RuleEvaluationType.MANUAL,
        )

        self.assertEqual(requirement.resource_types, ())

        with self.assertRaisesRegex(ValueError, "must not declare evidence"):
            ExtractedRequirement(
                source_locators=("section/9.2",),
                requirement="The security officer reviews agreements yearly.",
                requirement_summary="Annual processor agreement review",
                classification=CandidateClassification.MANUAL,
                mapping_reason="No tool observes contract review.",
                mapped_control_key="ORGANIZATIONAL_CONTROL_MANUAL_REVIEW",
                evaluation_type=RuleEvaluationType.MANUAL,
                required_evidence=("S3.PUBLIC_ACCESS_BLOCK",),
            )

    def test_an_unsupported_requirement_carries_no_rule_semantics(self) -> None:
        requirement = ExtractedRequirement(
            source_locators=("section/12.4",),
            requirement="Physical access to the data centre is logged.",
            requirement_summary="Data centre access logging",
            classification=CandidateClassification.UNSUPPORTED,
            mapping_reason="Physical security is outside this product's evaluation boundary.",
        )

        self.assertIsNone(requirement.mapped_control_key)

        with self.assertRaisesRegex(ValueError, "must not carry rule semantics"):
            ExtractedRequirement(
                source_locators=("section/12.4",),
                requirement="Physical access to the data centre is logged.",
                requirement_summary="Data centre access logging",
                classification=CandidateClassification.UNSUPPORTED,
                mapping_reason="Physical security is outside the boundary.",
                mapped_control_key="S3_BLOCK_PUBLIC_ACCESS",
            )

    def test_a_requirement_always_cites_at_least_one_locator(self) -> None:
        with self.assertRaisesRegex(ValueError, "source_locators must not be empty"):
            _automatable_requirement(source_locators=())

    def test_the_digest_ignores_whitespace_but_not_meaning(self) -> None:
        base = _automatable_requirement()
        respaced = _automatable_requirement(
            requirement="Every  S3 bucket\nmust block public access."
        )
        different = _automatable_requirement(requirement="Every RDS instance must be private.")

        self.assertEqual(base.digest, respaced.digest)
        self.assertNotEqual(base.digest, different.digest)


class ForbiddenExtractionFieldTest(unittest.TestCase):
    def test_the_extractor_output_defines_no_evaluation_outcome_field(self) -> None:
        """LLM이 판정·심각도·점수를 쓸 자리를 schema에 만들지 않는다.

        prompt로 금지하는 것과 schema에 자리가 없는 것은 다르다. 자리가 있으면 언젠가 채워진다.
        """
        payload = _automatable_requirement().to_dict()

        for forbidden in FORBIDDEN_EXTRACTION_FIELDS:
            with self.subTest(field=forbidden):
                self.assertNotIn(forbidden, payload)
                self.assertNotIn(forbidden, ExtractedRequirement.__dataclass_fields__)

    def test_classification_is_not_an_evaluation_status(self) -> None:
        """두 Enum 사이에 alias나 변환 함수를 만들지 않는다. 서로 다른 질문에 답한다."""
        classification_values = {value.value for value in CandidateClassification}
        status_values = {value.value for value in EvaluationStatus}

        self.assertEqual(classification_values & status_values, {"MANUAL"} & status_values)
        self.assertNotIn("AUTOMATABLE", status_values)
        self.assertNotIn("UNSUPPORTED", status_values)


PROVENANCE = AuthoringProvenance(
    extractor_id="fake-policy-candidate-extractor",
    extractor_version="1.0.0",
    model_id="fake",
    model_version="1",
    prompt_version="policy-authoring/2026-09-03",
    candidate_schema_version="policy-candidate/2026-09-03",
    control_catalog_version="governance-control-catalog/2026-09-03",
    authoring_run_id="run-1",
    requested_at="2026-09-03T00:00:00+00:00",
)


class AuthoringProvenanceTest(unittest.TestCase):
    def test_identity_excludes_the_run_id_so_retries_are_the_same_extraction(self) -> None:
        retry = AuthoringProvenance(
            extractor_id=PROVENANCE.extractor_id,
            extractor_version=PROVENANCE.extractor_version,
            model_id=PROVENANCE.model_id,
            model_version=PROVENANCE.model_version,
            prompt_version=PROVENANCE.prompt_version,
            candidate_schema_version=PROVENANCE.candidate_schema_version,
            control_catalog_version=PROVENANCE.control_catalog_version,
            authoring_run_id="run-2",
            requested_at=PROVENANCE.requested_at,
        )

        self.assertEqual(retry.extraction_identity, PROVENANCE.extraction_identity)

    def test_a_different_catalog_version_is_a_different_extraction(self) -> None:
        other = AuthoringProvenance(
            extractor_id=PROVENANCE.extractor_id,
            extractor_version=PROVENANCE.extractor_version,
            model_id=PROVENANCE.model_id,
            model_version=PROVENANCE.model_version,
            prompt_version=PROVENANCE.prompt_version,
            candidate_schema_version=PROVENANCE.candidate_schema_version,
            control_catalog_version="governance-control-catalog/2027-01-01",
            authoring_run_id=PROVENANCE.authoring_run_id,
            requested_at=PROVENANCE.requested_at,
        )

        self.assertNotEqual(other.extraction_identity, PROVENANCE.extraction_identity)

    def test_requested_at_must_carry_an_offset(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit UTC offset"):
            AuthoringProvenance(
                extractor_id=PROVENANCE.extractor_id,
                extractor_version=PROVENANCE.extractor_version,
                model_id=PROVENANCE.model_id,
                model_version=PROVENANCE.model_version,
                prompt_version=PROVENANCE.prompt_version,
                candidate_schema_version=PROVENANCE.candidate_schema_version,
                control_catalog_version=PROVENANCE.control_catalog_version,
                authoring_run_id=PROVENANCE.authoring_run_id,
                requested_at="2026-09-03T00:00:00",
            )


class AuthoringResultTest(unittest.TestCase):
    def setUp(self) -> None:
        self.document = ready_document()
        locator = self.document.units[0].locator
        self.reference = SourceReference(
            source_id=self.document.source_id,
            source_version=self.document.source_version,
            locator=locator,
            content_sha256=self.document.units[0].text_sha256,
        )
        self.requirement = _automatable_requirement(source_locators=(locator,))
        self.rule = PolicyRule(
            rule_id="CUST-S3_BLOCK_PUBLIC_ACCESS-0123456789ab",
            version=self.document.source_version,
            title=self.requirement.requirement_summary,
            severity=RuleSeverity.HIGH,
            applicable_phases=(AssessmentPhase.INITIAL,),
            resource_types=("AWS::S3::Bucket",),
            source_references=(self.reference,),
            control_key="S3_BLOCK_PUBLIC_ACCESS",
            control_catalog_version="governance-control-catalog/2026-09-03",
            evaluation_type=RuleEvaluationType.AWS,
            evaluation_rubric="Fail when any block-public-access flag is false.",
            required_evidence=("S3.PUBLIC_ACCESS_BLOCK",),
        )
        self.accepted = AcceptedRequirement(
            requirement=self.requirement, candidate=RuleCandidate(rule=self.rule)
        )

    def test_an_accepted_requirement_keeps_the_requirement_beside_the_rule(self) -> None:
        self.assertEqual(self.accepted.proposed_severity, RuleSeverity.HIGH)
        self.assertEqual(
            self.accepted.requirement.mapping_reason,
            "The sentence names S3 buckets and public access directly.",
        )

    def test_an_accepted_requirement_must_agree_with_its_rule(self) -> None:
        mismatched = PolicyRule(
            rule_id="CUST-S3_TLS_ONLY-0123456789ab",
            version=self.document.source_version,
            title="TLS only",
            severity=RuleSeverity.HIGH,
            applicable_phases=(AssessmentPhase.INITIAL,),
            resource_types=("AWS::S3::Bucket",),
            source_references=(self.reference,),
            control_key="S3_TLS_ONLY",
            control_catalog_version="governance-control-catalog/2026-09-03",
            evaluation_type=RuleEvaluationType.AWS,
            evaluation_rubric="Fail without a TLS-only bucket policy.",
            required_evidence=("S3.PUBLIC_ACCESS_BLOCK",),
        )

        with self.assertRaisesRegex(ValueError, "control key must match"):
            AcceptedRequirement(
                requirement=self.requirement, candidate=RuleCandidate(rule=mismatched)
            )

    def test_only_automatable_and_manual_requirements_become_approvable_rules(self) -> None:
        unsupported = ExtractedRequirement(
            source_locators=(self.document.units[0].locator,),
            requirement="Physical access to the data centre is logged.",
            requirement_summary="Data centre access logging",
            classification=CandidateClassification.UNSUPPORTED,
            mapping_reason="Physical security is outside the evaluation boundary.",
        )
        rejected = RejectedRequirement(
            requirement=_automatable_requirement(
                source_locators=(self.document.units[0].locator,),
                mapped_control_key="NOT_A_CONTROL",
            ),
            rejection_codes=(CandidateRejectionCode.UNKNOWN_CONTROL_KEY,),
        )

        result = PolicyAuthoringResult(
            document=self.document,
            accepted=(self.accepted,),
            unsupported=(unsupported,),
            rejected=(rejected,),
            provenance=PROVENANCE,
        )

        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(
            result.counts, {"accepted": 1, "manual": 0, "unsupported": 1, "rejected": 1}
        )

    def test_an_unsupported_requirement_cannot_be_filed_as_accepted(self) -> None:
        unsupported = ExtractedRequirement(
            source_locators=(self.document.units[0].locator,),
            requirement="Physical access to the data centre is logged.",
            requirement_summary="Data centre access logging",
            classification=CandidateClassification.UNSUPPORTED,
            mapping_reason="Physical security is outside the evaluation boundary.",
        )

        with self.assertRaisesRegex(TypeError, "accepted items must be AcceptedRequirement"):
            PolicyAuthoringResult(
                document=self.document,
                accepted=(unsupported,),  # type: ignore[arg-type]
                provenance=PROVENANCE,
            )

        manual_entry = AcceptedRequirement(
            requirement=self.requirement, candidate=RuleCandidate(rule=self.rule)
        )
        with self.assertRaisesRegex(ValueError, "manual must contain only MANUAL"):
            PolicyAuthoringResult(
                document=self.document,
                manual=(manual_entry,),
                provenance=PROVENANCE,
            )

    def test_a_result_must_not_duplicate_a_rule_version(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not duplicate a rule version"):
            PolicyAuthoringResult(
                document=self.document,
                accepted=(self.accepted, self.accepted),
                provenance=PROVENANCE,
            )


if __name__ == "__main__":
    unittest.main()
