"""Constrain generated Terraform patches to one approved IaC snapshot."""

from typing import Protocol

from packages.contracts import RemediationContext, RemediationPatch


class RemediationContractError(ValueError):
    """Raised when a generated patch is not bound to the requested finding and snapshot."""


class PatchGenerator(Protocol):
    def generate(self, *, context: RemediationContext) -> RemediationPatch: ...


class RemediationService:
    """Validate an injected generator output before GitHub integration is allowed to consume it."""

    def __init__(self, generator: PatchGenerator) -> None:
        if generator is None:
            raise TypeError("generator is required")
        self._generator = generator

    def generate(self, *, context: RemediationContext) -> RemediationPatch:
        if not isinstance(context, RemediationContext):
            raise TypeError("context must be a RemediationContext")
        finding_id = context.finding.finding_id
        snapshot = context.snapshot
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
