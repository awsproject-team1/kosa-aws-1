# Infrastructure

CloudFormation packaging, IAM definitions, and deployment parameters for the customer-deployed platform.

## Customer-operated M1 bootstrap

`cloudformation/m1-customer-bootstrap.yaml` is the one-time stack a customer
administrator runs in the target sandbox account before any repository workflow
can access it. It creates a private, versioned Lambda-code bucket, a GitHub
Actions OIDC deployment role, and a CloudFormation execution role. The GitHub
role trusts only the two exact protected Environment subjects supplied to the
bootstrap and can upload only `lambda/m0/*`, manage the one declared foundation
stack, and pass only the execution role to CloudFormation. It has no customer
workload, Secrets Manager, Bedrock, or `sts:AssumeRole` permission.

The bootstrap supports an existing account-level GitHub OIDC provider through
`ExistingGitHubOidcProviderArn`; otherwise it creates one. A customer must run
this stack with its own approved administrator path. It is not run by a local
Agent session or by the repository workflow it enables. Its outputs are the
only AWS identifiers needed by `Deploy M0 Foundation`; see
`docs/M1-SANDBOX-INTEGRATION.md` for the complete M1 sequence.

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
and the User Pool is retained on stack deletion or replacement. `LambdaCodeS3Bucket`,
`LambdaCodeS3Key`, and `LambdaCodeS3ObjectVersion` must identify the exact versioned ZIP built by
`scripts/package-m0-lambda.sh`; the package-validation and deployment workflows pin Python 3.12,
Git attributes normalize text files under `apps/`, `packages/`, and `fixtures/` to LF, and the
script writes sorted entries with fixed metadata for a deterministic hash. It verifies application
imports and the required `fixtures/m0/` files inside the same Python process without an early-exit
shell pipeline. `.github/workflows/deploy-m0-foundation.yml` is the approval-gated
manual GitHub Actions OIDC path. Its preparation job validates the expected AWS account against the
role ARN, STS identity, and artifact-bucket owner; computes the ZIP SHA-256; and conditionally creates
or verifies the matching commit-qualified object version. A second, distinct protected Environment
then requires reviewers to approve that exact commit/key/hash/Version ID before the deployment job
revalidates and pins it in every Lambda resource. Both Environments must be configured with required
reviewers and the same `EXPECTED_AWS_ACCOUNT_ID`. No stack is deployed from a local developer or Agent
session. `AssessmentScopeJson` is a fail-closed, customer-scoped M0 selector map and must be supplied
by the deployment workflow.

Frontend releases use the separate `.github/workflows/deploy-frontend.yml` workflow. It builds
the Vite SPA at an immutable commit, records the archive and `index.html` SHA-256 values, and pauses
at the protected deployment Environment before assuming a frontend-only OIDC role. That role can
write only the configured private SPA bucket and invalidate only the configured CloudFront
distribution; it cannot mutate the foundation stack, API, Cognito, DynamoDB, or runtime roles.
The foundation deploy creates this role only when the protected Environment supplies both
`FRONTEND_SPA_BUCKET_NAME` and `FRONTEND_DISTRIBUTION_ID`.

For M1 sandbox frontend testing, the stack also creates Cognito `Admin`/`User` groups and a
Hosted UI domain with Authorization Code OAuth enabled. The customer-operated local-user
handoff and PKCE frontend test are documented in `docs/M1-AUTH-FRONTEND-TEST.md`; no user
password is a CloudFormation parameter or repository value.

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
cfn-lint --non-zero-exit-code error \
  infrastructure/cloudformation/m0-foundation.yaml \
  infrastructure/cloudformation/m1-customer-bootstrap.yaml
```
