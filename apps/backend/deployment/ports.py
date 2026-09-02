"""D execution port signatures for the M3 approved-apply boundary (ADR-0019).

These four Protocols are the seam between A/C (who orchestrate and evaluate) and
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
    The dispatch input is `deployment_id`, `commit_sha`, `plan_hash`; the workflow
    resolves and verifies its own plan artifact (ADR-0019 §5). Apply consumes the
    saved binary plan, never a re-computed plan.
    """

    def dispatch_apply(
        self,
        *,
        approval: DeploymentApproval,
        plan: TerraformPlan,
        state_version: TerraformStateVersion,
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
