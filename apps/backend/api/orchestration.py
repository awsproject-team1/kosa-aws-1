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
from packages.contracts import ModelProfile, OrchestrationDecision, OrchestrationRequest


class ParentRouter(Protocol):
    def route(
        self, request: OrchestrationRequest, *, model_profile: ModelProfile
    ) -> OrchestrationDecision: ...


class OrchestrationApiService:
    """Authorize then delegate one natural-language turn to the Parent Orchestrator."""

    def __init__(self, *, router: ParentRouter, model_profile: ModelProfile) -> None:
        if router is None:
            raise TypeError("router is required")
        if not isinstance(model_profile, ModelProfile):
            raise TypeError("model_profile must be a ModelProfile")
        self._router = router
        self._model_profile = model_profile

    def orchestrate(
        self, principal: Principal, request: OrchestrationRequest
    ) -> OrchestrationDecision:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        if not isinstance(request, OrchestrationRequest):
            raise TypeError("request must be an OrchestrationRequest")
        authorize(principal, Action.ORCHESTRATE)
        return self._router.route(request, model_profile=self._model_profile)
