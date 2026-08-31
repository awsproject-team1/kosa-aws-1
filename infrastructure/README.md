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

`cloudformation/m0-foundation.yaml` implements this M0 resource skeleton: metadata table,
private versioned artifact bucket, Assessment/Remediation/Deployment queues with DLQs, and
separate API/Workflow runtime roles. The Python adapters and Job HTTP boundary remain injected
so their unit tests require neither AWS credentials nor a locally deployed stack. Lambda/API
Gateway packaging and Cognito authorizer wiring are deliberately deferred to the deployment
integration slice. No stack is deployed from a local developer or Agent session.
