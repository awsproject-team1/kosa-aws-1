"""LangGraph wiring for the Parent Orchestrator (ADR-0012).

This is the thin orchestration shell the design names "LangGraph Parent". The routing
judgment lives in ``ParentOrchestrator`` (agent/agents/parent_orchestrator.py); this
module only composes it into a StateGraph so the natural-language entry has one graph
with an explicit classify → route → terminal shape. The graph performs no authorization
and starts no workflow: it returns the same non-authorizing ``OrchestrationDecision``.

The graph classifies once and then routes to a terminal node per intent. The terminal
nodes are deliberately trivial — Policy Q&A is already answered inside the decision, and
workflow intents are proposals the Backend must confirm — but keeping them as distinct
graph nodes makes the ADR-0012 routing (POLICY_QA vs ASSESSMENT/REMEDIATION/DEPLOYMENT
vs UNSUPPORTED) an explicit, inspectable structure rather than a hidden branch.
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from agent.agents.parent_orchestrator import ParentOrchestrator
from packages.contracts import (
    ModelProfile,
    OrchestrationDecision,
    OrchestrationIntent,
    OrchestrationRequest,
)


class ParentState(TypedDict, total=False):
    """Graph state threaded from the request to the routing decision."""

    request: OrchestrationRequest
    model_profile: ModelProfile
    decision: OrchestrationDecision


def build_parent_graph(orchestrator: ParentOrchestrator):
    """Compile the classify → route → terminal graph around one orchestrator.

    The compiled graph's ``invoke`` takes ``{"request", "model_profile"}`` and returns a
    state whose ``decision`` is the ``OrchestrationDecision``. Routing to a terminal node
    is driven by the classified intent, mirroring ADR-0012's four outcomes.
    """
    if not isinstance(orchestrator, ParentOrchestrator):
        raise TypeError("orchestrator must be a ParentOrchestrator")

    def classify(state: ParentState) -> ParentState:
        decision = orchestrator.route(state["request"], model_profile=state["model_profile"])
        return {"decision": decision}

    def _terminal(state: ParentState) -> ParentState:
        # The decision is already complete; terminal nodes exist to make each ADR-0012
        # outcome an explicit graph endpoint without mutating the non-authorizing result.
        return {}

    def route(state: ParentState) -> str:
        return state["decision"].intent.value

    graph = StateGraph(ParentState)
    graph.add_node("classify", classify)
    graph.add_node("policy_qa", _terminal)
    graph.add_node("propose_workflow", _terminal)
    graph.add_node("unsupported", _terminal)
    graph.add_edge(START, "classify")
    graph.add_conditional_edges(
        "classify",
        route,
        {
            OrchestrationIntent.POLICY_QA.value: "policy_qa",
            OrchestrationIntent.ASSESSMENT.value: "propose_workflow",
            OrchestrationIntent.REMEDIATION.value: "propose_workflow",
            OrchestrationIntent.DEPLOYMENT.value: "propose_workflow",
            OrchestrationIntent.UNSUPPORTED.value: "unsupported",
        },
    )
    for terminal in ("policy_qa", "propose_workflow", "unsupported"):
        graph.add_edge(terminal, END)
    return graph.compile()
