"""Bedrock adapter keeps model output within the approved assessment boundary."""

import json
import unittest

from apps.backend.assessment import BedrockEvaluationError, BedrockStructuredEvaluator
from apps.backend.policy import PolicyContext
from packages.contracts import (
    AssessmentPhase,
    EvaluationPerspective,
    EvaluationStatus,
    ModelProfile,
    ModelProfileRole,
    PolicyRule,
    RuleEvaluationType,
    RuleSeverity,
    SourceReference,
)

PROFILE = ModelProfile(
    model_profile_id="assessment-nova-lite-m1-v1",
    role=ModelProfileRole.ASSESSMENT,
    region="us-east-1",
    model_id="amazon.nova-lite-v1:0",
    prompt_version="assessment-s3-v1",
    rubric_version="mvp-v1",
    golden_dataset_version="m1-s3-v1",
)
RULE = PolicyRule(
    rule_id="S3-PUBLIC-001",
    version="2026-08-01",
    title="S3 buckets must block public access",
    severity=RuleSeverity.HIGH,
    applicable_phases=(AssessmentPhase.INITIAL,),
    resource_types=("AWS::S3::Bucket",),
    source_references=(
        SourceReference(
            source_id="isms-p",
            source_version="2023-10-31",
            locator="control/5.2.1",
            content_sha256="abc",
        ),
    ),
)
CONTEXT = PolicyContext(
    policy_profile_id="profile-mvp-baseline",
    policy_profile_version="v1",
    phase=AssessmentPhase.INITIAL,
    resource_type="AWS::S3::Bucket",
    rules=(RULE,),
)


class Client:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def converse(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self.response


def response(body: dict[str, object]) -> dict[str, object]:
    return {"output": {"message": {"content": [{"text": json.dumps(body)}]}}}


class BedrockStructuredEvaluatorTest(unittest.TestCase):
    def evaluator(self, client: Client) -> BedrockStructuredEvaluator:
        return BedrockStructuredEvaluator(
            client=client,
            perspective=EvaluationPerspective.AWS_ACTUAL,
            resource_document={"bucket": "bucket-public-001", "public_access_block": False},
            evidence_references=("aws:s3:GetPublicAccessBlock",),
        )

    def test_reconstructs_authoritative_fields_and_invokes_approved_model(self) -> None:
        client = Client(
            response(
                {
                    "status": "FAIL",
                    "score": 21,
                    "rationale": "Public access block is disabled.",
                    "evidence_references": [
                        "aws:s3:GetPublicAccessBlock",
                        "isms-p@2023-10-31#control/5.2.1",
                    ],
                }
            )
        )

        result = self.evaluator(client).evaluate(
            resource_id="bucket-public-001", rule=RULE, context=CONTEXT, model_profile=PROFILE
        )

        self.assertEqual(result.perspective, EvaluationPerspective.AWS_ACTUAL)
        self.assertEqual(result.rule_id, RULE.rule_id)
        self.assertEqual(result.severity, "HIGH")
        self.assertEqual(result.status, EvaluationStatus.FAIL)
        self.assertEqual(
            result.evidence_references,
            ("aws:s3:GetPublicAccessBlock", "isms-p@2023-10-31#control/5.2.1"),
        )
        self.assertEqual(client.calls[0]["modelId"], PROFILE.model_id)
        message = client.calls[0]["messages"]
        self.assertIn('"resource_id":"bucket-public-001"', message[0]["content"][0]["text"])

    def test_rejects_model_evidence_outside_the_snapshot_and_rule(self) -> None:
        client = Client(
            response(
                {
                    "status": "FAIL",
                    "score": 21,
                    "rationale": "Public access block is disabled.",
                    "evidence_references": ["aws:iam:ListUsers"],
                }
            )
        )

        with self.assertRaisesRegex(BedrockEvaluationError, "outside approved evidence"):
            self.evaluator(client).evaluate(
                resource_id="bucket-public-001", rule=RULE, context=CONTEXT, model_profile=PROFILE
            )

    def test_rejects_extra_or_unstructured_model_fields(self) -> None:
        client = Client(
            response(
                {
                    "status": "PASS",
                    "score": 100,
                    "rationale": "Safe.",
                    "evidence_references": ["aws:s3:GetPublicAccessBlock"],
                    "severity": "LOW",
                }
            )
        )

        with self.assertRaisesRegex(BedrockEvaluationError, "fields are invalid"):
            self.evaluator(client).evaluate(
                resource_id="bucket-public-001", rule=RULE, context=CONTEXT, model_profile=PROFILE
            )

    def test_accepts_json_object_wrapped_in_a_markdown_code_fence(self) -> None:
        # Nova models frequently wrap the JSON object in a ```json ... ``` fence.
        body = {
            "status": "PASS",
            "score": 100,
            "rationale": "Public access block is enabled.",
            "evidence_references": ["aws:s3:GetPublicAccessBlock"],
        }
        fenced = "```json\n" + json.dumps(body) + "\n```"
        client = Client({"output": {"message": {"content": [{"text": fenced}]}}})

        result = self.evaluator(client).evaluate(
            resource_id="bucket-public-001", rule=RULE, context=CONTEXT, model_profile=PROFILE
        )

        self.assertEqual(result.status, EvaluationStatus.PASS)
        self.assertEqual(result.score, 100)

    def test_ai_may_judge_a_rule_out_of_scope_for_the_resource(self) -> None:
        # The AI Evaluator selects applicability within the approved boundary: a rule
        # that does not govern this resource is OUT_OF_SCOPE, not PASS/FAIL. The plan
        # still fixed the coordinate, so Coverage counts it as completed while
        # readiness excludes it (ADR-0002 AI selects applicable Rule; ADR-0016).
        client = Client(
            response(
                {
                    "status": "OUT_OF_SCOPE",
                    "score": 0,
                    "rationale": "This rule governs a different resource concern.",
                    "evidence_references": ["aws:s3:GetPublicAccessBlock"],
                }
            )
        )

        result = self.evaluator(client).evaluate(
            resource_id="bucket-public-001", rule=RULE, context=CONTEXT, model_profile=PROFILE
        )

        self.assertEqual(result.status, EvaluationStatus.OUT_OF_SCOPE)
        # OUT_OF_SCOPE never becomes a Finding; only follow-up statuses do.
        from apps.backend.assessment.findings import finding_from_result

        self.assertIsNone(finding_from_result(result))

    def test_system_prompt_grants_applicability_judgment(self) -> None:
        from apps.backend.assessment.bedrock import _SYSTEM_PROMPT

        # The prompt must give the model authority to decide applicability and name
        # OUT_OF_SCOPE, otherwise "AI selects applicable Rule" (ADR-0002) is not honored.
        self.assertIn("OUT_OF_SCOPE", _SYSTEM_PROMPT)
        self.assertIn("whether the rule even applies", _SYSTEM_PROMPT)


class ScoreContractGuardTest(unittest.TestCase):
    """Runtime guards between the model's number and the stored Evaluation Result.

    연속 0–100 점수 계약에서 Runtime이 지켜야 하는 것 두 가지다. (1) 점수는 유한한 숫자여야 한다 —
    NaN·무한대·문자열·bool은 계약 위반이다. (2) 판정이 아닌 status(MANUAL_REVIEW,
    INSUFFICIENT_EVIDENCE, OUT_OF_SCOPE)에는 모델의 숫자가 남지 않는다. Code가 만드는 같은
    status(`actual_evaluator`, `manual_review`)와 같은 0.0으로 고정해야 같은 좌표가 실행마다 다른
    점수를 갖지 않고, readiness 평균에 판정 아닌 숫자가 섞이지 않는다.
    """

    def _evaluate(self, client: Client):
        return BedrockStructuredEvaluator(
            client=client,
            perspective=EvaluationPerspective.AWS_ACTUAL,
            resource_document={"bucket": "bucket-public-001"},
            evidence_references=("aws:s3:GetPublicAccessBlock",),
        ).evaluate(
            resource_id="bucket-public-001", rule=RULE, context=CONTEXT, model_profile=PROFILE
        )

    def _body(self, **overrides: object) -> dict[str, object]:
        body: dict[str, object] = {
            "status": "FAIL",
            "score": 21,
            "rationale": "Public access block is disabled.",
            "evidence_references": ["aws:s3:GetPublicAccessBlock"],
        }
        body.update(overrides)
        return body

    def test_non_judgment_statuses_never_keep_the_model_score(self) -> None:
        from apps.backend.assessment.actual_evaluator import INSUFFICIENT_EVIDENCE_SCORE
        from apps.backend.assessment.bedrock import NON_JUDGMENT_SCORE
        from apps.backend.assessment.manual_review import MANUAL_REVIEW_SCORE

        # Code 쪽 규약과 같은 값이어야 한다. 셋이 갈라지면 같은 status가 다른 점수를 갖는다.
        self.assertEqual(NON_JUDGMENT_SCORE, INSUFFICIENT_EVIDENCE_SCORE)
        self.assertEqual(NON_JUDGMENT_SCORE, MANUAL_REVIEW_SCORE)
        for status in ("INSUFFICIENT_EVIDENCE", "MANUAL_REVIEW", "OUT_OF_SCOPE"):
            with self.subTest(status=status):
                result = self._evaluate(Client(response(self._body(status=status, score=57))))
                self.assertEqual(result.status, EvaluationStatus(status))
                self.assertEqual(result.score, NON_JUDGMENT_SCORE)

    def test_judgment_statuses_keep_the_continuous_score(self) -> None:
        for status, score in (("FAIL", 12.5), ("PASS", 88)):
            with self.subTest(status=status):
                result = self._evaluate(Client(response(self._body(status=status, score=score))))
                self.assertEqual(result.score, score)

    def test_non_numeric_or_non_finite_scores_are_contract_errors(self) -> None:
        for score in ("80", True, None, float("nan"), float("inf"), -1, 100.5):
            with self.subTest(score=repr(score)):
                body = self._body(score=score)
                text = json.dumps(body, allow_nan=True)
                client = Client({"output": {"message": {"content": [{"text": text}]}}})
                with self.assertRaisesRegex(BedrockEvaluationError, "score must be a number"):
                    self._evaluate(client)

    def test_a_resource_anchor_inside_an_approved_file_is_accepted(self) -> None:
        """Golden IaC evidence is `terraform:{path}#{resource address}`; the allow-list holds
        `terraform:{path}`. Rejecting the anchored form failed every IAC PASS run live."""
        client = Client(
            response(
                {
                    "status": "PASS",
                    "score": 100,
                    "rationale": "All four flags are true.",
                    "evidence_references": [
                        "terraform:storage.tf#aws_s3_bucket_public_access_block.media",
                        "isms-p@2023-10-31#control/5.2.1",
                    ],
                }
            )
        )
        result = BedrockStructuredEvaluator(
            client=client,
            perspective=EvaluationPerspective.IAC,
            resource_document={"files": [{"path": "storage.tf", "content": "..."}]},
            evidence_references=("terraform:storage.tf",),
        ).evaluate(
            resource_id="bucket-public-001", rule=RULE, context=CONTEXT, model_profile=PROFILE
        )
        self.assertEqual(
            result.evidence_references,
            (
                "terraform:storage.tf#aws_s3_bucket_public_access_block.media",
                "isms-p@2023-10-31#control/5.2.1",
            ),
        )

    def test_an_anchor_on_an_unapproved_file_or_policy_locator_is_still_refused(self) -> None:
        for reference in (
            "terraform:other.tf#aws_s3_bucket.x",  # file not in the snapshot
            "isms-p@2023-10-31#control/9.9.9",  # policy locator the rule does not cite
            "terraform:storage.tf#",  # empty anchor
        ):
            with self.subTest(reference=reference):
                client = Client(
                    response(
                        {
                            "status": "PASS",
                            "score": 100,
                            "rationale": "ok",
                            "evidence_references": [reference],
                        }
                    )
                )
                evaluator = BedrockStructuredEvaluator(
                    client=client,
                    perspective=EvaluationPerspective.IAC,
                    resource_document={"files": []},
                    evidence_references=("terraform:storage.tf",),
                )
                with self.assertRaisesRegex(BedrockEvaluationError, "outside approved evidence"):
                    evaluator.evaluate(
                        resource_id="bucket-public-001",
                        rule=RULE,
                        context=CONTEXT,
                        model_profile=PROFILE,
                    )

    def test_the_prompt_asks_for_gradation_without_advertising_the_evasive_statuses(
        self,
    ) -> None:
        """Score pinning belongs to `_normalized_score`, not to prose.

        An earlier revision of this prompt re-listed MANUAL_REVIEW, INSUFFICIENT_EVIDENCE and
        OUT_OF_SCOPE with their fixed score. Live A/B on RDS-ACCESS-001 (private instance,
        3306 open to 0.0.0.0/0, n=8 per arm) showed that wording cost accuracy: 5/8 correct
        FAIL without it, 0/8 with it — every run evaded to OUT_OF_SCOPE. The runtime pins the
        score either way, so the enumeration bought nothing and is gone.
        """
        from apps.backend.assessment.bedrock import _SYSTEM_PROMPT

        self.assertIn("place a partially satisfied resource between the extremes", _SYSTEM_PROMPT)
        # The one applicability sentence the dev prompt already had may name OUT_OF_SCOPE;
        # a second, score-shaped mention is what moved the model.
        self.assertEqual(_SYSTEM_PROMPT.count("OUT_OF_SCOPE"), 2)
        self.assertNotIn("no judgment was made", _SYSTEM_PROMPT)


AUTHORED_RULE = PolicyRule(
    rule_id="S3-PUBLIC-AUTHORED",
    version="2026-09-03",
    title="Object storage blocks public access",
    severity=RuleSeverity.CRITICAL,
    applicable_phases=(AssessmentPhase.INITIAL,),
    resource_types=("AWS::S3::Bucket",),
    source_references=RULE.source_references,
    control_key="S3_BLOCK_PUBLIC_ACCESS",
    control_catalog_version="governance-control-catalog/2026-09-03",
    evaluation_type=RuleEvaluationType.AWS,
    applicability_semantics="Every bucket the workload writes to.",
    required_evidence=("S3.PUBLIC_ACCESS_BLOCK",),
    evaluation_rubric="FAIL unless all four public access block flags are true.",
    severity_guidance="Public read of customer media is a data exposure.",
)


class ApprovedRuleSemanticsTest(unittest.TestCase):
    """승인된 Rule의 실행 의미가 모델에 도달해야 정책 → Rule → Assessment가 연결된다."""

    def _client(self) -> Client:
        return Client(
            response(
                {
                    "status": "FAIL",
                    "score": 5,
                    "rationale": "Two flags are false.",
                    "evidence_references": ["aws:s3:GetPublicAccessBlock"],
                }
            )
        )

    def _evaluate(self, client: Client, rule: PolicyRule) -> None:
        context = PolicyContext(
            policy_profile_id="profile-customer",
            policy_profile_version="v1",
            phase=AssessmentPhase.INITIAL,
            resource_type="AWS::S3::Bucket",
            rules=(rule,),
        )
        BedrockStructuredEvaluator(
            client=client,
            perspective=EvaluationPerspective.AWS_ACTUAL,
            resource_document={"bucket": "bucket-public-001"},
            evidence_references=("aws:s3:GetPublicAccessBlock",),
        ).evaluate(
            resource_id="bucket-public-001", rule=rule, context=context, model_profile=PROFILE
        )

    def test_the_approved_rubric_and_evidence_capabilities_reach_the_model(self) -> None:
        client = self._client()
        self._evaluate(client, AUTHORED_RULE)
        body = json.loads(client.calls[0]["messages"][0]["content"][0]["text"])
        rule_view = body["rule"]
        self.assertEqual(rule_view["evaluation_rubric"], AUTHORED_RULE.evaluation_rubric)
        self.assertEqual(
            rule_view["applicability_semantics"], AUTHORED_RULE.applicability_semantics
        )
        self.assertEqual(rule_view["required_evidence"], ["S3.PUBLIC_ACCESS_BLOCK"])
        self.assertEqual(rule_view["evaluation_type"], "AWS")
        # 판정에 영향을 주지 않는 identity 필드는 prompt에 넣지 않는다.
        self.assertNotIn("control_key", rule_view)
        self.assertNotIn("applicable_phases", rule_view)

    def test_a_legacy_rule_view_carries_no_execution_semantics(self) -> None:
        client = self._client()
        self._evaluate(client, RULE)
        rule_view = json.loads(client.calls[0]["messages"][0]["content"][0]["text"])["rule"]
        self.assertEqual(
            set(rule_view), {"rule_id", "version", "title", "severity", "source_references"}
        )

    def test_the_system_prompt_names_the_rubric_and_the_insufficient_evidence_exit(self) -> None:
        client = self._client()
        self._evaluate(client, AUTHORED_RULE)
        system_text = client.calls[0]["system"][0]["text"]
        self.assertIn("evaluation_rubric", system_text)
        self.assertIn("INSUFFICIENT_EVIDENCE", system_text)


class ModelStatusBoundaryTest(unittest.TestCase):
    def test_the_model_cannot_return_execution_error(self) -> None:
        """EXECUTION_ERROR는 Code가 기록하는 실행 실패이지 모델의 판정이 아니다."""
        client = Client(
            response(
                {
                    "status": "EXECUTION_ERROR",
                    "score": 0,
                    "rationale": "I could not decide.",
                    "evidence_references": ["aws:s3:GetPublicAccessBlock"],
                }
            )
        )
        with self.assertRaisesRegex(BedrockEvaluationError, "reserved for the runtime"):
            BedrockStructuredEvaluator(
                client=client,
                perspective=EvaluationPerspective.AWS_ACTUAL,
                resource_document={"bucket": "bucket-public-001"},
                evidence_references=("aws:s3:GetPublicAccessBlock",),
            ).evaluate(
                resource_id="bucket-public-001", rule=RULE, context=CONTEXT, model_profile=PROFILE
            )

    def test_the_model_may_still_report_insufficient_evidence(self) -> None:
        client = Client(
            response(
                {
                    "status": "INSUFFICIENT_EVIDENCE",
                    "score": 0,
                    "rationale": "The document does not show the block configuration.",
                    "evidence_references": ["aws:s3:GetPublicAccessBlock"],
                }
            )
        )
        result = BedrockStructuredEvaluator(
            client=client,
            perspective=EvaluationPerspective.AWS_ACTUAL,
            resource_document={"bucket": "bucket-public-001"},
            evidence_references=("aws:s3:GetPublicAccessBlock",),
        ).evaluate(
            resource_id="bucket-public-001", rule=RULE, context=CONTEXT, model_profile=PROFILE
        )
        self.assertIs(result.status, EvaluationStatus.INSUFFICIENT_EVIDENCE)
