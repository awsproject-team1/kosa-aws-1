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

    def route(self, request, *, model_profile):
        self.calls.append((request, model_profile))
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


if __name__ == "__main__":
    unittest.main()
