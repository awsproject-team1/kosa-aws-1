# M0 foundation parameters

`infrastructure/cloudformation/m0-foundation.json` requires explicit `ProjectName` and
`Environment` values. Do not commit customer names, AWS account identifiers, credentials, or
other sensitive deployment values.

## Parameters

| Parameter | Constraint | Purpose |
| --- | --- | --- |
| `ProjectName` | 3-40 lowercase letters, numbers, or hyphens; starts and ends with a letter or number; does not start with `xn--`, `sthree-`, or `amzn-s3-demo-` | Project/customer-qualified prefix that must be unique enough for the global S3 namespace |
| `Environment` | 2-12 lowercase letters, numbers, or hyphens; starts and ends with a letter or number | Deployment environment such as `dev`, `stage`, or `prod` |

The constraints reject S3-reserved prefixes and keep `${ProjectName}-${Environment}-artifacts`
within the 63-character S3 bucket-name limit. The approved values derive these physical names:

- `${ProjectName}-${Environment}-metadata`
- `${ProjectName}-${Environment}-artifacts`

A non-sensitive parameter file may use this shape:

```json
[
  {"ParameterKey": "ProjectName", "ParameterValue": "example-platform"},
  {"ParameterKey": "Environment", "ParameterValue": "dev"}
]
```

## Deployment boundary

Local developers and Agent sessions validate the template but do not create or update the
stack. Customer-specific parameters and stack termination protection belong to the approved
GitHub Actions deployment path. The template retains the table and bucket if the stack or a
resource replacement is requested.
