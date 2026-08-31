# M1 customer sandbox actual-integration runbook

This runbook enables the live M1 Worker path. It does not authorize local AWS
commands or direct production access; dispatch the protected GitHub Actions
workflow only after customer review.

## Protected deployment Environment

In the second, artifact-deployment GitHub Environment, add these three Secrets.
They are intentionally not workflow-dispatch inputs and must not be copied to
issues, PRs, or repository files.

| Secret | Value |
| --- | --- |
| `M1_ASSESSMENT_RUNTIME_JSON` | JSON array of approved targets shown below |
| `M1_ASSESSMENT_SECRET_ARNS` | Comma-separated exact ARNs for the two target Secrets |
| `M1_ASSESSMENT_READ_ROLE_ARNS` | Comma-separated exact customer AWS read Role ARNs |

For one S3 target, the configuration JSON shape is:

```json
[
  {
    "customer_id": "<Cognito customer claim>",
    "repository_id": "<product repository ID>",
    "policy_profile_id": "profile-mvp-baseline",
    "commit_sha": "<reviewed 40-character Git commit SHA>",
    "github_repository": "<owner>/<repository>",
    "github_token_secret_id": "<GitHub installation-token Secret ARN>",
    "aws_account_id": "<12-digit customer AWS account ID>",
    "aws_read_role_arn": "<exact customer cross-account read Role ARN>",
    "aws_external_id_secret_id": "<External ID Secret ARN>",
    "s3_bucket_id": "<approved sandbox bucket name>"
  }
]
```

The GitHub Secret contains a short-lived GitHub App installation token with
`Contents: Read-only` on the one configured repository. The customer must renew
it before expiry through its App-token rotation process; neither a PAT nor the
GitHub App private key is accepted by this Worker configuration. The External
ID Secret contains only the random External ID required by the customer read
Role trust policy.

## Required customer controls

- The GitHub App is installed only on `github_repository`, with Contents read
  permission and no write/PR permissions.
- The customer AWS read Role trusts only the platform Worker Role, requires the
  configured External ID, and permits S3 read operations required by the
  selected bucket. It has no write permissions.
- The deployment Role is allowed to pass the three exact values into the stack;
  the Worker Role is allowed to read only the listed Secrets, assume only the
  listed read Roles, and invoke only the approved Nova Lite model.
- `AssessmentScopeJson` independently allows the same
  customer/repository/profile tuple at the API boundary.

## Actual E2E

1. Merge the reviewed `dev` revision and dispatch **Deploy M0 Foundation**.
2. Approve artifact preparation, review its immutable package evidence, then
   approve the separate deployment Environment.
3. Create the controlled Cognito `User` as described in
   [M1-AUTH-FRONTEND-TEST.md](M1-AUTH-FRONTEND-TEST.md).
4. In the SPA, log in and submit the configured repository ID and policy profile.
5. Verify the report shows an `AWS_ACTUAL` result, coverage 100%, and any
   persisted Findings/Readiness Score. Record only customer-controlled evidence
   references and the workflow run URL.

If the configured commit, repository, customer, account, Role, or secret does
not match, the Worker fails closed. It never falls back to an unconfigured
repository or account in live M1 mode.
