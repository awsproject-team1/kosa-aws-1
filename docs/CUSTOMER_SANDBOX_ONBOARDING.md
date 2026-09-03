# Customer sandbox onboarding kit

This document is the request package for a customer's first protected sandbox deployment. It starts before a Terraform workload or AWS workload resource exists. The platform repository never stores the customer's account ID, role ARNs, state bucket name, GitHub token, Terraform variables, policy originals, or Terraform state.

The customer creates a separate, customer-owned Terraform repository. Do not run the template in this repository, and do not use a production account for the first exercise. A real customer repository must be private. A public repository is permitted only for an explicitly approved disposable test, with no customer identifiers, credentials, policy material, state file, or production configuration.

## 1. Customer decision and evidence request

The customer administrator supplies the following through the approved customer channel or protected GitHub Environment, never a repository issue, PR body, or chat transcript.

| Item | Customer action | Acceptance condition |
| --- | --- | --- |
| Sandbox boundary | Select an AWS sandbox account and an owner | It is separate from production and approved for creating an S3 test bucket. |
| Terraform repository | Create an empty private IaC repository and name its default branch | The customer owns administration and branch protection. A public repository is a test-only exception under the boundary above. |
| Reviewers | Nominate Terraform and security reviewers | Apply requires at least one reviewer who is not the workflow initiator. |
| Region | Select the approved sandbox Region | It matches the platform's approved model profile; currently `us-east-1`. |
| State backend | Create a state S3 bucket and DynamoDB lock table | Bucket is versioned, encrypted, TLS-only, and bucket-owner-enforced; lock table is dedicated to Terraform state. |
| OIDC roles | Create separate Plan and Apply roles | Each trust policy is limited to the exact repository and allowed GitHub OIDC subject; neither is an administrator role. If the Organization customizes OIDC subjects, use the immutable-ID `sub` actually issued by GitHub. |
| GitHub Environments | Create `customer-terraform-plan` and protected `customer-terraform-apply` | Apply has required reviewers; each role trust matches its exact Environment subject. Both hold the same state backend variables. |
| Platform connection | Approve the GitHub App installation and a read credential for workflow/run/artifact verification | App has `contents: write` and `pull_requests: write` only; it does not have `workflows: write`. |

The customer owns workload-specific IAM permissions. A generic template cannot know which AWS services their Terraform will manage, so it must not grant `AdministratorAccess` or wildcard write access merely to make a first run easier.

## 2. Repository starter installation

Copy the directory [`templates/customer-sandbox-terraform-repository`](../templates/customer-sandbox-terraform-repository/) into the root of the new customer-owned repository. It is intentionally a template, not deployable customer IaC.

Before a first pull request, the customer administrator must:

1. Replace the example backend values with the customer-controlled state bucket, DynamoDB table, Region, and a state key derived from the customer/repository workspace. Keep the completed local `backend.hcl` untracked.
2. Choose a globally unique, non-sensitive value for `sandbox_bucket_name` in a protected customer variable source. Do not put customer names, policy text, or production identifiers in it.
3. Run `terraform init -backend-config=backend.hcl` and `terraform providers lock` in the customer-controlled repository, then commit the resulting `.terraform.lock.hcl`. This lock file is mandatory for reproducible plans.
4. Copy these platform-owned files without changing their security semantics:

   | Source in this repository | Destination in customer repo |
   | --- | --- |
   | `ci/terraform/terraform-plan.yml` | `.github/workflows/terraform-plan.yml` |
   | `ci/terraform/terraform-apply.yml` | `.github/workflows/terraform-apply.yml` |
   | `ci/terraform/canonical_plan_hash.py` | `ci/terraform/canonical_plan_hash.py` |

5. Add GitHub Environment variables. `customer-terraform-plan` needs `AWS_REGION`, `TF_PLAN_ROLE_ARN`, `TF_STATE_BUCKET`, `TF_STATE_KEY`, `TF_LOCK_TABLE`, and `SANDBOX_BUCKET_NAME`; `customer-terraform-apply` needs the same values except `TF_APPLY_ROLE_ARN` replaces `TF_PLAN_ROLE_ARN`. The state values and bucket name must be identical in both Environments.
6. Configure the `customer-terraform-apply` GitHub Environment with required reviewers before enabling workflow dispatch. Keep Terraform `1.9.5`, the saved-plan-only apply, plan hash verification, and state `lineage`/`serial` verification unchanged.

The templates are explained in [the Terraform workflow guide](../ci/terraform/README.md). The Foundation application deployment has a separate customer bootstrap and runbook; do not treat this Terraform workload state backend as Foundation state.

## 3. First safe test

The starter's first resource is one private S3 bucket with public access block, bucket-owner-enforced ownership, default encryption, and a TLS-only bucket policy. It has `force_destroy = false`; Terraform will refuse to delete a non-empty bucket.

Use this sequence:

1. Open a PR containing the initialized lock file and the secure S3 baseline.
2. Customer reviewers merge it to the default branch.
3. Create a platform deployment for that merge commit; the platform dispatches the customer-installed plan workflow.
4. Check the plan and its mapped S3 bucket identity. If the plan is destructive or the state changed, stop for manual review.
5. Approve through the platform and the protected GitHub Apply Environment. The apply workflow uses only the saved plan and rechecks its hash and state.
6. Confirm the bucket through the platform's read-only resource path and retain only the approved non-sensitive execution evidence in customer storage.

Do not introduce an intentional insecure setting until this secure baseline has completed. Any later remediation demonstration belongs in a separately reviewed PR and must still use the protected plan/approval/apply path.

## 4. Handoff checklist

- [ ] Separate sandbox account and customer owner approved
- [ ] Customer-owned Terraform repository and default branch protected
- [ ] State bucket and lock table evidence reviewed
- [ ] Separate least-privilege Plan and Apply OIDC roles reviewed
- [ ] Apply GitHub Environment has required reviewers
- [ ] Starter copied, backend configured, and `.terraform.lock.hcl` committed
- [ ] Platform plan/apply workflows manually installed by the customer
- [ ] First secure S3 baseline PR merged
- [ ] Platform repository connection and customer scope approved

If any item is absent, stop before creating a platform Deployment or granting any additional permission.
