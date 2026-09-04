"""The orchestrate API boundary authorizes then delegates without starting work."""

import unittest

from apps.backend.api.orchestration import OrchestrationApiService
from apps.backend.auth import Principal, Role
from packages.contracts import (
    AssessmentPhase,
    ModelProfile,
    ModelProfileRole,
    OrchestrationDecision,
    OrchestrationIntent,
    OrchestrationRequest,
    PolicyProfile,
    PolicyRule,
    PolicyRuleReference,
    RuleSeverity,
    SourceReference,
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
        self.contexts: list[str | None] = []

    def route(self, request, *, model_profile, policy_context=None):
        self.calls.append((request, model_profile))
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


def _source_reference() -> SourceReference:
    return SourceReference(
        source_id="isms-p",
        source_version="2023-10-31",
        locator="control/5.2.1",
        content_sha256="digest-001",
    )


def _rule(rule_id: str) -> PolicyRule:
    return PolicyRule(
        rule_id=rule_id,
        version="v1",
        title=f"{rule_id} title",
        severity=RuleSeverity.HIGH,
        applicable_phases=(AssessmentPhase.INITIAL,),
        resource_types=("AWS::S3::Bucket",),
        source_references=(_source_reference(),),
    )


class FakeCatalog:
    """Customer-scoped Profile/Rule reader double."""

    def __init__(self, customer_id: str) -> None:
        self.customer_id = customer_id
        self.profile = PolicyProfile(
            policy_profile_id="profile-internal-baseline",
            version="v1",
            rule_references=(PolicyRuleReference(rule_id="S3-PUBLIC-001", version="v1"),),
        )
        self.rules = {"S3-PUBLIC-001": _rule("S3-PUBLIC-001")}

    def get_profile(self, policy_profile_id, version=None):
        return self.profile if policy_profile_id == self.profile.policy_profile_id else None

    def get_rule(self, rule_id, version):
        rule = self.rules.get(rule_id)
        return rule if rule is not None and rule.version == version else None


def _policy_qa() -> OrchestrationDecision:
    return OrchestrationDecision(
        intent=OrchestrationIntent.POLICY_QA,
        rationale="asks about a control",
        answer="S3-PUBLIC-001 requires blocking public access.",
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

    def test_a_named_profile_grounds_the_router_in_that_profiles_rules(self) -> None:
        seen: list[str] = []

        def factory(customer_id: str) -> FakeCatalog:
            seen.append(customer_id)
            return FakeCatalog(customer_id)

        router = Router(_policy_qa())
        service = OrchestrationApiService(
            router=router, model_profile=PROFILE, catalog_factory=factory
        )
        service.orchestrate(
            principal(),
            OrchestrationRequest(
                message="what does our public access rule require?",
                policy_profile_id="profile-internal-baseline",
            ),
        )
        # The catalog is built from the caller's own customer, and the rule text reaches the router.
        self.assertEqual(seen, ["cust"])
        context = router.contexts[0]
        self.assertIsNotNone(context)
        self.assertIn("S3-PUBLIC-001", context)
        self.assertIn("profile-internal-baseline", context)

    def test_no_named_profile_means_no_grounding(self) -> None:
        router = Router(_policy_qa())
        service = OrchestrationApiService(
            router=router, model_profile=PROFILE, catalog_factory=FakeCatalog
        )
        service.orchestrate(principal(), OrchestrationRequest(message="hello"))
        self.assertIsNone(router.contexts[0])

    def test_an_unknown_profile_degrades_to_no_grounding(self) -> None:
        router = Router(_policy_qa())
        service = OrchestrationApiService(
            router=router, model_profile=PROFILE, catalog_factory=FakeCatalog
        )
        service.orchestrate(
            principal(),
            OrchestrationRequest(message="hi", policy_profile_id="no-such-profile"),
        )
        self.assertIsNone(router.contexts[0])

    def test_a_catalog_failure_does_not_break_the_turn(self) -> None:
        def factory(_customer_id: str):
            raise RuntimeError("dynamodb unavailable")

        router = Router(_policy_qa())
        service = OrchestrationApiService(
            router=router, model_profile=PROFILE, catalog_factory=factory
        )
        decision = service.orchestrate(
            principal(),
            OrchestrationRequest(message="hi", policy_profile_id="profile-internal-baseline"),
        )
        self.assertIs(decision.intent, OrchestrationIntent.POLICY_QA)
        self.assertIsNone(router.contexts[0])


if __name__ == "__main__":
    unittest.main()
