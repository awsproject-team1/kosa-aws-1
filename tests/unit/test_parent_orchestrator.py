"""Parent Orchestrator keeps natural-language routing inside the ADR-0012 boundary."""

import json
import unittest

from agent.agents.parent_orchestrator import OrchestrationError, ParentOrchestrator
from agent.graphs.parent_graph import build_parent_graph
from packages.contracts import (
    ModelProfile,
    ModelProfileRole,
    OrchestrationIntent,
    OrchestrationRequest,
)

PARENT_PROFILE = ModelProfile(
    model_profile_id="parent-nova-lite-m1-v1",
    role=ModelProfileRole.PARENT,
    region="us-east-1",
    model_id="amazon.nova-lite-v1:0",
    prompt_version="parent-v1",
    rubric_version="parent-v1",
    golden_dataset_version="parent-v1",
)
ASSESSMENT_PROFILE = ModelProfile(
    model_profile_id="assessment-nova-lite-m1-v1",
    role=ModelProfileRole.ASSESSMENT,
    region="us-east-1",
    model_id="amazon.nova-lite-v1:0",
    prompt_version="assessment-v1",
    rubric_version="mvp-v1",
    golden_dataset_version="m1-s3-v1",
)


class Client:
    def __init__(self, body: object) -> None:
        self.body = body
        self.calls: list[dict[str, object]] = []

    def converse(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return {"output": {"message": {"content": [{"text": json.dumps(self.body)}]}}}


def request(message: str = "check my s3 buckets") -> OrchestrationRequest:
    return OrchestrationRequest(message=message)


class ParentOrchestratorTest(unittest.TestCase):
    def test_policy_qa_is_answered_directly_without_a_workflow(self) -> None:
        client = Client(
            {
                "intent": "POLICY_QA",
                "rationale": "The user asks what a control means.",
                "answer": "ISMS-P 2.5 requires encryption at rest.",
                "selector": None,
            }
        )

        decision = ParentOrchestrator(client=client).route(
            request("what does ISMS-P 2.5 mean?"), model_profile=PARENT_PROFILE
        )

        self.assertIs(decision.intent, OrchestrationIntent.POLICY_QA)
        self.assertIn("encryption", decision.answer)
        self.assertIsNone(decision.selector)
        self.assertFalse(decision.requires_confirmation)

    def test_assessment_is_a_proposal_requiring_confirmation_with_selectors(self) -> None:
        client = Client(
            {
                "intent": "ASSESSMENT",
                "rationale": "The user wants to evaluate a repository.",
                "answer": None,
                "selector": {
                    "repository_id": "test-s3-sandbox",
                    "policy_profile_id": "profile-mvp-baseline",
                },
            }
        )

        decision = ParentOrchestrator(client=client).route(request(), model_profile=PARENT_PROFILE)

        self.assertIs(decision.intent, OrchestrationIntent.ASSESSMENT)
        self.assertTrue(decision.requires_confirmation)
        self.assertTrue(decision.is_workflow_proposal)
        self.assertEqual(decision.selector.repository_id, "test-s3-sandbox")
        self.assertIsNone(decision.answer)

    def test_remediation_proposal_carries_finding_selector(self) -> None:
        client = Client(
            {
                "intent": "REMEDIATION",
                "rationale": "The user wants to fix a finding.",
                "answer": None,
                "selector": {"finding_id": "finding-abc"},
            }
        )

        decision = ParentOrchestrator(client=client).route(
            request("fix finding-abc"), model_profile=PARENT_PROFILE
        )

        self.assertIs(decision.intent, OrchestrationIntent.REMEDIATION)
        self.assertEqual(decision.selector.finding_id, "finding-abc")
        self.assertTrue(decision.requires_confirmation)

    def test_unsupported_request_carries_no_answer_or_selector(self) -> None:
        client = Client(
            {
                "intent": "UNSUPPORTED",
                "rationale": "Out of scope for this platform.",
                "answer": None,
                "selector": None,
            }
        )

        decision = ParentOrchestrator(client=client).route(
            request("what's the weather?"), model_profile=PARENT_PROFILE
        )

        self.assertIs(decision.intent, OrchestrationIntent.UNSUPPORTED)
        self.assertIsNone(decision.answer)
        self.assertIsNone(decision.selector)
        self.assertFalse(decision.requires_confirmation)

    def test_rejects_a_non_parent_model_profile(self) -> None:
        with self.assertRaisesRegex(OrchestrationError, "not approved for the Parent"):
            ParentOrchestrator(client=Client({})).route(request(), model_profile=ASSESSMENT_PROFILE)

    def test_rejects_extra_model_fields(self) -> None:
        client = Client(
            {
                "intent": "POLICY_QA",
                "rationale": "x",
                "answer": "y",
                "selector": None,
                "extra": 1,
            }
        )
        with self.assertRaisesRegex(OrchestrationError, "fields are invalid"):
            ParentOrchestrator(client=client).route(request(), model_profile=PARENT_PROFILE)

    def test_rejects_invalid_intent(self) -> None:
        client = Client({"intent": "START_JOB", "rationale": "x", "answer": None, "selector": None})
        with self.assertRaisesRegex(OrchestrationError, "intent is invalid"):
            ParentOrchestrator(client=client).route(request(), model_profile=PARENT_PROFILE)


class ParentGraphTest(unittest.TestCase):
    def test_graph_routes_policy_qa_to_a_direct_answer(self) -> None:
        client = Client(
            {
                "intent": "POLICY_QA",
                "rationale": "Question about policy.",
                "answer": "Buckets must block public access.",
                "selector": None,
            }
        )
        graph = build_parent_graph(ParentOrchestrator(client=client))

        state = graph.invoke(
            {"request": request("what is required?"), "model_profile": PARENT_PROFILE}
        )

        self.assertIs(state["decision"].intent, OrchestrationIntent.POLICY_QA)
        self.assertIn("public access", state["decision"].answer)

    def test_graph_routes_workflow_intent_to_a_confirmation_proposal(self) -> None:
        client = Client(
            {
                "intent": "ASSESSMENT",
                "rationale": "Wants an assessment.",
                "answer": None,
                "selector": {"repository_id": "repo-1"},
            }
        )
        graph = build_parent_graph(ParentOrchestrator(client=client))

        state = graph.invoke({"request": request(), "model_profile": PARENT_PROFILE})

        self.assertIs(state["decision"].intent, OrchestrationIntent.ASSESSMENT)
        self.assertTrue(state["decision"].requires_confirmation)


class PolicyGroundingTest(unittest.TestCase):
    """The customer's material rides in a second system block with the grounding rules.

    Without it the model answered from its own memory and called that the customer's policy.
    With it, the rules say: only this material, cite locators, walk the outline for a list, and
    admit when the material does not cover the question.
    """

    ANSWER = {
        "intent": "POLICY_QA",
        "rationale": "The user asks about their S3 policy.",
        "answer": "모든 S3 버킷은 퍼블릭 액세스 차단을 활성화해야 합니다 [heading/1/item/1].",
        "selector": None,
    }

    def _system_texts(self, client: Client) -> list[str]:
        return [block["text"] for block in client.calls[0]["system"]]

    def test_material_and_rules_reach_the_model_as_system_text(self) -> None:
        client = Client(self.ANSWER)
        material = "## 문서 1: 정책.md\n[heading/1/item/1] 모든 S3 버킷은 퍼블릭 액세스 차단"
        ParentOrchestrator(client=client).route(
            request("사내 S3 정책 설명해줘"), model_profile=PARENT_PROFILE, policy_context=material
        )
        texts = self._system_texts(client)
        self.assertEqual(len(texts), 2)
        self.assertIn("POLICY MATERIAL", texts[1])
        self.assertIn(material, texts[1])
        self.assertIn("cite", texts[1])
        self.assertIn("outline", texts[1])
        # The user's message itself is untouched: material never rides in the user turn.
        self.assertEqual(
            client.calls[0]["messages"][0]["content"][0]["text"], "사내 S3 정책 설명해줘"
        )

    def test_without_material_the_model_is_told_none_exists(self) -> None:
        client = Client(self.ANSWER)
        ParentOrchestrator(client=client).route(
            request("정책 나열해줘"), model_profile=PARENT_PROFILE
        )
        texts = self._system_texts(client)
        self.assertEqual(len(texts), 2)
        self.assertIn("No policy document", texts[1])
        self.assertNotIn("POLICY MATERIAL", texts[1])

    def test_blank_material_counts_as_none(self) -> None:
        client = Client(self.ANSWER)
        ParentOrchestrator(client=client).route(
            request(), model_profile=PARENT_PROFILE, policy_context="   "
        )
        self.assertIn("No policy document", self._system_texts(client)[1])

    def test_the_answer_budget_fits_a_grounded_list(self) -> None:
        client = Client(self.ANSWER)
        ParentOrchestrator(client=client).route(request(), model_profile=PARENT_PROFILE)
        self.assertGreaterEqual(client.calls[0]["inferenceConfig"]["maxTokens"], 2048)

    def test_material_must_be_text(self) -> None:
        with self.assertRaises(TypeError):
            ParentOrchestrator(client=Client(self.ANSWER)).route(
                request(),
                model_profile=PARENT_PROFILE,
                policy_context=["not", "text"],  # type: ignore[arg-type]
            )

    def test_the_graph_threads_material_into_the_classify_node(self) -> None:
        client = Client(self.ANSWER)
        graph = build_parent_graph(ParentOrchestrator(client=client))
        graph.invoke(
            {"request": request(), "model_profile": PARENT_PROFILE, "policy_context": "## 문서 1"}
        )
        self.assertIn("## 문서 1", self._system_texts(client)[1])


if __name__ == "__main__":
    unittest.main()
