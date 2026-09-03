"""D execution port signatures for the M3 approved-apply boundary (ADR-0019).

These Protocols are the seam between A/C (who orchestrate and evaluate) and
D (who runs Terraform and calls GitHub). They are frozen here first, ahead of D's
live adapters, so A and C can build against Protocols and fixtures in parallel
rather than waiting for D's implementation (PROGRESS.md M3 ordering). D owns the
concrete adapters; A/C only depend on these signatures.

Every method returns a `packages.contracts` value type so no role imports across
another role's app package.
"""

from typing import Protocol, runtime_checkable

from packages.contracts import (
    ApplyDispatchReceipt,
    DeploymentApproval,
    PlanExecutionResult,
    RemediationPatch,
    TerraformPlan,
    TerraformStateVersion,
    WorkflowRunFacts,
    WorkflowRunReference,
)
from packages.contracts.remediation import RemediationSyncTarget


@runtime_checkable
class PlanRequestPort(Protocol):
    """Run a refreshed Terraform plan on the approved default-branch commit.

    D executes `terraform plan` against the merge commit (ADR-0019 §3), captures
    the allow-listed projection and its `plan_hash`, saves the binary plan, and
    records the plan-time state `(lineage, serial)`. The returned
    `PlanExecutionResult` binds all three to one `deployment_id`.
    """

    def request_plan(
        self,
        *,
        customer_id: str,
        deployment_id: str,
        repository_id: str,
        commit_sha: str,
    ) -> PlanExecutionResult: ...


@runtime_checkable
class ApplyDispatchPort(Protocol):
    """Dispatch the apply workflow for an approved, re-verified deployment.

    Called only after D re-checks the stored approval, `commit_sha`, `plan_hash`,
    and state version and wins the `APPROVED → APPLYING` conditional transition.
    The dispatch inputs are `deployment_id`, `commit_sha`, `plan_hash`, and the
    plan run's id; the workflow downloads and verifies that run's saved plan
    artifact (ADR-0019 §1, §5). Apply consumes the saved binary plan, never a
    re-computed plan.

    `plan_run` is passed rather than re-derived because apply runs in a later
    invocation than plan: the caller reloads it from the durable
    `PlanExecutionResult` and D never guesses which run produced the artifact.
    """

    def dispatch_apply(
        self,
        *,
        approval: DeploymentApproval,
        plan: TerraformPlan,
        state_version: TerraformStateVersion,
        plan_run: WorkflowRunReference,
    ) -> ApplyDispatchReceipt: ...


@runtime_checkable
class WorkflowRunReader(Protocol):
    """Re-read a GitHub Actions run so its facts are verified, not trusted.

    D never trusts EventBridge payloads. It re-reads the run by `run_id` and
    compares workflow path, repository, `ref`/commit, conclusion, and plan digest
    against the approved deployment; any mismatch routes to MANUAL_REVIEW rather
    than a retry (ADR-0019 §7).
    """

    def read_run(self, reference: WorkflowRunReference) -> WorkflowRunFacts: ...


@runtime_checkable
class ActualRereadPort(Protocol):
    """Re-read AWS Actual state after apply to feed Post-Deploy Verification.

    After a verified `APPLY_COMPLETED`, the verification Assessment re-evaluates
    the same planned coordinates against the post-apply Actual (ADR-0020). The
    target is the approved default-branch commit already bound to the deployment.
    """

    def reread_actual(
        self,
        *,
        customer_id: str,
        deployment_id: str,
        sync_target: RemediationSyncTarget,
    ) -> None: ...


@runtime_checkable
class DeploymentCommitResolver(Protocol):
    """Resolve the default-branch commit a `TERRAFORM_PATCH` deployment applies.

    ADR-0019 §3 fixes the apply target as the **merge commit on the default
    branch**, not the patch's base commit: the PR head's plan is CI reference only,
    and applying anything but merged code makes the later `DRIFT` perspective read a
    tree no human approved. §4 then makes "reachable from the default branch" a
    precondition of creating the deployment at all.

    Both questions are one GitHub read, so they are one method. `None` means the
    patch is not on the default branch yet — normally because nobody has merged the
    PR. That is an ordinary not-yet, not an error, so it is a return value: the
    caller reports `CONFLICT` and the customer merges when ready. We deliberately do
    not observe the customer's CI (§4); an unmerged PR simply fails this check.

    `ACTUAL_SYNC` does not use this port. Its target is already a default-branch
    commit that passed the `IAC` perspective (`RemediationSyncTarget.commit_sha`).
    """

    def resolve_default_branch_commit(
        self, *, customer_id: str, repository_id: str, patch: RemediationPatch
    ) -> str | None: ...
