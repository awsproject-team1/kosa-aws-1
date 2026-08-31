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

Implementation order: CloudFormation resource skeleton and outputs → least-privilege IAM
roles/policies → injected DynamoDB/S3 adapters → API Gateway/Lambda handlers. No stack is
deployed from a local developer or Agent session.

## M0 foundation template

`cloudformation/m0-foundation.json` implements the storage foundation with:

- `MetadataTable`: on-demand DynamoDB with SSE, PITR, `expires_at` TTL, deletion protection,
  and `GSI1`-`GSI3` using `KEYS_ONLY` projections
- `ArtifactBucket`: AES256 server-side encryption, bucket-owner-enforced ownership, all
  public access blocks enabled, and a retained bucket policy that denies non-TLS requests
- `DeletionPolicy` and `UpdateReplacePolicy` set to `Retain` for both data resources and the
  bucket policy

The stack exposes `MetadataTableName`, `MetadataTableArn`, `ArtifactBucketName`, and
`ArtifactBucketArn` for the follow-up IAM and adapter wiring tasks. Parameter constraints and
a non-sensitive example are documented in `parameters/README.md`.

Run the offline CloudFormation and JSON syntax checks without deploying:

```bash
cfn-lint infrastructure/cloudformation/m0-foundation.json
python -m json.tool infrastructure/cloudformation/m0-foundation.json
```
