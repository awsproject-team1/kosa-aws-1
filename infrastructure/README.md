# Infrastructure

CloudFormation packaging, IAM definitions, and deployment parameters for the customer-deployed platform.

## M0 foundation contract

The first stack accepts `ProjectName` and `Environment`; it normally derives physical names as
`<project>-<env>-<component>`. The globally named artifact bucket is the explicit exception and
uses `<project>-<env>-artifacts-<account-id>`. The stack creates one metadata DynamoDB table and
one private artifact S3 bucket. The table has on-demand capacity, encryption, point-in-time recovery,
the `expires_at` TTL attribute, and `GSI1`–`GSI3` from `docs/DATABASE.md`.

The stack must retain data by default. It must not create customer-workload write permissions:
Agent Runtime and AWS Resource Tool roles are read-only, and Terraform write permissions stay
on the separately approved GitHub Actions OIDC Apply path.

`cloudformation/m0-foundation.yaml` implements the M0 resource skeleton: metadata table,
private versioned artifact bucket, Assessment/Remediation/Deployment queues with DLQs,
Cognito User Pool/client, HTTP API JWT authorizer, Job API Lambda, EventBridge-scheduled Outbox
sweeper Lambda, and Assessment SQS Worker Lambda. The worker is deliberately restricted to the
packaged synthetic S3 Fixture in M0. The HTTP API uses its `$default` stage with auto deployment,
and the User Pool is retained on stack deletion or replacement. `LambdaCodeS3Bucket` and
`LambdaCodeS3Key` must identify the versioned ZIP built by `scripts/package-m0-lambda.sh`; the
script verifies that application imports and the required `fixtures/m0/` files are present.
`.github/workflows/deploy-m0-foundation.yml` is the approval-gated manual GitHub Actions OIDC path
that uploads that ZIP to the versioning-enabled customer-owned bucket and deploys the stack. The
chosen GitHub Environment must be configured with required reviewers before a customer deployment.
No stack is deployed from a local developer or Agent session. `AssessmentScopeJson` is a fail-closed,
customer-scoped M0 selector map and must be supplied by the deployment workflow.

## Storage hardening and validation

The canonical YAML template protects the metadata table with deletion protection, SSE, PITR,
TTL, and retained replacement/deletion behavior. The account-qualified artifact bucket enables
AES256 encryption, versioning, bucket-owner-enforced ownership, all public-access blocks, and a
retained bucket policy that denies non-TLS requests to the bucket and its objects. Both storage
resources carry Project, Environment, and Component tags.

Artifact bucket object reads and writes are recorded by a retained single-region CloudTrail trail
with log-file validation. The trail selects only `AWS::S3::Object` data events under the artifact
bucket and delivers them to a separate retained, versioned, encrypted, private audit bucket. The
audit destination is deliberately excluded from the data selector to avoid recursive event
collection. CloudTrail data events add event-based cost and retain object-key metadata; sensitive
material must not be embedded in artifact object keys. The customer deployment role must permit
creation and update of the CloudTrail trail and audit destination, and a customer-approved sandbox
run must verify a controlled artifact Get/Put produces a delivered, validated trail record.

M0 Assessment Workers use packaged synthetic fixtures and receive no ArtifactBucket permission.
Before a Worker accesses customer artifacts, its runtime identity must be tenant-scoped as defined
in `docs/decisions/ADR-0014-artifact-audit-and-tenant-isolation.md`; a shared `customers/*` role
is not an acceptable tenant isolation boundary.

The stack exposes metadata table and artifact bucket names and ARNs for runtime injection and
least-privilege integrations. `ProjectName` and `Environment` constraints, including the
account-qualified S3 naming exception, are documented in `parameters/README.md` and
`docs/NAMING.md`. The non-deploying approval-input checklist and controlled sandbox CloudTrail
acceptance procedure are in `parameters/m0-foundation-sandbox-deployment-runbook.md`; they must be
completed through the protected GitHub Actions path, not from a local developer or Agent session.

Run the same offline CloudFormation validation used by CI without deploying:

```bash
cfn-lint --non-zero-exit-code error infrastructure/cloudformation/m0-foundation.yaml
```
