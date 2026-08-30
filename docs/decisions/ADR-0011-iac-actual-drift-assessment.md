# ADR-0011: Separate IaC, actual-state, and drift assessment perspectives

## Context

Terraform configuration is the desired state, but the actual customer AWS state can differ
because of console changes, legacy resources, failed deployments, or unmanaged resources.
Combining the two inputs into one opaque Initial Assessment result can hide either a policy
violation or a configuration drift, and leaves unclear whether a remediation should change
IaC, AWS, or both.

## Decision

For Terraform-managed resources, Initial Assessment produces separate `Resource × Rule`
results for `IAC`, `AWS_ACTUAL`, and `DRIFT` perspectives. Each result records its own
Evidence Reference. A drift result indicates that the desired IaC and observed AWS state do
not agree; it does not grant the AI or platform a direct customer-workload write capability.

When IaC is unsafe, remediation changes it to the approved secure desired state. When IaC is
already secure and only AWS Actual has drifted, the existing IaC commit is the synchronization
target and no Patch is created. Deployment Readiness runs a refreshing Terraform Plan against
the current AWS state and can block or require revision of the Patch or synchronization target.
Only the approved GitHub Actions OIDC Apply changes AWS Actual.
Post-Deploy Verification rechecks Actual Compliance and Drift. Resources outside Terraform
management, or without a safe IaC-to-AWS mapping, result in `MANUAL_REVIEW` rather than an
automated Patch.

## Consequences

Assessment consumers must preserve the evaluation perspective and evidence for each result.
Golden fixtures cover each perspective as the implementation grows. A separate Drift entity
is not introduced for M0; results and Findings carry the perspective. If query volume or
drift lifecycle needs require it later, the DynamoDB model can add a dedicated entity through
a versioned Contract change.
