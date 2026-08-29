# ADR-0010: M0 platform foundation and Job ownership

## Context

M0 roles need a common storage and asynchronous Job boundary before CloudFormation, Lambda,
Policy Context, Assessment, and Remediation implementations can proceed independently. The
repository has an in-memory/testable Job lifecycle and customer-scoped DynamoDB adapter, but
the AWS table, lifecycle retention, index use, and API ownership need one shared decision.

## Decision

The customer-deployed M0 stack receives `ProjectName` and `Environment` CloudFormation
parameters and derives physical resource names as `<project>-<env>-<component>`. It creates
one on-demand DynamoDB metadata table and one private artifact bucket. The table enables
server-side encryption, PITR, `expires_at` TTL, and the documented GSI1–GSI3 indexes. Data is
retained by default; only terminal Job/checkpoint records get an `expires_at` 30 days after
their terminal transition.

The Backend is the sole owner of tenant identity, opaque IDs, timestamps, revisions, keys,
and TTL values. Clients provide approved scope selectors only. Job reads use the base-table
`CUSTOMER#{customer_id}` + `JOB#{job_id}` key after JWT-derived scope; GSI1 is never an
authorization mechanism. All Job state transitions use the persisted revision condition.

GSI2 is reserved for a future customer-scoped Job list endpoint and is not populated until
that endpoint and its repository query are implemented. GSI3 is populated by Assessments,
not the Job adapter.

## Consequences

B/C/D can reference `job_id`, approved scope, Artifact references, and immutable domain IDs
without depending on A's implementation branch. A must implement the CloudFormation table and
bucket, IAM roles, injected adapters, then API handlers in that order. The infrastructure
cannot grant customer-workload writes to Agent Runtime or the AWS Resource Tool, and no local
session may deploy the stack.
