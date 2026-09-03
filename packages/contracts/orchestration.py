"""Parent Orchestrator natural-language routing contracts (ADR-0012).

The Parent is an LLM agent that reads one natural-language request and either answers
a Policy Q&A directly or proposes a workflow intent with candidate selectors. It never
creates a Job, validates scope, approves a deployment, or changes AWS: the Backend
validates the proposed selectors against the caller's JWT and requires explicit user
confirmation before an Assessment, Remediation, or Deployment starts.

So this module models a *proposal*, not an action. The routing decision carries at most
a suggested intent and candidate selectors; committing to a workflow remains the
deterministic Backend path (POST /assessments, POST /findings/{id}/remediations, ...).
"""

from dataclasses import dataclass
from enum import StrEnum

from packages.contracts._validation import (
    require_non_empty_string,
    require_optional_non_empty_string,
)


class OrchestrationIntent(StrEnum):
    """What the Parent believes the natural-language request wants.

    POLICY_QA is answered by the Parent itself. The three workflow intents are only
    *proposals*; the Backend still validates selectors and requires confirmation.
    """

    POLICY_QA = "POLICY_QA"
    ASSESSMENT = "ASSESSMENT"
    REMEDIATION = "REMEDIATION"
    DEPLOYMENT = "DEPLOYMENT"
    # The request could not be mapped to a supported intent within the boundary.
    UNSUPPORTED = "UNSUPPORTED"


_WORKFLOW_INTENTS = frozenset(
    {
        OrchestrationIntent.ASSESSMENT,
        OrchestrationIntent.REMEDIATION,
        OrchestrationIntent.DEPLOYMENT,
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class OrchestrationRequest:
    """One natural-language turn addressed to the Parent Orchestrator."""

    message: str

    def __post_init__(self) -> None:
        require_non_empty_string(self.message, "message")

    def to_dict(self) -> dict[str, object]:
        return {"message": self.message}


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkflowSelectorCandidate:
    """A candidate selector the Parent proposes for a workflow intent.

    These are *suggestions* extracted from the request, not authorized targets. The
    Backend validates them against the JWT-derived customer scope before anything runs.
    """

    repository_id: str | None = None
    policy_profile_id: str | None = None
    finding_id: str | None = None
    remediation_id: str | None = None

    def __post_init__(self) -> None:
        require_optional_non_empty_string(self.repository_id, "repository_id")
        require_optional_non_empty_string(self.policy_profile_id, "policy_profile_id")
        require_optional_non_empty_string(self.finding_id, "finding_id")
        require_optional_non_empty_string(self.remediation_id, "remediation_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "repository_id": self.repository_id,
            "policy_profile_id": self.policy_profile_id,
            "finding_id": self.finding_id,
            "remediation_id": self.remediation_id,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class OrchestrationDecision:
    """The Parent's non-authorizing result for one request.

    - POLICY_QA: ``answer`` is present; ``selector`` is absent; no workflow starts.
    - ASSESSMENT/REMEDIATION/DEPLOYMENT: a *proposal*. ``selector`` carries candidate
      selectors and ``requires_confirmation`` is always True — the Backend must validate
      scope against the JWT and get user confirmation before starting the workflow.
    - UNSUPPORTED: neither an answer nor a workflow; ``rationale`` explains why.
    """

    intent: OrchestrationIntent
    rationale: str
    answer: str | None = None
    selector: WorkflowSelectorCandidate | None = None
    requires_confirmation: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.intent, OrchestrationIntent):
            raise TypeError("intent must be an OrchestrationIntent")
        require_non_empty_string(self.rationale, "rationale")
        require_optional_non_empty_string(self.answer, "answer")
        if self.selector is not None and not isinstance(self.selector, WorkflowSelectorCandidate):
            raise TypeError("selector must be a WorkflowSelectorCandidate or None")
        if not isinstance(self.requires_confirmation, bool):
            raise TypeError("requires_confirmation must be a bool")

        if self.intent is OrchestrationIntent.POLICY_QA:
            # Policy Q&A is answered directly; proposing a workflow here would blur the
            # boundary between answering and acting.
            require_non_empty_string(self.answer, "answer")
            if self.selector is not None:
                raise ValueError("POLICY_QA decision must not carry a workflow selector")
            if self.requires_confirmation:
                raise ValueError("POLICY_QA decision does not require confirmation")
        elif self.intent in _WORKFLOW_INTENTS:
            # A workflow intent is a proposal the Backend must confirm; it never carries
            # a direct answer and always needs confirmation before anything runs.
            if self.answer is not None:
                raise ValueError("a workflow proposal must not carry a Policy Q&A answer")
            if not self.requires_confirmation:
                raise ValueError("a workflow proposal must require confirmation")
        else:  # UNSUPPORTED
            if self.answer is not None or self.selector is not None:
                raise ValueError("an unsupported request carries neither answer nor selector")
            if self.requires_confirmation:
                raise ValueError("an unsupported request does not require confirmation")

    @property
    def is_workflow_proposal(self) -> bool:
        return self.intent in _WORKFLOW_INTENTS

    def to_dict(self) -> dict[str, object]:
        return {
            "intent": self.intent.value,
            "rationale": self.rationale,
            "answer": self.answer,
            "selector": None if self.selector is None else self.selector.to_dict(),
            "requires_confirmation": self.requires_confirmation,
        }
