# M0 foundation 승인 배포 및 sandbox 감사 검증 runbook

이 runbook은 병합된 M0 foundation을 고객 sandbox에 배포하기 전에 승인 자료를 준비하고,
ArtifactBucket CloudTrail data-event 감사를 검증하는 운영 절차다. 고객 이름, AWS account ID,
role ARN, scope JSON, credentials, artifact 내용, object key를 저장소나 PR에 기록하지 않는다.

## Boundary

- 이 문서는 GitHub Actions의 `Deploy M0 Foundation` 수동 workflow를 위한 준비 절차다.
  로컬 개발자·Agent 세션에서 `aws cloudformation deploy`, artifact upload, CloudTrail 조회 또는
  object Get/Put을 실행하지 않는다.
- 실제 변경은 보호된 GitHub Environment의 required reviewer 승인 뒤 customer-approved OIDC role로
  실행되는 workflow만 수행한다.
- M0 CloudFormation foundation은 고객 Terraform workload의 Plan/Apply 경로가 아니다. 따라서
  Terraform `commit_sha`/`plan_hash` 승인 바인딩을 이 workflow에 대입하지 않는다. 고객 workload
  변경은 ADR-0007의 별도 승인 경로를 따른다.
- 이 runbook은 CloudTrail delivery를 검증하기 위한 절차이지, customer artifact access를 M0 Worker에
  부여하는 절차가 아니다. M0 Worker는 ArtifactBucket 접근 권한을 갖지 않는다.
- CloudTrail log-file validation은 customer-controlled evidence executor가 승인된 account/region/day
  log와 digest prefix에서만 파일을 일시적으로 읽도록 허용한다. 원시 log 내용은 저장소, PR, 또는
  Agent 세션으로 복사하지 않는다.

## 1. Approval packet

승인 요청자는 workflow dispatch 전에 아래 값을 승인자에게 별도 전달한다. 실제 값은 GitHub
Environment 또는 고객 승인 채널에서만 공유하며, 이 저장소와 PR 본문에는 넣지 않는다.

| Item | Required evidence | Acceptance criterion |
| --- | --- | --- |
| Source revision | Merged `dev` commit SHA and PR/CI links | Required CI is green for the exact revision dispatched. |
| Stack identity | Non-sensitive stack name, project name, environment, region | Names satisfy `infrastructure/parameters/README.md`; target is an approved sandbox. |
| GitHub Environment | Selected protected Environment and reviewer list | Required reviewers are configured and approve before the deployment job receives credentials. |
| OIDC deployment role | Customer-managed role ARN and trust-policy attestation | Trust is restricted to this repository, approved ref/environment, and the deployment workflow; permissions are limited to the M0 stack, Lambda artifact bucket, CloudTrail, and audit destination. |
| Lambda artifact bucket | Bucket name and versioning evidence | Customer-owned, versioning is `Enabled`, and the OIDC role can upload the immutable workflow key. |
| Assessment scope | Approved selector map | Fail-closed JSON; contains no credentials, policy originals, prompts, or full IaC content. `{}` is valid when no selector is approved. |
| Retention operations | Named customer owner and retention decision | Owner accepts retained metadata/audit resources, CloudTrail data-event cost, audit-log access boundary, and the stack termination-protection decision. |
| Sandbox test principal | Customer-approved controlled principal and window | May perform only one verification `PutObject` and `GetObject` for the opaque ArtifactBucket key; it is not a Worker runtime identity. |
| Evidence executor | Customer-approved separate principal and window | May list the approved account/region/day audit prefixes and transiently `GetObject` only for those CloudTrail logs/digests during validation; it cannot write audit artifacts or read other prefixes. |

Stop before dispatch if any item is missing, the revision differs from the reviewed revision, the
Environment lacks required reviewers, the artifact bucket is not versioned, or the OIDC trust
boundary cannot be attested.

## 2. Workflow inputs

Dispatch `.github/workflows/deploy-m0-foundation.yml` manually from the reviewed merged `dev`
revision. Populate only the workflow inputs below. `LambdaCodeS3Key` is derived by the workflow as
`lambda/m0/<GitHub commit SHA>.zip`; do not provide it separately.

| Workflow input | Source and validation |
| --- | --- |
| `stack_name` | Customer-approved sandbox stack name. It must not identify a production workload. |
| `project_name` | Same constraints as `ProjectName`: 2–31 lowercase letters, digits, or hyphens; starts with a letter; ends with a letter or digit; avoids reserved S3 prefixes. |
| `environment` | The exact protected GitHub Environment selected for the sandbox. It is also the CloudFormation `Environment` value and must satisfy its 2–8 lowercase-character constraint. |
| `aws_region` | Customer-approved deployment region. M0 design currently targets `us-east-1` unless an approved exception exists. |
| `role_to_assume` | Customer-approved OIDC deployment role ARN. Do not enter an Agent, user, or workload runtime role. |
| `lambda_code_s3_bucket` | Versioning-enabled customer-owned deployment-artifact bucket. The workflow checks versioning before upload. |
| `assessment_scope_json` | Approved fail-closed selector JSON. Do not paste sensitive policy or artifact data into the workflow input. |

Before approving the job, reviewers compare the displayed inputs and checked-out revision with the
approval packet. They also confirm the expected resource inventory: metadata table, artifact bucket,
separate audit bucket, artifact-access trail, queues/DLQs, IAM roles, Cognito, API, and Lambda
functions. The workflow packages the Lambda ZIP, uploads it to the derived immutable key, then
performs the CloudFormation update with `CAPABILITY_NAMED_IAM`.

The workflow currently uses `aws cloudformation deploy` rather than a separately archived change
set. Reviewers must therefore review the exact template revision and inputs before Environment
approval. The customer operator owns any stack termination-protection configuration outside this
repository.

## 3. Sandbox CloudTrail acceptance test

Run this test only after the stack workflow reports success and only through the customer-approved
controlled test principal and separate evidence executor from the approval packet.

1. Record the workflow run URL, deployment revision, deployment start/end time in UTC, stack name,
   region, and non-secret stack output references for `ArtifactBucketName`, `ArtifactAuditLogBucketName`,
   and `ArtifactAccessTrailArn`.
2. Choose one newly generated opaque verification key under a dedicated verification prefix. Do not
   embed customer names, policy text, prompts, repository names, or IaC content in the key or object
   body. Record the key only in customer-controlled evidence storage.

### Bounded customer-executed commands

The commands below are an input contract for customer operators; they are not commands for a local
Agent or developer session. The controlled test principal receives only the artifact-bucket
`PutObject`/`GetObject` permissions required for this one test. The separate evidence executor has
only `ListBucket` for the listed audit prefixes and transient `GetObject` for the same approved
account/region/day CloudTrail log and digest prefixes. Substitute customer-controlled values only
after Environment approval, keep the UTC window bounded, and retain output outside this repository.

```bash
set -euo pipefail

export AWS_REGION="<approved-sandbox-region>"
export ARTIFACT_BUCKET="<ArtifactBucketName-output>"
export AUDIT_BUCKET="<ArtifactAuditLogBucketName-output>"
export TRAIL_ARN="<ArtifactAccessTrailArn-output>"
export ACCOUNT_ID="<customer-account-id>"
export VERIFY_KEY="verification/<new-opaque-random-id>"
export WINDOW_START_UTC="<YYYY-MM-DDTHH:MM:SSZ>"
export WINDOW_END_UTC="<YYYY-MM-DDTHH:MM:SSZ>"
export WINDOW_DATE_UTC="<YYYY/MM/DD-within-approved-window>"
export TEST_BODY="$(mktemp)"
export READBACK_BODY="$(mktemp)"
printf 'm0-cloudtrail-verification\n' > "${TEST_BODY}"
```

The approved test principal performs exactly one harmless write and one read of that same opaque
key, then removes only the local temporary files. It must not list the ArtifactBucket, write a
second key, or change bucket/trail policy.

```bash
aws s3api put-object \
  --bucket "${ARTIFACT_BUCKET}" \
  --key "${VERIFY_KEY}" \
  --body "${TEST_BODY}" \
  --output json

aws s3api get-object \
  --bucket "${ARTIFACT_BUCKET}" \
  --key "${VERIFY_KEY}" \
  "${READBACK_BODY}" \
  --output json

cmp -- "${TEST_BODY}" "${READBACK_BODY}"
rm -- "${TEST_BODY}" "${READBACK_BODY}"
```

The evidence executor does not manually export or retain raw CloudTrail log content. It may retrieve
the selected log/digest objects transiently only while executing `validate-logs`; validation output
and retained evidence stay in customer-controlled storage. It lists only the expected account/region/day
prefixes for the approved window. If the window crosses UTC midnight, repeat the two bounded list
commands for each date in that approved window.

```bash
export LOG_PREFIX="AWSLogs/${ACCOUNT_ID}/CloudTrail/${AWS_REGION}/${WINDOW_DATE_UTC}/"
export DIGEST_PREFIX="AWSLogs/${ACCOUNT_ID}/CloudTrail-Digest/${AWS_REGION}/${WINDOW_DATE_UTC}/"

aws s3api list-objects-v2 \
  --bucket "${AUDIT_BUCKET}" \
  --prefix "${LOG_PREFIX}" \
  --max-keys 20 \
  --query 'Contents[].{Key:Key,LastModified:LastModified,Size:Size}' \
  --output table

aws s3api list-objects-v2 \
  --bucket "${AUDIT_BUCKET}" \
  --prefix "${DIGEST_PREFIX}" \
  --max-keys 20 \
  --query 'Contents[].{Key:Key,LastModified:LastModified,Size:Size}' \
  --output table

aws cloudtrail validate-logs \
  --trail-arn "${TRAIL_ARN}" \
  --start-time "${WINDOW_START_UTC}" \
  --end-time "${WINDOW_END_UTC}" \
  --verbose
```

3. Wait for CloudTrail delivery according to the customer-approved observation window. Do not weaken
   the trail selector, add the audit bucket to the selector, or broaden the audit-bucket policy to
   troubleshoot delayed delivery.
4. Confirm that a delivered CloudTrail log under `LOG_PREFIX` contains S3 data events for the
   ArtifactBucket only. Verify the event source, `PutObject` and `GetObject` event names, test-principal
   identity, event time within the window, and the opaque verification key. Inspect this content only
   through the approved evidence path; never copy raw logs into this repository.
5. Record the result as pass only when both artifact operations have delivered audit events and
   `validate-logs` succeeds. Record the workflow run, trail ARN, UTC window, opaque-key digest or
   reference, event IDs, validation result, reviewer, and evidence-retention location. Do not commit
   raw audit logs or object keys to this repository.
6. Do not delete the verification object as part of this procedure. Retain it as customer-controlled
   evidence until the customer security owner approves its expiry or a separately reviewed cleanup
   operation. Retained audit resources must never be deleted as test cleanup.

## 4. Fail-closed response

Mark the sandbox verification failed and do not promote the stack or grant ArtifactBucket access to
any runtime when any of the following occurs:

- The workflow, Environment approval, OIDC role assumption, Lambda artifact upload, or CloudFormation
  update fails.
- The audit bucket receives no matching delivered data event in the approved observation window.
- The event is not limited to the expected ArtifactBucket test operation, the identity/time/key does
  not match the controlled test, or log-file validation fails.
- The audit policy or trail selector must be broadened to make the test pass.
- Any credential, customer scope, policy original, prompt, or raw artifact content appears in workflow
  logs, review artifacts, or repository files.

On failure, preserve the non-sensitive workflow and audit evidence, notify the named customer
security owner, and open a new reviewed change for the root cause. Do not bypass the protected
Environment, run a local deployment, or retry with broader IAM/S3 permissions.

## Completion record

The customer security owner records the approval packet reference, workflow run URL, exact revision,
CloudTrail acceptance result, evidence location, retained-resource owner, and any cost/lifecycle
decision in the customer-controlled deployment record. This repository stores only non-sensitive
architecture and runbook updates.
