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
| Rule metadata | `CUSTOMER#{customer_id}` | `RULE#{rule_id}#VERSION#{version}` | Published Rule and source reference (candidate lifecycle is managed before publication) |
| Golden dataset case | `CUSTOMER#{customer_id}` | `GOLDEN_CASE#{case_id}#RUBRIC#{rubric_version}` | Expected evaluation range and artifact reference |
| Job | `CUSTOMER#{customer_id}` | `JOB#{job_id}` | Async workflow state and current step |
| Job checkpoint | `CUSTOMER#{customer_id}` | `JOB#{job_id}#CHECKPOINT#{revision}` | Immutable resumable step, next resource, retry metadata, Artifact references |
| Assessment | `CUSTOMER#{customer_id}` | `ASSESSMENT#{assessment_id}` | Assessment selectors, phase, optional verification provenance; report projection exposes score and coverage |
| Assessment evaluation plan | `CUSTOMER#{customer_id}` | `ASSESSMENT#{assessment_id}#PLAN` | Immutable planned applicable Resource × Rule × Perspective **set** (`planned_coordinates`), its count, and the completion counter |
| Assessment result | `CUSTOMER#{customer_id}` | `ASSESSMENT#{assessment_id}#RESULT#{resource_id}#RULE#{rule_id}#PERSPECTIVE#{perspective}` | IaC, Actual, or Drift Resource × Rule judgment, evidence, assessed commit, and evaluation time |
| Finding | `CUSTOMER#{customer_id}` | `ASSESSMENT#{assessment_id}#FINDING#{finding_id}` | Actionable result, severity, and copied assessed commit/evaluation time |
| Remediation | `CUSTOMER#{customer_id}` | `REMEDIATION#{remediation_id}` | Immutable `RemediationDecision`, C context, source Finding, optional Job/result reference |
| Remediation exception | `CUSTOMER#{customer_id}` | `REMEDIATION_EXCEPTION#RULE#{rule_id}#VERSION#{version}#EXCEPTION#{exception_id}` | Admin-approved enum reason, optional Resource scope, approval/expiry binding |
| Deployment | `CUSTOMER#{customer_id}` | `DEPLOYMENT#{deployment_id}` | Plan, approval, apply, verification state |
| Approval | `CUSTOMER#{customer_id}` | `DEPLOYMENT#{deployment_id}#APPROVAL#{approval_id}` | Approver, `commit_sha`, `plan_hash` binding |
| Audit event | `CUSTOMER#{customer_id}` | `AUDIT#{occurred_at}#{event_id}` | Immutable application audit trail |

`Assessment result` and `Finding` are co-located with their Assessment so one query can retrieve the full assessment report. If an Assessment can exceed DynamoDB partition or response limits, results and Findings are independently paginated by their `SK` prefixes. 새 plan의 `completed_evaluations`는 Result/Finding immutable write와 같은 transaction에서만 증가하며, 진행 중 Coverage read는 이 counter를 사용한다. large report payloads remain in S3.

M1의 Readiness Score는 immutable Result와 Plan에서 report read 시 결정적으로 계산한다. 완료
counter와 materialized score는 이후 storage migration 전에는 Assessment metadata에 별도 write하지
않으므로, 진행 중 Assessment에 오래된 점수가 남지 않는다.

## Secondary indexes and access patterns

| Index | Key | Access pattern |
| --- | --- | --- |
| Base table | `CUSTOMER#{customer_id}` + `JOB#{job_id}` | Get a customer-scoped Job |
| Base table | `CUSTOMER#{customer_id}` + `ASSESSMENT#{assessment_id}` prefix | Assessment with results/findings |
| Base table | `CUSTOMER#{customer_id}` + `DEPLOYMENT#{deployment_id}` prefix | Deployment and approval records |
| Base table | `CUSTOMER#{customer_id}` + `REMEDIATION_EXCEPTION#RULE#{rule_id}#VERSION#{version}` prefix | 고객의 exact Rule version 예외 조회; resource scope는 조회 후 좁힌다 |
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

M2 Remediation에서 actionable decision은 `REMEDIATION#{remediation_id}`, 연결된 `JOB#{job_id}`,
`OUTBOX#JOB#{job_id}`, `REMEDIATION_DECIDED` audit event를 하나의 conditional transaction으로
쓴다. Remediation item의 `decision`은 B `RemediationDecision.to_dict()`, `context`는 C의
Finding/Snapshot/evidence 값이며 immutable하다. `MANUAL_REVIEW`와 `SUPPRESSED`는 Remediation과
audit 두 항목만 쓰고 Job/Outbox는 만들지 않는다. 고객 예외 등록도 exact Rule version key와
`REMEDIATION_EXCEPTION_APPROVED` audit event를 같은 transaction에 쓰며 ID/customer/approver/time은
Backend가 발급한다.

## M3 planned deployment and verification storage

ADR-0020은 `Accepted`이고 C 비교 Contract가 구현됐으며, 검증 Assessment의 phase/correlation
저장과 runtime 복원도 구현됐다. 아래 Deployment durable 저장과 endpoint 배선은 아직 구현되지
않았으며 A/D는 이 key/field 경계 밖에 검증 결과를 쓰지 않는다. ADR-0019의 Deployment 상태 기계는
계속 `Proposed`다.

| Entity | PK | SK | Purpose |
| --- | --- | --- | --- |
| Deployment workflow event | `CUSTOMER#{customer_id}` | `DEPLOYMENT#{deployment_id}#EVENT#{run_id}` | Plan/Apply 완료 Event detail. 같은 `run_id`는 한 번만 기록된다 |

- `DEPLOYMENT#{deployment_id}` item은 대상 `commit_sha`, `plan_hash`, 투영된 canonical JSON plan과
  saved binary plan의 Artifact reference, plan 시점의 Terraform **state `lineage`와 `serial`**,
  `remediation_id`, `source_assessment_id`, 그리고 검증 후의 `verification_assessment_id`를 보관한다.
  `serial` 단독으로는 state 재생성을 잡지 못하므로 두 값을 쌍으로 둔다.
- **`DeploymentStatus`는 이 item에 저장하지 않는다.** 배포 생애주기 위치는 `JobStatus`와
  `JobCurrentStep`에 이미 저장돼 있고, 표현 값은 read 시 `derive_deployment_status()`로 계산한다.
  M1 Readiness Score와 같은 원칙이며, 저장된 두 번째 사본이 없으므로 마이그레이션도 정합성 규칙도
  필요 없다 (ADR-0019 §8). 상태별 목록 조회가 필요해지면
  `GSI2PK = CUSTOMER#{customer_id}#DEPLOYMENT_STATUS#{status}`로 materialize하며, 그때까지 채우지
  않는다.
- Plan/Apply 완료 Event는 Queue payload에 값을 싣지 않고 이 event item으로 저장한다. Queue에는 계속
  `job_id`, `expected_revision`, `command`만 흐른다. Event detail은 신뢰 대상이 아니라 기록이며, D
  Worker가 `run_id`로 GitHub Actions run을 다시 읽어 대조한 결과만 Deployment 상태로 전이된다.
  GitHub Actions에는 DynamoDB write 권한을 주지 않는다.
- Post-Deploy Verification은 **새 `assessment_id`**로 저장한다. `ASSESSMENT#{assessment_id}` item은
  `phase`, `source_assessment_id`, `deployment_id`를 저장하고 result/finding SK 구조는 바꾸지 않는다.
  새 Initial Assessment도 `phase=INITIAL`을 명시한다. 기존 phase 없는 record는 두 correlation이 모두
  없을 때만 `INITIAL`로 복원하며, verification의 correlation 누락·부분 값·자기 참조는 fail-closed한다.
  result SK에 phase가 없으므로 같은 Assessment에 재평가 결과를 append하면 immutable 조건부 write가
  충돌한다.
- 비교 결과(Finding Resolution, 점수·Coverage delta)는 별도 item으로 저장하지 않는다. 두 immutable
  Assessment에서 읽을 때 계산하는 projection이다. 억제 여부도 저장하지 않고 조회 시 유효한 예외를
  join해 표시만 한다. 예외는 만료되므로 저장하면 만료 후 과거 결과가 사실과 달라진다.
- Terraform state는 이 metadata table에 두지 않는다. 고객 bootstrap stack이 만드는 별도 S3 state
  bucket과 DynamoDB lock table을 사용하고, state key는 `(repository_id, workspace)`로 분리한다.
  workspace 이름은 `{customer_id}-{repository_id}`이며 Repository 승인 시점에 두 ID를
  `^[A-Za-z0-9_-]+$`로 검증해 배포 시점에 이름이 깨지지 않게 한다.
- 감사 event의 **종류**를 담는 정본 필드명은 `event_type`이고 값은 `AuditEventType`
  (`packages/contracts/audit.py`) 어휘를 쓴다. `action`은 도메인 payload 전용이다 —
  `REMEDIATION_DECIDED` audit item은 한 item에 `event_type`(audit 종류)과 `action`
  (`RemediationAction` 값)을 다른 뜻으로 함께 쓰므로, 두 개념에 같은 필드명을 쓰면 값이 충돌한다.
  `action`을 종류 필드로 쓰던 세 곳(`DEPLOYMENT_APPROVED`, `POLICY_SOURCE_APPROVED`,
  `POLICY_PROFILE_PUBLISHED`)은 `event_type`으로 개명했고 다섯 writer 모두 같은 필드명을 쓴다.
  M3에서 `DEPLOYMENT_REQUESTED`, `DEPLOYMENT_REJECTED`, `APPLY_DISPATCHED`, `APPLY_COMPLETED`,
  `APPLY_FAILED`, `POST_DEPLOY_VERIFIED`, `MANUAL_RECONCILIATION_REQUIRED`가 ADR-0019 합의와 함께
  추가된다.

## Example items

```json
{
  "PK": "CUSTOMER#cust_123",
  "SK": "ASSESSMENT#asm_456",
  "entity_type": "ASSESSMENT",
  "customer_id": "cust_123",
  "repository_id": "repo_123",
  "policy_profile_id": "profile_001",
  "phase": "INITIAL",
  "status": "COMPLETED",
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
  "SK": "ASSESSMENT#asm_456#PLAN",
  "entity_type": "ASSESSMENT_EVALUATION_PLAN",
  "customer_id": "cust_123",
  "assessment_id": "asm_456",
  "planned_evaluations": 3,
  "planned_coordinates": [
    {"resource_id": "s3_bucket_logs", "rule_id": "S3-PUBLIC-001", "perspective": "IAC"},
    {"resource_id": "s3_bucket_logs", "rule_id": "S3-PUBLIC-001", "perspective": "AWS_ACTUAL"},
    {"resource_id": "s3_bucket_logs", "rule_id": "S3-PUBLIC-001", "perspective": "DRIFT"}
  ],
  "completed_evaluations": 3
}
```

```json
{
  "PK": "CUSTOMER#cust_123",
  "SK": "ASSESSMENT#asm_456#FINDING#finding-2bf4c6a1454e6b9d2d9adf73",
  "entity_type": "FINDING",
  "customer_id": "cust_123",
  "assessment_id": "asm_456",
  "rule_id": "S3-PUBLIC-001",
  "rule_version": "2026-08",
  "perspective": "AWS_ACTUAL",
  "status": "FAIL",
  "severity": "HIGH",
  "evidence_references": ["aws:s3:bucket/example#read-resource"]
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
  duplicate Queue delivery cannot advance a Job. C Remediation Worker는 GSI1에서 exact Job revision을
  확인한 뒤 customer partition의 Remediation context/decision을 consistent read하며 queue의 action이나
  tenant state를 신뢰하지 않는다.
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
| Deployment item 필드와 상태 전이, workflow event key (ADR-0019) | A + D | M3 착수 전 | Deployment 상태 저장·조회, apply 재검증 |
| Terraform state bucket/lock table 소유와 state key 분리 (ADR-0019) | D + Security | M3 착수 전 | plan-apply 재현성, 고객 bootstrap 확장 |
| 검증 Assessment 필드 확장(`phase`, `source_assessment_id`, `deployment_id`) (ADR-0020) | C + A | M3 착수 전 | Post-Deploy Verification 저장, before/after 비교 |
