"""Public boundary for natural-language routing through the Parent Orchestrator.

`POST /orchestrate` authorizes the caller, hands one natural-language message to the
Parent, and returns its non-authorizing decision. Per ADR-0012 this never starts a
workflow: a POLICY_QA decision carries a direct answer, and a workflow intent is a
proposal the client must confirm through the deterministic workflow endpoints
(POST /assessments, POST /findings/{id}/remediations, ...). Selectors the Parent
proposes are suggestions; the Backend re-validates them against the JWT scope when the
client later confirms.
"""

from typing import Protocol

from apps.backend.auth import Action, Principal, authorize
from apps.backend.policy.qa_context import PolicyQaContext
from packages.contracts import ModelProfile, OrchestrationDecision, OrchestrationRequest


class ParentRouter(Protocol):
    def route(
        self,
        request: OrchestrationRequest,
        *,
        model_profile: ModelProfile,
        policy_context: str | None = None,
    ) -> OrchestrationDecision: ...


class PolicyContextBuilder(Protocol):
    def build(self, *, customer_id: str, question: str) -> PolicyQaContext: ...


class OrchestrationApiService:
    """Authorize then delegate one natural-language turn to the Parent Orchestrator."""

    def __init__(
        self,
        *,
        router: ParentRouter,
        model_profile: ModelProfile,
        policy_context: PolicyContextBuilder | None = None,
    ) -> None:
        if router is None:
            raise TypeError("router is required")
        if not isinstance(model_profile, ModelProfile):
            raise TypeError("model_profile must be a ModelProfile")
        self._router = router
        self._model_profile = model_profile
        # Optional so a deployment without a policy bucket still routes; the Parent is then told
        # that no material exists and answers accordingly.
        self._policy_context = policy_context

    def orchestrate(
        self, principal: Principal, request: OrchestrationRequest
    ) -> OrchestrationDecision:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        if not isinstance(request, OrchestrationRequest):
            raise TypeError("request must be an OrchestrationRequest")
        authorize(principal, Action.ORCHESTRATE)
        # Grounding is keyed by the customer the JWT proved — the caller cannot choose it.
        material: str | None = None
        if self._policy_context is not None:
            context = self._policy_context.build(
                customer_id=principal.customer_id, question=request.message
            )
            material = context.prompt_text if context.available else None
        return self._router.route(
            request, model_profile=self._model_profile, policy_context=material
        )
