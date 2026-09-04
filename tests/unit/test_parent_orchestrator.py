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

    def test_policy_context_is_passed_to_the_model_as_system_grounding(self) -> None:
        client = Client(
            {
                "intent": "POLICY_QA",
                "rationale": "The user asks what a listed rule requires.",
                "answer": "S3-PUBLIC-001 requires blocking public access.",
                "selector": None,
            }
        )

        decision = ParentOrchestrator(client=client).route(
            request("what does our public access rule require?"),
            model_profile=PARENT_PROFILE,
            policy_context="rule_id: S3-PUBLIC-001\ntitle: S3 block public access",
        )

        self.assertIs(decision.intent, OrchestrationIntent.POLICY_QA)
        system_texts = " ".join(block["text"] for block in client.calls[0]["system"])
        self.assertIn("POLICY CONTEXT —", system_texts)
        self.assertIn("S3-PUBLIC-001", system_texts)

    def test_without_policy_context_no_grounding_block_is_sent(self) -> None:
        client = Client(
            {
                "intent": "POLICY_QA",
                "rationale": "general question",
                "answer": "Encryption at rest protects stored data.",
                "selector": None,
            }
        )

        ParentOrchestrator(client=client).route(
            request("what is encryption at rest?"), model_profile=PARENT_PROFILE
        )

        system_texts = " ".join(block["text"] for block in client.calls[0]["system"])
        self.assertNotIn("POLICY CONTEXT —", system_texts)

    def test_extra_model_fields_are_tolerated(self) -> None:
        # The strict exact-field-set check rejected harmless drift (an added key), turning a usable
        # reply into a 500. Extra keys are now ignored; the known fields still drive the decision.
        client = Client(
            {
                "intent": "POLICY_QA",
                "rationale": "x",
                "answer": "Block public access.",
                "selector": None,
                "confidence": 0.9,
            }
        )
        decision = ParentOrchestrator(client=client).route(request(), model_profile=PARENT_PROFILE)
        self.assertIs(decision.intent, OrchestrationIntent.POLICY_QA)
        self.assertEqual(decision.answer, "Block public access.")

    def test_a_missing_rationale_is_tolerated(self) -> None:
        client = Client({"intent": "ASSESSMENT", "selector": {"repository_id": "repo-1"}})
        decision = ParentOrchestrator(client=client).route(request(), model_profile=PARENT_PROFILE)
        self.assertIs(decision.intent, OrchestrationIntent.ASSESSMENT)
        self.assertEqual(decision.selector.repository_id, "repo-1")
        self.assertTrue(decision.rationale)

    def test_policy_qa_answer_falls_back_to_rationale_when_answer_is_missing(self) -> None:
        client = Client({"intent": "POLICY_QA", "rationale": "Encryption at rest is required."})
        decision = ParentOrchestrator(client=client).route(request(), model_profile=PARENT_PROFILE)
        self.assertIs(decision.intent, OrchestrationIntent.POLICY_QA)
        self.assertEqual(decision.answer, "Encryption at rest is required.")

    def test_a_reply_split_across_content_blocks_is_joined(self) -> None:
        class SplitClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def converse(self, **kwargs: object) -> object:
                self.calls.append(kwargs)
                return {
                    "output": {
                        "message": {
                            "content": [
                                {"text": '{"intent":"POLICY_QA","rationale":"r",'},
                                {"text": '"answer":"Split answer."}'},
                            ]
                        }
                    }
                }

        decision = ParentOrchestrator(client=SplitClient()).route(
            request(), model_profile=PARENT_PROFILE
        )
        self.assertIs(decision.intent, OrchestrationIntent.POLICY_QA)
        self.assertEqual(decision.answer, "Split answer.")

    def test_a_response_without_intent_still_fails(self) -> None:
        client = Client({"rationale": "x", "answer": "y", "selector": None})
        with self.assertRaisesRegex(OrchestrationError, "missing intent"):
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


if __name__ == "__main__":
    unittest.main()
