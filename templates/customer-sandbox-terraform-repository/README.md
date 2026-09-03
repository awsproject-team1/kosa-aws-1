# Customer sandbox Terraform repository starter

Copy these files into a new, customer-owned private repository. They are a non-deploying starter only: the customer must supply the state backend, a globally unique bucket name, reviewer-approved OIDC roles, and a generated `.terraform.lock.hcl` before any workflow is run. A public repository is allowed only for an explicitly approved disposable test with no customer data, credentials, policy material, state file, or production configuration.

This starter creates a single secure S3 baseline, not a production workload. It deliberately does not include account IDs, role ARNs, credentials, state, or a committed provider lock file.

## Install

1. Copy `backend.hcl.example` to the customer-controlled `backend.hcl` and replace every placeholder.
2. Copy `terraform.tfvars.example` to a protected customer variable source or customer-controlled `terraform.tfvars`; choose a globally unique bucket name.
3. In the customer repository, initialize and lock the exact providers:

   ```bash
   terraform init -backend-config=backend.hcl
   terraform providers lock
   ```

   Commit `.terraform.lock.hcl`; do not commit `backend.hcl` or `terraform.tfvars` if they identify the customer.
4. Manually install the platform workflow templates and canonical hash helper described in `docs/CUSTOMER_SANDBOX_ONBOARDING.md`. Do not put role, state, or sandbox bucket values in the copied workflows: configure `AWS_REGION`, each role ARN, the shared state bucket/key/lock-table values, and `SANDBOX_BUCKET_NAME` as GitHub Environment variables instead.
5. Submit the first baseline through a normal customer PR. Never run local `terraform apply`; actual apply is through the protected GitHub workflow.

## State conventions

The backend uses one state key per customer/repository workspace. The concrete key belongs in customer-controlled `backend.hcl`; this template provides no default to prevent accidental state sharing.
