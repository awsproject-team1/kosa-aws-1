# M0 foundation parameters

`infrastructure/cloudformation/m0-foundation.yaml` accepts deployment-specific values. Do not
commit customer names, AWS account identifiers, credentials, approved scope maps, or other
sensitive deployment values.

## Parameters

| Parameter | Constraint | Purpose |
| --- | --- | --- |
| `ProjectName` | 2-31 lowercase letters, digits, or hyphens; starts with a letter, ends with a letter or digit, and does not start with `xn--`, `sthree-`, or `amzn-s3-demo-` | Project/customer-qualified resource prefix |
| `Environment` | 2-8 lowercase letters, digits, or hyphens; starts with a letter and ends with a letter or digit | Deployment environment such as `dev`, `stage`, or `prod` |
| `LambdaCodeS3Bucket` | At least 3 characters | Versioning-enabled deployment-artifact bucket containing the packaged Lambda ZIP |
| `LambdaCodeS3Key` | At least 1 character | Commit-qualified object key for the packaged Lambda ZIP |
| `LambdaCodeS3ObjectVersion` | At least 1 character | Exact S3 Version ID returned by the approved conditional ZIP upload and pinned by every Lambda function |
| `AssessmentScopeJson` | JSON string; defaults to `{}` and is hidden with `NoEcho` | Fail-closed customer repository/profile selector map supplied by the approved deployment workflow |

The `ProjectName` and `Environment` maxima keep the longest derived bucket name within S3's
63-character limit. The stack uses the account ID pseudo-parameter to reduce global namespace
collisions; it is not supplied by the customer. Important physical names include:

- `${ProjectName}-${Environment}-metadata`
- `${ProjectName}-${Environment}-artifacts-${AWS::AccountId}`
- `${ProjectName}-${Environment}-audit-${AWS::AccountId}` (CloudTrail artifact-access audit destination)
- `${ProjectName}-${Environment}-assessment`
- `${ProjectName}-${Environment}-remediation`
- `${ProjectName}-${Environment}-deployment`

A non-sensitive parameter file may use this shape. Placeholder values must be replaced only by the
approval-gated workflow; customer values are not committed.

```json
[
  {"ParameterKey": "ProjectName", "ParameterValue": "example-platform"},
  {"ParameterKey": "Environment", "ParameterValue": "dev"},
  {"ParameterKey": "LambdaCodeS3Bucket", "ParameterValue": "example-deployment-artifacts"},
  {"ParameterKey": "LambdaCodeS3Key", "ParameterValue": "lambda/m0/example-commit-sha.zip"},
  {"ParameterKey": "LambdaCodeS3ObjectVersion", "ParameterValue": "example-s3-version-id"},
  {"ParameterKey": "AssessmentScopeJson", "ParameterValue": "{}"}
]
```

## Deployment boundary

Local developers and Agent sessions validate the template but do not create or update the stack.
Customer-specific parameters, stack termination protection, Lambda packaging, and stack updates
belong to the approval-gated GitHub Actions OIDC deployment path. The artifact-preparation and
artifact-deployment Environments must be distinct, require reviewers, and define the same
`EXPECTED_AWS_ACCOUNT_ID`. Both jobs validate the role ARN and assumed STS identity; S3 operations
also validate the deployment-artifact bucket owner.

The preparation job computes the Lambda ZIP SHA-256 and conditionally creates the commit-qualified
object key in a versioning-enabled bucket. A rerun reuses the current version only when its checksum,
commit metadata, ZIP-hash metadata, and Version ID exactly match the rebuilt artifact. The separate
artifact-deployment Environment then requires human approval of the commit SHA, object key, ZIP
SHA-256, and S3 Version ID. After approval, the deployment job revalidates that exact version before
passing it to every Lambda function. Customer account and bucket values are never committed.

The template retains the table, artifact bucket, artifact audit bucket, both bucket policies,
CloudTrail audit trail, and Cognito User Pool if the stack or a resource replacement is requested.
The customer-approved deployment role must allow the template's CloudTrail and S3 audit-destination
resources.
