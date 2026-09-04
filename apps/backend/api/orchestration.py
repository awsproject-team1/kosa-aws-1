"""Public boundary for natural-language routing through the Parent Orchestrator.

`POST /orchestrate` authorizes the caller, hands one natural-language message to the
Parent, and returns its non-authorizing decision. Per ADR-0012 this never starts a
workflow: a POLICY_QA decision carries a direct answer, and a workflow intent is a
proposal the client must confirm through the deterministic workflow endpoints
(POST /assessments, POST /findings/{id}/remediations, ...). Selectors the Parent
proposes are suggestions; the Backend re-validates them against the JWT scope when the
client later confirms.

When the request names a Policy Profile, the Backend resolves that Profile's published
rules inside the caller's own customer partition and passes them to the Parent as
read-only grounding, so a Policy Q&A answer cites the customer's actual rules rather than
generic concepts. Resolution is best-effort: an unknown or unresolved Profile simply omits
the grounding and the turn still routes.
"""

from typing import Protocol

from apps.backend.auth import Action, Principal, authorize
from packages.contracts import (
    ModelProfile,
    OrchestrationDecision,
    OrchestrationRequest,
    PolicyProfile,
    PolicyRule,
)

#: Cap the number of rules rendered into the prompt so a large Profile cannot blow the model's
#: context window; the earliest rules in the Profile are the ones kept.
_MAX_CONTEXT_RULES = 40
#: Trim each free-text field so one verbose rule cannot crowd out the rest.
_MAX_FIELD_CHARS = 400


class ParentRouter(Protocol):
    def route(
        self,
        request: OrchestrationRequest,
        *,
        model_profile: ModelProfile,
        policy_context: str | None = None,
    ) -> OrchestrationDecision: ...


class PolicyCatalogReader(Protocol):
    """Read-only Profile/Rule lookup already scoped to one customer partition."""

    def get_profile(
        self, policy_profile_id: str, version: str | None = None
    ) -> PolicyProfile | None: ...

    def get_rule(self, rule_id: str, version: str) -> PolicyRule | None: ...


class OrchestrationApiService:
    """Authorize then delegate one natural-language turn to the Parent Orchestrator."""

    def __init__(
        self,
        *,
        router: ParentRouter,
        model_profile: ModelProfile,
        catalog_factory: object | None = None,
    ) -> None:
        if router is None:
            raise TypeError("router is required")
        if not isinstance(model_profile, ModelProfile):
            raise TypeError("model_profile must be a ModelProfile")
        if catalog_factory is not None and not callable(catalog_factory):
            raise TypeError("catalog_factory must be callable or None")
        self._router = router
        self._model_profile = model_profile
        # Builds a customer-scoped PolicyCatalogReader from a customer_id. Optional: when absent,
        # Policy Q&A still works but without rule grounding.
        self._catalog_factory = catalog_factory

    def orchestrate(
        self, principal: Principal, request: OrchestrationRequest
    ) -> OrchestrationDecision:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        if not isinstance(request, OrchestrationRequest):
            raise TypeError("request must be an OrchestrationRequest")
        authorize(principal, Action.ORCHESTRATE)
        policy_context = self._policy_context(principal, request)
        return self._router.route(
            request, model_profile=self._model_profile, policy_context=policy_context
        )

    def _policy_context(self, principal: Principal, request: OrchestrationRequest) -> str | None:
        """Render the caller's published Profile rules for the Parent, or None.

        Resolution stays inside the caller's own customer partition (the catalog is built from
        `principal.customer_id`), so a caller cannot read another customer's rules by naming their
        Profile. Any resolution failure degrades to no grounding rather than failing the turn:
        answering without the customer's rules is worse than a generic answer, but breaking the
        chatbot entirely is worse still.
        """
        if self._catalog_factory is None or request.policy_profile_id is None:
            return None
        try:
            catalog = self._catalog_factory(principal.customer_id)
            profile = catalog.get_profile(request.policy_profile_id)
            if profile is None:
                return None
            rules: list[PolicyRule] = []
            for reference in profile.rule_references[:_MAX_CONTEXT_RULES]:
                rule = catalog.get_rule(reference.rule_id, reference.version)
                if rule is not None:
                    rules.append(rule)
            if not rules:
                return None
            return _render_rules(profile, rules)
        except Exception:
            return None


def _render_rules(profile: PolicyProfile, rules: list[PolicyRule]) -> str:
    """Render resolved rules into a compact, deterministic text block for the prompt."""
    lines = [f"Policy Profile: {profile.policy_profile_id} (version {profile.version})", ""]
    for rule in rules:
        lines.append(f"- rule_id: {rule.rule_id} (version {rule.version})")
        lines.append(f"  title: {rule.title}")
        lines.append(f"  severity: {rule.severity.value}")
        lines.append(f"  resource_types: {', '.join(rule.resource_types)}")
        for label, value in (
            ("applicability", rule.applicability_semantics),
            ("evaluation_rubric", rule.evaluation_rubric),
            ("severity_guidance", rule.severity_guidance),
        ):
            if value:
                lines.append(f"  {label}: {_trim(value)}")
    return "\n".join(lines)


def _trim(value: str) -> str:
    return value if len(value) <= _MAX_FIELD_CHARS else value[:_MAX_FIELD_CHARS].rstrip() + "…"
