# Infrastructure

CloudFormation packaging, IAM definitions, and deployment parameters for the customer-deployed platform.

## M0 foundation contract

The first stack accepts `ProjectName` and `Environment`; it derives every physical name as
`<project>-<env>-<component>`. It creates one metadata DynamoDB table and one private
artifact S3 bucket. The table has on-demand capacity, encryption, point-in-time recovery,
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
