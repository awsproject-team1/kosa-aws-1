"""Constrain generated Terraform patches to one approved IaC snapshot."""

from typing import Protocol

from packages.contracts import (
    RemediationAction,
    RemediationContext,
    RemediationDecision,
    RemediationPatch,
)


class RemediationContractError(ValueError):
    """Raised when a generated patch is not bound to the requested finding and snapshot."""


class RemediationNotAutomatableError(ValueError):
    """The policy context requires review and must not enter patch generation."""


class PatchGenerator(Protocol):
    def generate(self, *, context: RemediationContext) -> RemediationPatch: ...


class RemediationService:
    """Validate an injected generator output before GitHub integration is allowed to consume it."""

    def __init__(self, generator: PatchGenerator) -> None:
        if generator is None:
            raise TypeError("generator is required")
        self._generator = generator

    def generate(
        self, *, context: RemediationContext, decision: RemediationDecision
    ) -> RemediationPatch:
        if not isinstance(context, RemediationContext):
            raise TypeError("context must be a RemediationContext")
        if not isinstance(decision, RemediationDecision):
            raise TypeError("decision must be a RemediationDecision")
        finding_id = context.finding.finding_id
        snapshot = context.snapshot
        if decision.finding_id != finding_id:
            raise RemediationContractError("remediation decision is outside context")
        if decision.action is not RemediationAction.TERRAFORM_PATCH:
            raise RemediationContractError("remediation decision does not permit a Terraform patch")
        patch = self._generator.generate(context=context)
        if not isinstance(patch, RemediationPatch):
            raise RemediationContractError("generator must return a RemediationPatch")
        if patch.finding_id != finding_id:
            raise RemediationContractError("patch finding_id is outside request context")
        if patch.base_commit_sha != snapshot.commit_sha:
            raise RemediationContractError("patch is not bound to the IaC snapshot commit")
        if patch.artifact.customer_id != snapshot.customer_id:
            raise RemediationContractError("patch artifact customer scope does not match snapshot")
        if patch.artifact.repository_id != snapshot.repository_id:
            raise RemediationContractError(
                "patch artifact repository scope does not match snapshot"
            )
        return patch
