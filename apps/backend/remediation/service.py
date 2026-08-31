"""Constrain generated Terraform patches to one approved IaC snapshot."""

from typing import Protocol

from packages.contracts import IaCSnapshot, RemediationPatch


class RemediationContractError(ValueError):
    """Raised when a generated patch is not bound to the requested finding and snapshot."""


class PatchGenerator(Protocol):
    def generate(self, *, finding_id: str, snapshot: IaCSnapshot) -> RemediationPatch: ...


class RemediationService:
    """Validate an injected generator output before GitHub integration is allowed to consume it."""

    def __init__(self, generator: PatchGenerator) -> None:
        if generator is None:
            raise TypeError("generator is required")
        self._generator = generator

    def generate(self, *, finding_id: str, snapshot: IaCSnapshot) -> RemediationPatch:
        if not isinstance(finding_id, str) or not finding_id.strip():
            raise ValueError("finding_id must be a non-empty string")
        if not isinstance(snapshot, IaCSnapshot):
            raise TypeError("snapshot must be an IaCSnapshot")
        patch = self._generator.generate(finding_id=finding_id, snapshot=snapshot)
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
