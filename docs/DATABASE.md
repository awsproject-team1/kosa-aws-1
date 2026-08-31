# Database Design

> Status: M0 CloudFormation foundation and storage hardening implemented; no customer stack deployed
>
> Scope: DynamoDB metadata/state and S3 artifact references. The foundation defines storage
> encryption, retention, ownership, transport controls, and artifact-access audit delivery;
> lifecycle/Object Lock and audit-retention policy remain follow-up work.

## Decision summary

- MVP uses one DynamoDB table for operational and domain metadata: `<project>-<env>-metadata`.
- The table uses on-demand capacity, deletion protection, server-side encryption, point-in-time recovery, and a TTL attribute named `expires_at`.
- The account-qualified artifact bucket is versioned, encrypted, bucket-owner enforced, private,
  denies non-TLS requests, and emits its S3 object read/write data events to a separate retained
  CloudTrail audit destination with log-file validation.
- Every item includes `customer_id`, `entity_type`, `created_at`, `updated_at`, and a schema/version field where applicable.
- Large or immutable artifacts remain in S3; DynamoDB stores only their identity, hash, version, and access scope.
- SQS carries only resumable work notifications. DynamoDB remains the authoritative Job and
  checkpoint state store; EventBridge receives GitHub Actions completion events.
- All access is tenant-scoped. The API validates the Cognito JWT scope before issuing a DynamoDB query or mutation.

CloudFormation receives `ProjectName` and `Environment` parameters and derives the deployed
table name as `<project>-<env>-metadata`. The repository never hard-codes the official
project name; validation restricts the two parameters to the naming rule in `docs/NAMING.md`.

## Primary key model

The primary key keeps a customer's records together while allowing entity-prefix queries.

| Attribute | Type | Meaning |
| --- | --- | --- |
| `PK` | String | `CUSTOMER#{customer_id}` |
| `SK` | String | Entity hierarchy and ordering key |
| `entity_type` | String | Entity discriminator |
| `customer_id` | String | Required tenant boundary |
| `version` | Number/String | Schema or entity version |
| `created_at`, `updated_at` | ISO-8601 String | Audit timestamps |
| `expires_at` | Number | Unix epoch TTL; omitted for retained records |

## Item layout

| Entity | PK | SK | Purpose |
| --- | --- | --- | --- |
| Customer | `CUSTOMER#{customer_id}` | `PROFILE` | Customer metadata and configuration |
| Repository | `CUSTOMER#{customer_id}` | `REPOSITORY#{repository_id}` | Approved GitHub repository and scope |
| Policy profile | `CUSTOMER#{customer_id}` | `POLICY_PROFILE#{policy_profile_id}` | Allowed policy/rule boundary |
| Policy source | `CUSTOMER#{customer_id}` | `POLICY_SOURCE#{source_id}#VERSION#{version}` | Policy artifact identity, version, hash |
| Policy ingestion | `CUSTOMER#{customer_id}` | `POLICY_INGESTION#{source_id}#VERSION#{version}` | Upload validation, parser/normalization status and immutable Artifact references |
| Rule metadata | `CUSTOMER#{customer_id}` | `RULE#{rule_id}#VERSION#{version}` | Rule, source reference, lifecycle |
| Golden dataset case | `CUSTOMER#{customer_id}` | `GOLDEN_CASE#{case_id}#RUBRIC#{rubric_version}` | Expected evaluation range and artifact reference |
| Job | `CUSTOMER#{customer_id}` | `JOB#{job_id}` | Async workflow state and current step |
| Job checkpoint | `CUSTOMER#{customer_id}` | `JOB#{job_id}#CHECKPOINT#{revision}` | Immutable resumable step, next resource, retry metadata, Artifact references |
| Assessment | `CUSTOMER#{customer_id}` | `ASSESSMENT#{assessment_id}` | Assessment metadata, score, coverage |
| Assessment evaluation plan | `CUSTOMER#{customer_id}` | `ASSESSMENT#{assessment_id}#PLAN` | Immutable planned applicable Resource × Rule × Perspective count |
| Assessment result | `CUSTOMER#{customer_id}` | `ASSESSMENT#{assessment_id}#RESULT#{resource_id}#RULE#{rule_id}#PERSPECTIVE#{perspective}` | IaC, Actual, or Drift Resource × Rule judgment and evidence references |
| Finding | `CUSTOMER#{customer_id}` | `ASSESSMENT#{assessment_id}#FINDING#{finding_id}` | Actionable result and severity |
| Remediation | `CUSTOMER#{customer_id}` | `REMEDIATION#{remediation_id}` | Patch, PR, source Finding references |
| Deployment | `CUSTOMER#{customer_id}` | `DEPLOYMENT#{deployment_id}` | Plan, approval, apply, verification state |
| Approval | `CUSTOMER#{customer_id}` | `DEPLOYMENT#{deployment_id}#APPROVAL#{approval_id}` | Approver, `commit_sha`, `plan_hash` binding |
| Audit event | `CUSTOMER#{customer_id}` | `AUDIT#{occurred_at}#{event_id}` | Immutable application audit trail |

`Assessment result` and `Finding` are co-located with their Assessment so one query can retrieve the full assessment report. If an Assessment can exceed DynamoDB partition or response limits, results are paginated by `SK` and large report payloads remain in S3.

## Secondary indexes and access patterns

| Index | Key | Access pattern |
| --- | --- | --- |
| Base table | `CUSTOMER#{customer_id}` + `JOB#{job_id}` | Get a customer-scoped Job |
| Base table | `CUSTOMER#{customer_id}` + `ASSESSMENT#{assessment_id}` prefix | Assessment with results/findings |
| Base table | `CUSTOMER#{customer_id}` + `DEPLOYMENT#{deployment_id}` prefix | Deployment and approval records |
| `GSI1` | `GSI1PK = JOB#{job_id}`, `GSI1SK = CUSTOMER#{customer_id}` | Resolve a Job ID, then verify customer scope before return |
| `GSI2` | `GSI2PK = CUSTOMER#{customer_id}#JOB_STATUS#{status}`, `GSI2SK = updated_at#JOB#{job_id}` | Customer Job list by status and recency |
| Base table | `CUSTOMER#{customer_id}` + `POLICY_INGESTION#{source_id}#VERSION#{version}` | Processing status of one uploaded Policy Source version |
| `GSI2` | `GSI2PK = CUSTOMER#{customer_id}#INGESTION_STATUS#{status}`, `GSI2SK = updated_at#POLICY_INGESTION#{source_id}#VERSION#{version}` | Customer ingestion list by status and recency |
| `GSI3` | `GSI3PK = REPOSITORY#{repository_id}`, `GSI3SK = started_at#ASSESSMENT#{assessment_id}` | Assessment history for an approved repository after scope validation |

Only items requiring an access pattern populate the corresponding GSI attributes. No scan is permitted for request handling.

M0 Job records populate `GSI1` at creation. `GSI2` is a required table/index contract but
is populated only when the Job repository gains the customer-scoped list operation; the
current `get_job(customer_id, job_id)` path must use the base table. This avoids exposing a
global Job lookup as an authorization shortcut.

M0의 `POST /assessments`는 `ASSESSMENT#{assessment_id}`, 연결된 `JOB#{job_id}`,
`OUTBOX#JOB#{job_id}`를 DynamoDB transaction으로 함께 저장한다. Assessment 레코드는
`repository_id`, `policy_profile_id`, `job_id`를 영속화하므로 Worker는 최소 Queue payload만으로도
평가 selector를 복원한다. Outbox는 `GSI2PK = OUTBOX#PENDING`으로 pending 전송을 조회하며,
API는 커밋 직후 해당 task의 SQS 전송을 즉시 시도하고 성공한 경우에만 `DISPATCHED`로 전이한다.
SQS 또는 상태 갱신 실패는 `PENDING`으로 남아, Outbox sweeper가 다음 실행에서 at-least-once로
재시도한다.

## Example items

```json
{
  "PK": "CUSTOMER#cust_123",
  "SK": "ASSESSMENT#asm_456",
  "entity_type": "ASSESSMENT",
  "customer_id": "cust_123",
  "repository_id": "repo_123",
  "policy_profile_id": "profile_001",
  "status": "COMPLETED",
  "readiness_score": 73,
  "coverage": 0.8,
  "started_at": "2026-08-29T10:00:00Z",
  "updated_at": "2026-08-29T10:03:00Z",
  "version": 1,
  "GSI3PK": "REPOSITORY#repo_123",
  "GSI3SK": "2026-08-29T10:00:00Z#ASSESSMENT#asm_456"
}
```

```json
{
  "PK": "CUSTOMER#cust_123",
  "SK": "ASSESSMENT#asm_456#RESULT#s3_bucket_logs#RULE#S3-PUBLIC-001#PERSPECTIVE#AWS_ACTUAL",
  "entity_type": "ASSESSMENT_RESULT",
  "customer_id": "cust_123",
  "perspective": "AWS_ACTUAL",
  "status": "FAIL",
  "severity": "HIGH",
  "score": 27,
  "evidence_references": ["s3://policy-artifacts/...#hash", "aws:s3:bucket-policy"],
  "rule_version": "2026-08-01",
  "rubric_version": "v1",
  "created_at": "2026-08-29T10:03:00Z",
  "updated_at": "2026-08-29T10:03:00Z",
  "version": 1
}
```

## S3 artifact reference contract

```json
{
  "artifact_id": "art_123",
  "artifact_type": "TERRAFORM_SNAPSHOT",
  "bucket": "<project>-<env>-artifacts-<account-id>",
  "key": "customers/cust_123/repositories/repo_123/snapshots/sha256-...",
  "version_id": "optional-s3-version-id",
  "content_sha256": "hex-digest",
  "customer_id": "cust_123",
  "repository_id": "repo_123"
}
```

Artifact references belong on their owning DynamoDB item. Clients never receive a public S3 path; the Backend validates JWT and entity scope before issuing controlled access.

M0 Artifact type vocabulary is `POLICY_ORIGINAL`, `TERRAFORM_SNAPSHOT`, `AWS_SNAPSHOT`,
`REMEDIATION_PATCH`, `TERRAFORM_PLAN`, and `GOLDEN_DATASET`. Each reference persists its
`artifact_id`, `content_sha256`, `customer_id`, and where relevant `repository_id`; no
bucket/key is exposed through the public transport contract.

고객 정책 수집을 구현할 때 `POLICY_NORMALIZED` Artifact type을 추가하고 `POLICY_ORIGINAL`과
분리한다. Policy ingestion metadata는 원본 파일명, 선언/탐지 media type, byte size, parser
ID/version, 처리 상태, 원본·정규화 Artifact ID/hash, warning/error code를 고객 partition에
저장한다. 새 Source version은 기존 Artifact를 덮어쓰지 않으며, 승인 전에는 Policy Profile이
참조할 수 없다. 상세 lifecycle과 보안 경계는 `docs/POLICY_INGESTION.md`를 따른다.

Ingestion 레코드의 SK prefix는 `POLICY_INGESTION`이다. 별도 ingestion ID를 두지 않고
`POLICY_INGESTION#{source_id}#VERSION#{version}`으로 `POLICY_SOURCE` 항목과 같은
(source_id, version) 좌표를 공유하므로, 같은 Source version의 수집 상태와 승인된 Source가
같은 키로 대응된다. 상태 조회 API는 이 SK로 base table에서 직접 읽고, 고객 단위 처리 목록은
`GSI2`의 `INGESTION_STATUS` partition으로 조회한다. 두 경로 모두 scan을 쓰지 않는다.

## Consistency, state, and retention

- Use conditional writes for state transitions, optimistic version checks, and immutable approval binding.
- Job state transitions include at least `QUEUED`, `RUNNING`, `WAITING_APPROVAL`, `COMPLETED`, `FAILED`, `CANCELLED`.
- Job/workflow checkpoints set `expires_at` to 30 days after a terminal transition
  (`COMPLETED`, `FAILED`, `CANCELLED`). Queued, running, and approval-waiting Jobs never
  receive a TTL.
- Approval, deployment, and audit records do not use TTL until compliance retention requirements are agreed.
- DynamoDB TTL is asynchronous; application code must treat expired records as unavailable even before physical deletion.
- Queue payloads contain only `job_id`, `expected_revision`, and an approved command. Workers
  load the authoritative Job and latest checkpoint with a conditional revision check; a stale or
  duplicate Queue delivery cannot advance a Job.
- Assessment work is split by resource. With three minutes remaining in its 15-minute Lambda
  budget, a Worker conditionally persists its checkpoint and publishes the next Queue task.
- Retryable AWS, Bedrock, S3, and GitHub failures receive at most three total attempts before
  the Queue DLQ and terminal `FAILED` Job state. Validation, scope, permission, and Contract
  failures are terminal without retry. Apply never retries automatically; ambiguous outcomes
  require Terraform/AWS reconciliation and `MANUAL_REVIEW`.
- Admin retry creates a new Job revision and Queue task; it never replays a failed delivery in
  place. GitHub Actions sends Plan/Apply completion metadata through OIDC-authorized EventBridge
  events, which target the Deployment Queue.

All writeable entities use Backend-generated opaque IDs. Callers provide approved repository,
policy profile, and AWS Account selectors but never `customer_id`, `job_id`, timestamps,
revision, GSI, or TTL attributes. The Backend derives customer scope from the verified JWT,
generates the Job ID, and owns `created_at`, `updated_at`, revision, and `expires_at`.

## Security and tenant isolation

- The Backend derives `customer_id` and allowed repository/account scope from verified JWT claims; callers cannot select an arbitrary partition key.
- Read/write conditions require the expected `customer_id`, entity version, and approved state where applicable.
- `GSI1`/`GSI3` results are filtered by an authoritative item read or scoped condition before disclosure.
- DynamoDB and S3 encryption, DynamoDB deletion protection, S3 versioning and bucket-owner-enforced ownership, non-TLS request denial, least-privilege IAM, CloudTrail, and application audit events are required.
- The M0 Worker has no artifact-bucket permission because it processes only packaged synthetic
  fixtures. A runtime that handles customer artifacts must use the tenant-scoped identity boundary
  required by ADR-0014; a pooled `customers/*` role is not an acceptable tenant boundary.
- CloudTrail records ArtifactBucket `AWS::S3::Object` read/write data events in a separate retained
  audit bucket with log-file validation. Audit records can contain object-key metadata; artifact
  keys must not contain secrets, policy originals, prompts, or full IaC content.

## Open decisions

| Decision | Owner | Needed by | Blocks |
| --- | --- | --- | --- |
| Official `<project>` resource-name prefix | Team | Infrastructure implementation | Final table/bucket names |
| Queue visibility timeout, DLQ retention, and redrive alarm thresholds | A + Security | Infrastructure implementation | Worker resilience configuration |
| Audit/approval retention, Object Lock, and CloudTrail audit-destination lifecycle policy | A + Security | Before customer deployment | Compliance controls |
| Per-assessment result volume and pagination threshold | C + A | Assessment implementation | Query/pagination limits |
| Additional reporting/search index requirements | A/B/C/D | Before UI reporting implementation | GSI additions |
