# ADR-0013: SQS-resumable Worker execution

## Context

Assessment, Remediation, and Deployment work must return from public APIs immediately, can
outlive a single Lambda invocation, and depends on AWS, Bedrock, GitHub, Terraform Plan, Apply,
and human approval. A Queue must not become a second source of workflow state, and Terraform
Apply cannot be retried as if it were a read-only operation.

## Decision

The Backend writes a revision-bound Job and publishes a minimal `WorkflowTask` to one of three
SQS Standard Queues: Assessment, Remediation, or Deployment. Each Queue invokes only its
least-privilege Worker Lambda. `WorkflowTask` contains only `job_id`, `expected_revision`, and
an approved command; DynamoDB is the authoritative Job/checkpoint store and S3 holds large
Artifacts.

Assessment splits work by resource. Workers use a 15-minute Lambda timeout and, with three
minutes remaining, conditionally persist a checkpoint and enqueue the next task. Duplicate or
stale deliveries fail the revision condition and cannot advance the Job.

Retryable AWS, Bedrock, S3, and GitHub failures receive at most three total attempts before the
DLQ and terminal `FAILED` Job state. Validation, scope, permission, and Contract errors do not
retry. Admin retry creates a new Job revision. Terraform Apply does not retry automatically:
ambiguous completion requires Terraform/AWS reconciliation, `MANUAL_REVIEW`, and a new approval.

GitHub Actions assumes a narrow OIDC Event role and emits Plan/Apply completion Events to
EventBridge. EventBridge routes them to the Deployment Queue so the Deployment Worker resumes
without a public client callback.

Parent Orchestrator/Policy Q&A remains synchronous with a 30-second budget. It never creates a
long-running Parent Job; complex questions must be narrowed.

## Consequences

CloudFormation must provision the three Queues, each DLQ, Worker event-source mapping, EventBridge
rule, and least-privilege IAM. Observability includes Queue age, DLQ depth, retries, checkpoints,
and stale-revision rejections. Queue visibility/DLQ retention values and alarm thresholds remain
an infrastructure implementation decision.
