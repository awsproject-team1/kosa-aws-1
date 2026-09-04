"""Facts are decided by code; judgments are left to the model.

이 파일이 고정하는 것은 넷이다.

1. **부분 준수가 통과하지 않는다.** 라이브 측정에서 모델이 틀린 3건은 전부 위반을 PASS로 본
   false negative였고 부분 준수에 집중됐다. 준법 제품에서 그것은 "준비됐다"고 말해 놓고 준비되지
   않은 상태다.
2. **부분 충족은 점수가 아니라 관측 상세다.** 4개 중 3개는 `observed_satisfied=3 / observed_total=4`
   이지 score 75가 아니다. 비율의 분모는 리소스 개수라서, 점수로 쓰면 미암호화 볼륨 하나라는
   같은 위험이 볼륨을 더 붙일수록 준비도를 올린다. score는 status가 정한다(FAIL 0, PASS 100).
3. **술어가 없으면 코드가 판정하지 않는다.** 해석이 필요한 통제까지 코드가 답하면 그것은 근거
   없는 확신이다.
4. **식별자가 통제를 통과시키지 않는다.** `ALL_TRUE`는 참으로 평가되는 값이 아니라 `True`만 받는다.
"""

import unittest

from apps.backend.assessment.deterministic import (
    DeterministicEvaluationError,
    decidable_bindings,
    decide,
    result_from_verdict,
)
from apps.backend.policy.control_catalog import MVP_CONTROL_CATALOG as CATALOG
from packages.contracts import (
    AssessmentPhase,
    EvaluationPerspective,
    EvaluationStatus,
    EvidenceCapabilityBinding,
    EvidenceExpectation,
    ModelProfile,
    ModelProfileRole,
    PolicyRule,
    RuleEvaluationType,
    RuleSeverity,
    SourceReference,
)

S3 = "AWS::S3::Bucket"
ALB = "AWS::ElasticLoadBalancingV2::LoadBalancer"
EC2 = "AWS::EC2::Instance"

PROFILE = ModelProfile(
    model_profile_id="assessment-nova-lite-m1-v2",
    role=ModelProfileRole.ASSESSMENT,
    region="us-east-1",
    model_id="amazon.nova-lite-v1:0",
    prompt_version="assessment/1",
    rubric_version="m1-v2",
    golden_dataset_version="m1-golden/1",
)


def _rule(control_key: str, capability: str, resource_type: str, **overrides: object) -> PolicyRule:
    fields: dict[str, object] = {
        "rule_id": "CUST-RULE-1",
        "version": "ver-1",
        "title": "Rule",
        "severity": RuleSeverity.HIGH,
        "applicable_phases": (AssessmentPhase.INITIAL,),
        "resource_types": (resource_type,),
        "source_references": (
            SourceReference(
                source_id="src-1", source_version="ver-1", locator="item/1", content_sha256="abc"
            ),
        ),
        "control_key": control_key,
        "control_catalog_version": CATALOG.version,
        "evaluation_type": RuleEvaluationType.AWS,
        "required_evidence": (capability,),
        "evaluation_rubric": "Fail when the criterion is not met.",
    }
    fields.update(overrides)
    return PolicyRule(**fields)  # type: ignore[arg-type]


def _block(**flags: bool) -> dict[str, object]:
    return {"attributes": {"public_access_block": flags}}


def _verdict(control_key: str, capability: str, resource_type: str, document: dict[str, object]):
    rule = _rule(control_key, capability, resource_type)
    bindings = decidable_bindings(CATALOG, rule, resource_type=resource_type)
    return decide(bindings, document)


def _legacy(rule_id: str) -> PolicyRule:
    return PolicyRule(
        rule_id=rule_id,
        version="2026-08-31",
        title="Legacy",
        severity=RuleSeverity.HIGH,
        applicable_phases=(AssessmentPhase.INITIAL,),
        resource_types=(S3,),
        source_references=(
            SourceReference(source_id="s", source_version="v", locator="l", content_sha256="c"),
        ),
    )


class PartialComplianceTest(unittest.TestCase):
    """모델이 틀렸던 그 입력들이다."""

    def test_three_of_four_block_flags_is_a_violation_with_the_detail_kept(self) -> None:
        verdict = _verdict(
            "S3_BLOCK_PUBLIC_ACCESS",
            "S3.PUBLIC_ACCESS_BLOCK",
            S3,
            _block(
                BlockPublicAcls=True,
                IgnorePublicAcls=True,
                BlockPublicPolicy=True,
                RestrictPublicBuckets=False,
            ),
        )

        self.assertIs(verdict.status, EvaluationStatus.FAIL)
        self.assertEqual(verdict.score, 0.0)
        self.assertEqual((verdict.observed_satisfied, verdict.observed_total), (3, 4))
        self.assertIn("RestrictPublicBuckets", verdict.rationale)

    def test_all_four_block_flags_pass_with_a_full_score(self) -> None:
        verdict = _verdict(
            "S3_BLOCK_PUBLIC_ACCESS",
            "S3.PUBLIC_ACCESS_BLOCK",
            S3,
            _block(
                BlockPublicAcls=True,
                IgnorePublicAcls=True,
                BlockPublicPolicy=True,
                RestrictPublicBuckets=True,
            ),
        )

        self.assertIs(verdict.status, EvaluationStatus.PASS)
        self.assertEqual(verdict.score, 100.0)

    def test_the_observation_detail_counts_how_much_of_the_criterion_is_met(self) -> None:
        """비율은 관측 상세로 남고, score는 status가 정한다 — 4/4만 PASS·100이다."""
        observed = []
        scores = []
        for blocked in range(5):
            flags = dict.fromkeys(
                (
                    "BlockPublicAcls",
                    "IgnorePublicAcls",
                    "BlockPublicPolicy",
                    "RestrictPublicBuckets",
                ),
                False,
            )
            for name in list(flags)[:blocked]:
                flags[name] = True
            verdict = _verdict(
                "S3_BLOCK_PUBLIC_ACCESS", "S3.PUBLIC_ACCESS_BLOCK", S3, _block(**flags)
            )
            observed.append((verdict.observed_satisfied, verdict.observed_total))
            scores.append(verdict.score)

        self.assertEqual(observed, [(0, 4), (1, 4), (2, 4), (3, 4), (4, 4)])
        self.assertEqual(scores, [0.0, 0.0, 0.0, 0.0, 100.0])

    def test_an_http_listener_beside_https_is_a_violation(self) -> None:
        verdict = _verdict(
            "ALB_HTTPS_ONLY",
            "ALB.LISTENER_PROTOCOL",
            ALB,
            {
                "attributes": {
                    "listeners": [
                        {"ListenerArn": "arn:a", "Protocol": "HTTPS"},
                        {"ListenerArn": "arn:b", "Protocol": "HTTP"},
                    ]
                }
            },
        )

        self.assertIs(verdict.status, EvaluationStatus.FAIL)
        self.assertEqual(verdict.score, 0.0)
        self.assertEqual((verdict.observed_satisfied, verdict.observed_total), (1, 2))

    def test_a_bucket_with_default_encryption_passes(self) -> None:
        """라이브에서 모델은 AES256이 적용된 버킷을 암호화 없음으로 판정했다."""
        verdict = _verdict(
            "S3_ENCRYPTION_AT_REST",
            "S3.ENCRYPTION",
            S3,
            {
                "attributes": {
                    "encryption": {
                        "Rules": [
                            {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                        ]
                    }
                }
            },
        )

        self.assertIs(verdict.status, EvaluationStatus.PASS)

    def test_one_unencrypted_volume_among_two_is_a_violation(self) -> None:
        verdict = _verdict(
            "EC2_EBS_ENCRYPTION",
            "EC2.VOLUME_ENCRYPTION",
            EC2,
            {
                "attributes": {
                    "volumes": [
                        {"VolumeId": "vol-1", "Encrypted": True},
                        {"VolumeId": "vol-2", "Encrypted": False},
                    ]
                }
            },
        )

        self.assertIs(verdict.status, EvaluationStatus.FAIL)
        self.assertEqual(verdict.score, 0.0)
        self.assertEqual((verdict.observed_satisfied, verdict.observed_total), (1, 2))


class BoundaryTest(unittest.TestCase):
    def test_a_rule_whose_capability_has_no_predicate_is_left_to_the_model(self) -> None:
        """해석이 필요한 통제까지 코드가 답하면 근거 없는 확신이 된다."""
        rule = _rule("EC2_SG_INGRESS_RESTRICTED", "EC2.SECURITY_GROUP_INGRESS", EC2)

        self.assertEqual(decidable_bindings(CATALOG, rule, resource_type=EC2), ())

    def test_a_legacy_rule_the_catalog_maps_is_decided_like_an_authored_one(self) -> None:
        """배포된 baseline Profile은 legacy Rule로 돼 있다. Catalog는 그것이 어떤 Control인지 안다."""
        legacy = _legacy("S3-PUBLIC-001")

        bindings = decidable_bindings(CATALOG, legacy, resource_type=S3)

        self.assertEqual(
            [binding.capability_key for binding in bindings], ["S3.PUBLIC_ACCESS_BLOCK"]
        )

    def test_a_legacy_rule_the_catalog_does_not_map_is_left_to_the_model(self) -> None:
        self.assertEqual(decidable_bindings(CATALOG, _legacy("CUSTOM-1"), resource_type=S3), ())

    def test_a_mixed_rule_falls_back_entirely_rather_than_half_deciding(self) -> None:
        """하나의 결과가 두 근거 체계를 섞으면 그 결과가 무엇에 근거했는지 말할 수 없다."""
        rule = _rule(
            "RDS_ACCESS_RESTRICTED",
            "RDS.SECURITY_GROUP_INGRESS",
            "AWS::RDS::DBInstance",
            required_evidence=("RDS.SECURITY_GROUP_INGRESS", "RDS.SUBNET_GROUP"),
        )

        self.assertEqual(
            decidable_bindings(CATALOG, rule, resource_type="AWS::RDS::DBInstance"), ()
        )

    def test_an_identifier_never_satisfies_a_boolean_criterion(self) -> None:
        """`ALL_TRUE`가 참으로 평가되는 값을 받으면 식별자가 통제를 통과시킨다."""
        binding = EvidenceCapabilityBinding(
            capability_key="TEST.FLAG",
            perspective=EvaluationPerspective.AWS_ACTUAL,
            resource_type=S3,
            document_paths=("attributes.flag",),
            expectation=EvidenceExpectation.ALL_TRUE,
        )

        self.assertIs(
            decide((binding,), {"attributes": {"flag": "vol-0abc"}}).status,
            EvaluationStatus.FAIL,
        )

    def test_a_document_missing_the_declared_value_fails_closed(self) -> None:
        binding = EvidenceCapabilityBinding(
            capability_key="TEST.FLAG",
            perspective=EvaluationPerspective.AWS_ACTUAL,
            resource_type=S3,
            document_paths=("attributes.flag",),
            expectation=EvidenceExpectation.ALL_TRUE,
        )

        with self.assertRaises(DeterministicEvaluationError):
            decide((binding,), {"attributes": {}})


class ResultShapeTest(unittest.TestCase):
    def test_the_result_looks_like_any_other_actual_result(self) -> None:
        """조치·비교·보고가 결과의 출처로 분기하지 않는다."""
        rule = _rule("S3_BLOCK_PUBLIC_ACCESS", "S3.PUBLIC_ACCESS_BLOCK", S3)
        verdict = _verdict(
            "S3_BLOCK_PUBLIC_ACCESS",
            "S3.PUBLIC_ACCESS_BLOCK",
            S3,
            _block(
                BlockPublicAcls=False,
                IgnorePublicAcls=False,
                BlockPublicPolicy=False,
                RestrictPublicBuckets=False,
            ),
        )

        result = result_from_verdict(
            verdict,
            resource_id="bucket-1",
            rule=rule,
            evidence_references=("aws:s3:bucket/bucket-1#read-resource",),
            model_profile=PROFILE,
        )

        self.assertIs(result.perspective, EvaluationPerspective.AWS_ACTUAL)
        self.assertIs(result.status, EvaluationStatus.FAIL)
        self.assertEqual(result.severity, "HIGH")
        # 평가 구성의 정체는 모델을 부르지 않았어도 복원돼야 한다 — 배포 전후 비교가 요구한다.
        self.assertEqual(result.model_profile_id, PROFILE.model_profile_id)
        self.assertEqual(result.rubric_version, PROFILE.rubric_version)
        # 근거는 실제 read 하나와 Rule이 인용한 정책 판본이다.
        self.assertIn("aws:s3:bucket/bucket-1#read-resource", result.evidence_references)
        self.assertIn("src-1@ver-1#item/1", result.evidence_references)


if __name__ == "__main__":
    unittest.main()
