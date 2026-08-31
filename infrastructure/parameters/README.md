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
| `LambdaCodeS3Key` | At least 1 character | Object key for the packaged Lambda ZIP |
| `AssessmentScopeJson` | JSON string; defaults to `{}` and is hidden with `NoEcho` | Fail-closed customer repository/profile selector map supplied by the approved deployment workflow |

The `ProjectName` and `Environment` maxima keep the longest derived bucket name within S3's
63-character limit. The stack uses the account ID pseudo-parameter to reduce global namespace
collisions; it is not supplied by the customer. Important physical names include:

- `${ProjectName}-${Environment}-metadata`
- `${ProjectName}-${Environment}-artifacts-${AWS::AccountId}`
- `${ProjectName}-${Environment}-assessment`
- `${ProjectName}-${Environment}-remediation`
- `${ProjectName}-${Environment}-deployment`

A non-sensitive parameter file may use this shape:

```json
[
  {"ParameterKey": "ProjectName", "ParameterValue": "example-platform"},
  {"ParameterKey": "Environment", "ParameterValue": "dev"},
  {"ParameterKey": "LambdaCodeS3Bucket", "ParameterValue": "example-deployment-artifacts"},
  {"ParameterKey": "LambdaCodeS3Key", "ParameterValue": "m0/example-sha.zip"},
  {"ParameterKey": "AssessmentScopeJson", "ParameterValue": "{}"}
]
```

## Deployment boundary

Local developers and Agent sessions validate the template but do not create or update the
stack. Customer-specific parameters, stack termination protection, Lambda packaging, and stack
updates belong to the approval-gated GitHub Actions OIDC deployment path. The template retains
the table, artifact bucket, bucket policy, and Cognito User Pool if the stack or a resource
replacement is requested.
