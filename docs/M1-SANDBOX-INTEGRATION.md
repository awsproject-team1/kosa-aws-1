# M1 customer sandbox actual-integration runbook

This runbook enables the live M1 Worker path. It does not authorize local AWS
commands or direct production access; dispatch the protected GitHub Actions
workflow only after customer review.

## 0. Customer-operated bootstrap

Before configuring a repository workflow, the customer administrator runs
`infrastructure/cloudformation/m1-customer-bootstrap.yaml` once in the target
AWS sandbox account using the customer's approved Console, CloudFormation, or
customer CI path. The bootstrap is deliberately separate from GitHub Actions:
it establishes the first trust boundary rather than assuming one already
exists.

Supply these non-secret parameters:

| Parameter | Value |
| --- | --- |
| `ProjectName` | The lowercase project name later used by the foundation stack. |
| `PlatformEnvironment` | Short foundation environment, normally `sandbox`. |
| `FoundationStackName` | Exact stack name that the deployment role may manage. |
| `GitHubOidcSubjectPrefix` | `repo:<owner>/<repository>` for the repository running this workflow; use the immutable owner/repository-ID form when the repository uses immutable OIDC subjects. |
| `ArtifactPreparationEnvironment` | `customer-sandbox-artifact` unless the customer chose a different protected Environment. |
| `DeploymentApprovalEnvironment` | `customer-sandbox-deploy` unless the customer chose a different protected Environment. |
| `ExistingGitHubOidcProviderArn` | Leave empty only when the account has no GitHub OIDC provider; otherwise use the existing provider ARN. |

The bootstrap outputs `GitHubActionsDeploymentRoleArn`,
`FoundationExecutionRoleArn`, and `LambdaCodeBucketName`. Keep these values in
the customer deployment record. It does not create the workload repository,
GitHub App token, customer read role, or application secrets.

## Protected deployment Environment

In the current repository, create the two GitHub Environments specified in the
bootstrap. Both require reviewers and define the same non-secret
`EXPECTED_AWS_ACCOUNT_ID` Environment variable. The bootstrap trust allows only
these two Environment OIDC subjects to assume the deployment role.

The second, artifact-deployment GitHub Environment also defines the non-secret
`M1_ASSESSMENT_MODE` variable. Set it explicitly to `live` for this runbook or
`fixture` only for an intentionally synthetic deployment. A missing or unknown
mode is rejected before customer deployment credentials are configured. The
artifact-preparation identity is a separate, earlier approval boundary used only
to create or verify the immutable package. In `fixture` mode all three M1 Secrets
below must be absent; in `live` mode all three are mandatory.

| Variable | Value |
| --- | --- |
| `EXPECTED_AWS_ACCOUNT_ID` | Same approved 12-digit account ID in both Environments |
| `M1_ASSESSMENT_MODE` | `live` in the deployment Environment for customer integration |

In the second Environment, add these three Secrets for `live` mode. They are
intentionally not workflow-dispatch inputs and must not be copied to issues,
PRs, workflow logs, or repository files.

| Secret | Value |
| --- | --- |
| `M1_ASSESSMENT_RUNTIME_JSON` | JSON array of approved targets shown below |
| `M1_ASSESSMENT_SECRET_ARNS` | Comma-separated exact union of the target credential Secret ARNs |
| `M1_ASSESSMENT_READ_ROLE_ARNS` | Comma-separated exact customer AWS read Role ARNs |

Deployment and remediation write paths need two more Secrets on the same Environment. They are
all-or-none: the workflow refuses one without the other before calling CloudFormation, and the
template Rule `DeploymentCommitResolutionAllOrNone` asserts the same. Leaving both empty keeps
`TERRAFORM_PATCH` pull-request write and apply-target commit resolution fail-closed;
`ACTUAL_SYNC` still works.

| Secret | Value |
| --- | --- |
| `DEPLOYMENT_RUNTIME_JSON` | JSON array with exactly one approved deployment target (customer/repository/full name, GitHub token Secret ARN, AWS account, read Role ARN, external-id Secret ARN, resource types) |
| `DEPLOYMENT_GITHUB_SECRET_ARNS` | Comma-separated Secrets Manager ARNs holding that repository's GitHub token |

Three non-secret Environment variables complete the deployment. Without them the authoring
worker has no approved model (candidate extraction stops at configuration) and the Cognito
Hosted UI keeps its `http://localhost:5173` callback.

| Variable | Value |
| --- | --- |
| `POLICY_AUTHORING_MODEL_PROFILE_JSON` | Approved `POLICY_AUTHORING` model profile as JSON |
| `FRONTEND_CALLBACK_URL` | Exact HTTPS callback URL of the deployed SPA |
| `FRONTEND_LOGOUT_URL` | Exact HTTPS logout URL of the deployed SPA |

Deployment credentials themselves are **not** stored: the workflow assumes the customer's
GitHub OIDC deployment role (`permissions: id-token: write` plus `role-to-assume`), and both
jobs re-verify the STS caller account against `EXPECTED_AWS_ACCOUNT_ID`. No long-lived AWS
access key exists in this repository or its Environments.

For one S3 target, the configuration JSON shape is:

```json
[
  {
    "customer_id": "<Cognito customer claim>",
    "repository_id": "<product repository ID>",
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

To evaluate more than one resource, or a type other than S3, replace `s3_bucket_id`
with an explicit `resources` list. A target declares its resources one way or the
other, never both:

```json
[
  {
    "customer_id": "<Cognito customer claim>",
    "repository_id": "<product repository ID>",
    "commit_sha": "<reviewed 40-character Git commit SHA>",
    "github_repository": "<owner>/<repository>",
    "github_token_secret_id": "<GitHub installation-token Secret ARN>",
    "aws_account_id": "<12-digit customer AWS account ID>",
    "aws_read_role_arn": "<exact customer cross-account read Role ARN>",
    "aws_external_id_secret_id": "<External ID Secret ARN>",
    "resources": [
      {"resource_type": "AWS::S3::Bucket", "resource_id": "<approved bucket name>"},
      {"resource_type": "AWS::EC2::Instance", "resource_id": "<i-…>"},
      {"resource_type": "AWS::RDS::DBInstance", "resource_id": "<DB instance identifier>"},
      {
        "resource_type": "AWS::ElasticLoadBalancingV2::LoadBalancer",
        "resource_id": "<load balancer ARN>"
      }
    ]
  }
]
```

`resource_type` must be one of those four; a type without an Actual read adapter is
rejected before deployment. The public Assessment API accepts only the Repository and
Policy Profile. Its Worker expands that request over every resource in this protected
list and stores one complete immutable evaluation plan. An internal or legacy Initial Assessment
record may narrow evaluation by naming one approved `resource_type`/`resource_id`; a
resource outside the list is refused at read time. The cross-account read Role therefore
needs read permission for every configured type (for example
`ec2:DescribeInstances`/`DescribeVolumes`/`DescribeSecurityGroups`,
`rds:DescribeDBInstances` plus `ec2:DescribeSecurityGroups` for the ingress rules of
attached RDS VPC security groups, and
`elasticloadbalancing:DescribeLoadBalancers`/`DescribeListeners`/`DescribeLoadBalancerAttributes`)
and nothing more.

`github_repository` is a canonical GitHub path identity, not a URL. The owner is
1–39 ASCII alphanumeric characters with optional single internal hyphens. The
repository is 1–100 ASCII letters, digits, `.`, `_`, or `-`, includes at least
one alphanumeric character, and is not `.` or `..`. Whitespace, control
characters, query/fragment text, escapes, backslashes, and extra path segments
are rejected before credentials are configured and again by the Worker.

The GitHub Secret contains a short-lived GitHub App installation token with
`Contents: Read-only` on the one configured repository. The customer must renew
it before expiry through its App-token rotation process; neither a PAT nor the
GitHub App private key is accepted by this Worker configuration. The External
ID Secret contains only the random External ID required by the customer read
Role trust policy. Across the complete target array, the GitHub-token Secret ARN
set and External-ID Secret ARN set must be disjoint; one Secret can never serve
both credential roles, including across different targets.

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

## Quality gate precondition

Do not dispatch live M1 until the C/Shared owners approve and execute the exact
six-rule × three-perspective Golden gate. The current committed case file is not
yet executable against production semantics: its artifact IDs have no committed
artifact resolver, its evidence locators differ from the runtime canonical
locators, and paired `IAC=FAIL`/`AWS_ACTUAL=FAIL` rows incorrectly expect
`DRIFT=FAIL` even though deterministic drift derivation returns aligned
`PASS`. The generic benchmark dry-run is model-selection evidence, not this M1
quality approval. Record the approved corrected dataset/prompt/rubric version and
sanitized gate result before customer deployment.

## Actual E2E

1. Create the controlled workload: a test Terraform repository with one S3
   target, commit it, create the corresponding approved sandbox S3 bucket, and
   record the exact 40-character commit SHA. Install the GitHub App on that
   workload repository with Contents read-only permission only.
2. Create the short-lived GitHub installation-token Secret and random External
   ID Secret in the customer account. Create the customer S3 read-only Role
   trusted only by the future foundation Worker Role and requiring that External
   ID. Set the three protected M1 Environment Secrets using the target JSON
   shape above.
3. Merge the reviewed `dev` revision and dispatch **Deploy M0 Foundation** with
   all ten required workflow inputs:

   | Input | Approved value |
   | --- | --- |
   | `stack_name` | Bootstrap `FoundationStackName` |
   | `project_name` | Bootstrap `ProjectName` |
   | `environment` | `customer-sandbox-artifact` or the exact first protected Environment |
   | `stack_environment` | Bootstrap `PlatformEnvironment`, normally `sandbox` |
   | `artifact_approval_environment` | `customer-sandbox-deploy` or the exact distinct second Environment |
   | `aws_region` | Exact Region pinned by `fixtures/m1/assessment_model_profile.json`; currently `us-east-1` |
   | `role_to_assume` | Bootstrap `GitHubActionsDeploymentRoleArn` |
   | `cloudformation_execution_role_arn` | Bootstrap `FoundationExecutionRoleArn` |
   | `lambda_code_s3_bucket` | Bootstrap `LambdaCodeBucketName` |
   | `assessment_scope_json` | Customer-keyed selector map matching the runtime target tuples exactly |

   Example selector shape: `{"<customer-id>":[{"repository_id":"<product-repository-id>"}]}`. The selector
   names the Repository boundary only; the Policy Profile is chosen per Assessment from the
   customer partition's Catalog (ADR-0023), and a `policy_profile_id` key here fails validation.
   `M1_ASSESSMENT_MODE` is a protected Environment variable, not a dispatch input.
   The workflow validates mode, selector equality, canonical GitHub repository
   identity, exact ARN sets and credential-role
   disjointness, account, the approved Model Profile Region, and the lowercase
   40-character workload commit before configuring customer deployment credentials.
   The separately approved artifact-preparation identity has already created or
   verified the immutable package.
4. Approve artifact preparation, review its immutable package evidence, then
   approve the separate deployment Environment.
5. After the foundation succeeds, validate the committed catalog plan from the
   same revision:

   ```powershell
   .venv\Scripts\python.exe scripts\publish_policy_catalog.py `
     --customer-id "<approved-customer-id>" `
     --table-name "<MetadataTableName>" `
     --region us-east-1 `
     --dry-run
   ```

   Publishing without `--dry-run` is a DynamoDB write and requires a separate
   customer-approved protected operator/CI identity with exact-table
   `GetItem`/`PutItem`; the deployment and CloudFormation execution roles must
   not be broadened or reused for this step. Stop if an immutable key differs.
6. Create the controlled Cognito `User` as described in
   [M1-AUTH-FRONTEND-TEST.md](M1-AUTH-FRONTEND-TEST.md).
7. In the SPA, log in and submit the configured repository ID and policy profile.
8. Verify the report shows an `AWS_ACTUAL` result, coverage 100%, and any
   persisted Findings/Readiness Score. Record only customer-controlled evidence
   references and the workflow run URL.

If the configured commit, canonical repository identity, customer, account, Role, or secret does
not match, the Worker fails closed. It never falls back to an unconfigured or malformed
repository or account in live M1 mode.
