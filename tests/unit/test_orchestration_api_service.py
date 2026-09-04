"""The orchestrate API boundary authorizes then delegates without starting work."""

import unittest

from apps.backend.api.orchestration import OrchestrationApiService
from apps.backend.auth import Principal, Role
from packages.contracts import (
    ModelProfile,
    ModelProfileRole,
    OrchestrationDecision,
    OrchestrationIntent,
    OrchestrationRequest,
    WorkflowSelectorCandidate,
)

PROFILE = ModelProfile(
    model_profile_id="parent-nova-lite-m1-v1",
    role=ModelProfileRole.PARENT,
    region="us-east-1",
    model_id="amazon.nova-lite-v1:0",
    prompt_version="parent-v1",
    rubric_version="parent-v1",
    golden_dataset_version="parent-v1",
)


class Router:
    def __init__(self, decision: OrchestrationDecision) -> None:
        self.decision = decision
        self.calls: list[tuple[OrchestrationRequest, ModelProfile]] = []

    def route(self, request, *, model_profile, policy_context=None):
        self.calls.append((request, model_profile))
        self.contexts: list[str | None] = getattr(self, "contexts", [])
        self.contexts.append(policy_context)
        return self.decision


def principal(role: Role = Role.USER) -> Principal:
    return Principal(
        subject="user", client_id="client", customer_id="cust", roles=frozenset({role})
    )


def proposal() -> OrchestrationDecision:
    return OrchestrationDecision(
        intent=OrchestrationIntent.ASSESSMENT,
        rationale="wants an assessment",
        selector=WorkflowSelectorCandidate(repository_id="repo-1"),
        requires_confirmation=True,
    )


class OrchestrationApiServiceTest(unittest.TestCase):
    def test_authorizes_and_delegates_to_the_router(self) -> None:
        router = Router(proposal())
        service = OrchestrationApiService(router=router, model_profile=PROFILE)

        decision = service.orchestrate(principal(), OrchestrationRequest(message="assess repo-1"))

        self.assertIs(decision.intent, OrchestrationIntent.ASSESSMENT)
        self.assertTrue(decision.requires_confirmation)
        self.assertEqual(router.calls[0][1], PROFILE)

    def test_admin_is_also_authorized(self) -> None:
        service = OrchestrationApiService(router=Router(proposal()), model_profile=PROFILE)
        decision = service.orchestrate(principal(Role.ADMIN), OrchestrationRequest(message="hi"))
        self.assertIs(decision.intent, OrchestrationIntent.ASSESSMENT)

    def test_rejects_non_principal(self) -> None:
        service = OrchestrationApiService(router=Router(proposal()), model_profile=PROFILE)
        with self.assertRaises(TypeError):
            service.orchestrate(object(), OrchestrationRequest(message="hi"))


class PolicyGroundingWiringTest(unittest.TestCase):
    """The service grounds by the JWT's customer, and only when material actually exists."""

    class Builder:
        def __init__(self, context):
            self.context = context
            self.calls: list[tuple[str, str]] = []

        def build(self, *, customer_id, question):
            self.calls.append((customer_id, question))
            return self.context

    @staticmethod
    def _qa_decision():
        return OrchestrationDecision(
            intent=OrchestrationIntent.POLICY_QA, rationale="policy question", answer="answer"
        )

    def test_material_is_built_for_the_callers_customer_and_handed_to_the_router(self) -> None:
        from apps.backend.policy.qa_context import PolicyQaContext

        router = Router(self._qa_decision())
        builder = self.Builder(
            PolicyQaContext(prompt_text="## 문서 1", document_count=1, excerpt_count=1)
        )
        OrchestrationApiService(
            router=router, model_profile=PROFILE, policy_context=builder
        ).orchestrate(principal(), OrchestrationRequest(message="사내 S3 정책"))
        self.assertEqual(builder.calls, [("cust", "사내 S3 정책")])
        self.assertEqual(router.contexts, ["## 문서 1"])

    def test_unavailable_material_is_passed_as_none(self) -> None:
        from apps.backend.policy.qa_context import PolicyQaContext

        router = Router(self._qa_decision())
        builder = self.Builder(
            PolicyQaContext(
                prompt_text="",
                document_count=0,
                excerpt_count=0,
                unavailable_reason="NO_READY_DOCUMENT",
            )
        )
        OrchestrationApiService(
            router=router, model_profile=PROFILE, policy_context=builder
        ).orchestrate(principal(), OrchestrationRequest(message="정책 나열"))
        self.assertEqual(router.contexts, [None])

    def test_without_a_builder_the_router_still_runs(self) -> None:
        router = Router(self._qa_decision())
        OrchestrationApiService(router=router, model_profile=PROFILE).orchestrate(
            principal(), OrchestrationRequest(message="정책 나열")
        )
        self.assertEqual(router.contexts, [None])

    def test_identity_checks_precede_any_document_read(self) -> None:
        """A caller that fails the boundary checks must not trigger a customer document read."""
        router = Router(self._qa_decision())
        builder = self.Builder(None)
        service = OrchestrationApiService(
            router=router, model_profile=PROFILE, policy_context=builder
        )
        with self.assertRaises(TypeError):
            service.orchestrate("not-a-principal", OrchestrationRequest(message="정책"))  # type: ignore[arg-type]
        self.assertEqual(builder.calls, [])


if __name__ == "__main__":
    unittest.main()
