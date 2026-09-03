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
| Policy profile (current pointer) | `CUSTOMER#{customer_id}` | `POLICY_PROFILE#{policy_profile_id}` | 새 Assessment가 고를 현재 판본. `current_version`을 함께 담고, 교체는 `expected_current_version` 조건부 write로 보호된다 |
| Policy profile (version history) | `CUSTOMER#{customer_id}` | `POLICY_PROFILE#{policy_profile_id}#VERSION#{version}` | Immutable 판본. 판본을 고정한 Assessment가 나중에 직접 읽는다 |
| Policy source | `CUSTOMER#{customer_id}` | `POLICY_SOURCE#{source_id}#VERSION#{version}` | Policy artifact identity, version, hash |
| Policy ingestion | `CUSTOMER#{customer_id}` | `POLICY_INGESTION#{source_id}#VERSION#{version}` | Upload validation, parser/normalization status and immutable Artifact references |
| Policy authoring request | `CUSTOMER#{customer_id}` | `POLICY_SOURCE#{source_id}#VERSION#{version}#REQUEST` | 추출 요청의 durable record. `authoring_run_id`와 최초 `requested_at`을 고정해 worker 재시도가 같은 실행이 되게 한다 |
| Policy authoring manifest | `CUSTOMER#{customer_id}` | `POLICY_SOURCE#{source_id}#VERSION#{version}#AUTHORING` | 한 추출 실행의 상태·개수·`result_digest`·provenance. **Review와 Approval은 `READY`만 읽는다** |
| Policy authoring result | `CUSTOMER#{customer_id}` | `POLICY_SOURCE#{source_id}#VERSION#{version}#{CANDIDATE\|UNSUPPORTED\|REJECTED}#{digest}` | 결과 하나당 item 하나. key는 Requirement의 결정적 digest이므로 worker 재시도가 같은 후보를 두 번 만들지 않는다 |
| Policy candidate extraction (legacy) | `CUSTOMER#{customer_id}` | `POLICY_SOURCE#{source_id}#VERSION#{version}#CANDIDATES` | authoring 이전 경로가 쓴 단일 item. manifest가 없는 판본에만 남아 있다 |
| Rule metadata | `CUSTOMER#{customer_id}` | `RULE#{rule_id}#VERSION#{version}` | 승인된 Rule. `entity_type = POLICY_RULE`과 `lifecycle = APPROVED`를 명시하며, Catalog는 그 둘을 만족하는 item만 Rule로 인정한다 |
| Golden dataset case | `CUSTOMER#{customer_id}` | `GOLDEN_CASE#{case_id}#RUBRIC#{rubric_version}` | Expected evaluation range and artifact reference |
| Job | `CUSTOMER#{customer_id}` | `JOB#{job_id}` | Async workflow state and current step |
| Job checkpoint | `CUSTOMER#{customer_id}` | `JOB#{job_id}#CHECKPOINT#{revision}` | Immutable resumable step, next resource, retry metadata, Artifact references |
| Assessment | `CUSTOMER#{customer_id}` | `ASSESSMENT#{assessment_id}` | Assessment metadata, `phase`, **모든 phase가 갖는** `policy_profile_version`, optional verification provenance (`source_assessment_id`/`deployment_id`)와 verification 전용 pin(`model_profile_id`/`rubric_version`); report projection exposes score and coverage |
| Assessment evaluation plan | `CUSTOMER#{customer_id}` | `ASSESSMENT#{assessment_id}#PLAN` | Immutable planned applicable Resource × Rule × Perspective **set** (`planned_coordinates`), its count, and the completion counter |
| Assessment result | `CUSTOMER#{customer_id}` | `ASSESSMENT#{assessment_id}#RESULT#{resource_id}#RULE#{rule_id}#PERSPECTIVE#{perspective}` | IaC, Actual, or Drift Resource × Rule judgment, evidence, assessed commit, and evaluation time |
| Finding | `CUSTOMER#{customer_id}` | `ASSESSMENT#{assessment_id}#FINDING#{finding_id}` | Actionable result, severity, and copied assessed commit/evaluation time |
| Remediation | `CUSTOMER#{customer_id}` | `REMEDIATION#{remediation_id}` | Immutable `RemediationDecision`, C context (including optional source Assessment identity), source Finding, optional Job 참조, C Worker 결과 `result`, 그리고 D가 연 PR 사실 `pull_request`(number/url/head·base branch/head commit, 한 번만 기록) |
| Remediation patch content | `CUSTOMER#{customer_id}` | `REMEDIATION_PATCH#{content_sha256}` | `RemediationPatch`가 가리키는 canonical patch 바이트(`content`, ASCII JSON), `byte_size`, finding/base commit. digest가 key이므로 content-addressed·immutable이며 조건부 put으로 다른 바이트의 덮어쓰기를 막는다. 300KB 상한 |
| Remediation exception | `CUSTOMER#{customer_id}` | `REMEDIATION_EXCEPTION#RULE#{rule_id}#VERSION#{version}#EXCEPTION#{exception_id}` | Admin-approved enum reason, optional Resource scope, approval/expiry binding |
| Deployment | `CUSTOMER#{customer_id}` | `DEPLOYMENT#{deployment_id}` | Plan, approval, apply, verification state |
| Approval | `CUSTOMER#{customer_id}` | `DEPLOYMENT#{deployment_id}#APPROVAL#{approval_id}` | Approver, `commit_sha`, `plan_hash` binding |
| Audit event | `CUSTOMER#{customer_id}` | `AUDIT#{occurred_at}#{event_id}` | Immutable application audit trail. `GET /audit-events`는 이 prefix를 SK 역순으로 읽는다 |

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
| Base table | `CUSTOMER#{customer_id}` + `AUDIT#` prefix (descending) | Admin 감사 이력 페이지 조회. SK가 `occurred_at`으로 시작하므로 최신순이 key 순서 읽기이고 정렬이 필요 없다 |
| Base table | `CUSTOMER#{customer_id}` + `REMEDIATION_EXCEPTION#RULE#{rule_id}#VERSION#{version}` prefix | 고객의 exact Rule version 예외 조회; resource scope는 조회 후 좁힌다 |
| `GSI1` | `GSI1PK = JOB#{job_id}`, `GSI1SK = CUSTOMER#{customer_id}` | Resolve a Job ID, then verify customer scope before return |
| `GSI1` | `GSI1PK = DEPLOYMENT#{deployment_id}`, `GSI1SK = CUSTOMER#{customer_id}` | apply 완료 Event의 deployment id로 소유 고객을 해석한 뒤 그 scope로 record를 다시 읽는다 |
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
`repository_id`, `policy_profile_id`, `policy_profile_version`, `job_id`를 영속화하므로 Worker는
최소 Queue payload만으로도 평가 selector를 복원한다. 판본이 없는 record는 최신 pointer로 조용히
대체하지 않고 실패한다 — 그렇게 대체하면 실행 도중 게시된 새 Profile이 이미 계획된 평가의 Rule
집합을 바꾸고 그 사실이 어디에도 남지 않는다(ADR-0020 amendment). Outbox는 `GSI2PK = OUTBOX#PENDING`으로 pending 전송을 조회하며,
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

C Remediation Worker의 결과는 같은 `REMEDIATION#{remediation_id}` item에 `result` 속성으로
conditional update(`attribute_not_exists(result)`)한다. plan facts를 `DEPLOYMENT#` item에 채우는
것과 같은 관례이며 이유도 같다 — at-least-once 재시도는 흡수되고, 다른 결과는 이미 기록된 것을
덮어쓰지 못한다. `result.kind`는 `RemediationAction` 값(`TERRAFORM_PATCH`/`ACTUAL_SYNC`)이고
payload는 각각 `patch`(`RemediationPatch`)와 `sync_target`(`RemediationSyncTarget`)이다. 별도
`#RESULT` item으로 나누지 않는 이유는 Deployment 생성 경로에 있다 — 생성은 decision과 결과를 함께
확인해야 하는데(ADR-0019 §4), 한 item이면 그 확인이 단일 strongly-consistent get이고, 두 item이면
decision은 보이는데 결과는 아직 안 보이는 중간 상태를 읽을 수 있다.

Deployment 생성의 대상 commit은 action마다 다르다(ADR-0019 §3). `ACTUAL_SYNC`는 저장된
`sync_target.commit_sha`(이미 `IAC` 관점을 통과한 default branch commit)를 그대로 쓰므로 GitHub
read가 필요 없다. `TERRAFORM_PATCH`는 사람이 merge한 **default branch의 merge commit**이 대상이며,
그 값은 저장돼 있지 않고 D 소유 read-only port(`DeploymentCommitResolver`)가 GitHub에서 해석한다.
merge 전이면 해석 결과가 없고, 생성은 도달 불가로 거절된다. patch의 `base_commit_sha`를 대신 쓰지
않는다 — base는 patch를 만든 시점의 스냅샷이고, 그걸 apply하면 사람이 승인하지 않은 코드를
배포하게 된다.

## M3 planned deployment and verification storage

ADR-0020은 `Accepted`이고 C 비교 Contract는 구현됐다. ADR-0019의 Deployment 상태 기계도
`Accepted`이며 아래 정의대로 구현한다. A의 Deployment 생성·조회·거절과 D Worker의 plan/apply/verify
store가 구현됐다. apply 완료 Event 경계는 아래 "완료 Event 경계(A/D 공유 계약)"로 확정한다 —
A/D는 이 key/field 경계 밖에 검증 결과를 쓰지 않는다. live plan 어댑터는 승인 target의
customer-installed workflow만 dispatch하고, GitHub run/artifact를 재조회·검증한다.

| Entity | PK | SK | Purpose |
| --- | --- | --- | --- |
| Deployment workflow event | `CUSTOMER#{customer_id}` | `DEPLOYMENT#{deployment_id}#EVENT#{run_id}` | 하나의 apply run에 대한 item. `status`로 두 단계를 구분한다: `PENDING_VERIFICATION`(A/EventBridge가 완료 Event 수신 시 `run_id`만 담아 write) → `VERIFIED`(D Worker가 재조회로 검증한 `WorkflowRunFacts`로 확정). 같은 `run_id`는 한 번만 기록된다 |
| Deployment apply dispatch | `CUSTOMER#{customer_id}` | `DEPLOYMENT#{deployment_id}#DISPATCH` | apply `workflow_dispatch` 확인(`ApplyDispatchReceipt`). 결정적 키라 중복 dispatch가 두 번째 record를 만들지 않는다 |

- `DEPLOYMENT#{deployment_id}` item은 대상 `commit_sha`, `plan_hash`, 투영된 canonical JSON plan과
  saved binary plan의 Artifact reference, plan 시점의 Terraform **state `lineage`와 `serial`**,
  plan을 만든 Actions run 좌표 `plan_run`(deployment/repository/run_id — apply가 그 run의 saved
  artifact를 내려받는다), `remediation_id`, `source_assessment_id`, 그리고 검증 후의
  `verification_assessment_id`를 보관한다. `serial` 단독으로는 state 재생성을 잡지 못하므로 두 값을
  쌍으로 둔다.
- plan facts(`plan_hash`·plan/binary artifact·state·`plan_run`·`plan_summary`)는 D Worker가
  `PLAN_COMPLETED`에서 `DeploymentPlanStore`로 `DEPLOYMENT#{deployment_id}` item에 conditional
  update(`attribute_not_exists(plan_hash)`)로 채운다. `plan_summary`는 C readiness가 요구하는
  `refreshed`/`has_destructive_changes`/`mapped_resource_ids` 세 값이며, 승인이 plan보다 나중
  invocation에서 일어나므로 durable해야 한다(ADR-0019 §1-a). readiness 판정 자체는 저장하지 않고
  이 요약과 Worker context에서 read 시 파생한다 — `DeploymentStatus`와 같은 원칙이다. 같은 revision의 재시도는 흡수되고(멱등), 다른 plan은 덮어쓰지 못한다.
  D Worker의 authoritative work는 이 item과 `JOB#{job_id}`(revision)·approval item을 합성해 만든다.
- **`DeploymentStatus`는 이 item에 저장하지 않는다.** 배포 생애주기 위치는 `JobStatus`와
  `JobCurrentStep`에 이미 저장돼 있고, 표현 값은 read 시 `derive_deployment_status()`로 계산한다.
  apply 시작 전 Job이 terminal(`FAILED`/`CANCELLED`)이면 진행 중 step 대신 `MANUAL_REVIEW`로
  파생한다(거절은 그보다 먼저 `REJECTED`). M1 Readiness Score와 같은 원칙이며, 저장된 두 번째 사본이
  없으므로 마이그레이션도 정합성 규칙도 필요 없다 (ADR-0019 §8). 상태별 목록 조회가 필요해지면
  `GSI2PK = CUSTOMER#{customer_id}#DEPLOYMENT_STATUS#{status}`로 materialize하며, 그때까지 채우지
  않는다.
- Deployment 생성은 `DEPLOYMENT#{deployment_id}` item, `JOB#{job_id}`, `OUTBOX#JOB#{job_id}`
  (`RUN_DEPLOYMENT`), `DEPLOYMENT_REQUESTED` audit event를 **하나의 조건부 transaction**으로 쓴다
  (`attribute_not_exists`). 생성 시점에는 plan facts(`plan_hash`·plan/binary artifact·state)가
  아직 없고 PLAN_COMPLETED 이후 채워지며, 그 값들은 all-present-or-all-absent다. Deployment record가
  없으면 Job도 없다.
- 거절은 결정적 키 `DEPLOYMENT#{deployment_id}#REJECTION` item과 `DEPLOYMENT_REJECTED` audit,
  Job의 `CANCELLED` 전이를 한 transaction으로 쓴다. 결정적 키가 재거절과 같은 `plan_hash` 재승인을
  막는다 (ADR-0019 §8).
#### 완료 Event 경계 (A/D 공유 계약, ADR-0019 §7)

apply 완료 Event를 처리하는 책임을 A(인프라/EventBridge)와 D(Worker)로 나눈다. 이 계약은 A PR과
D PR이 각자 구현하되 같은 문장을 정본으로 인용한다.

- **Queue payload는 여전히 최소다:** `job_id`, `expected_revision`, `command`만 흐른다. `run_id`는
  큐에 싣지 않는다(payload 불신 원칙, ADR-0019 §7).
- **A / EventBridge (writer, 구현됨):** GitHub Actions apply run 완료 Event를 받으면, 그 event에서
  얻은 `run_id`로 `DEPLOYMENT#{deployment_id}#EVENT#{run_id}` item을 `status=PENDING_VERIFICATION`으로
  write하고(담는 값은 `run_id`뿐, run의 conclusion/facts는 담지 않는다 — 신뢰 대상이 아니므로),
  같은 deployment의 Job을 다음 revision으로 올리며 `APPLY_COMPLETED` task를 Deployment Queue에 넣는다.
  이 예약 item은 "재조회할 run 좌표 포인터"이지 사실 기록이 아니다. GitHub Actions에는 DynamoDB
  write 권한을 주지 않는다 — `ApplyCompletionFunction`이 유일한 write 경로다.
  세 write는 하나의 조건부 transaction이다. Job revision만 올라가고 예약이 없으면 D가
  `run_reference`를 못 찾아 fail-closed되고, 예약만 되고 task가 없으면 아무도 검증하지 않는다.
  EVENT item의 `attribute_not_exists(SK)`와 Job의 revision 조건이 함께 at-least-once 재전달을
  흡수한다. Event가 지목하는 것은 deployment뿐이고 **소유 고객은 Event가 아니라 저장에서 해석한다**
  — `DEPLOYMENT#` item의 `GSI1PK = DEPLOYMENT#{deployment_id}`로 id를 풀고 그 customer scope로
  record를 다시 읽는다(Job 해석과 같은 방식). 소유자를 payload에서 받으면 Event를 만들 수 있는
  누구든 남의 Job을 재개시킬 수 있다. 이미 terminal인 Job(거절·실패·완료)은 재개하지 않는다.
- **D Worker (reader → verifier):** `APPLY_COMPLETED`를 소비하면 `(job_id, revision)`으로 work를
  다시 읽고, 그 deployment의 `PENDING_VERIFICATION` EVENT item에서 `run_id`를 읽어 `run_reference`를
  만든다. 그 `run_id`로 GitHub Actions run을 재조회(`WorkflowRunReader`)해 승인 사실
  (repository/workflow_path/ref/commit/plan_hash/conclusion)과 대조하고, 성공·일치한 결과만
  `DeploymentVerificationStore`가 같은 `#EVENT#{run_id}` item을 검증된 `WorkflowRunFacts`로
  `status=VERIFIED`로 확정한다. 하나라도 다르면 재시도 없이 차단한다(MANUAL_REVIEW로 파생).
  예약 item이 없으면(`run_reference` 부재) Worker는 `APPLY_COMPLETED`를 fail-closed한다.
- **A / 검증 시작 (구현됨, ADR-0020 §1·§7):** D Worker가 run facts를 `VERIFIED`로 확정한 직후
  `PostDeployVerificationService`가 검증 Assessment를 시작한다. 원 Assessment의 `ASSESSMENT#` item
  (Repository·`policy_profile_id`·`policy_profile_version`), `#PLAN` item의 `planned_coordinates`,
  결과 item의 `model_profile_id`/`rubric_version`을 읽어 scope를 pin하고, 다음 네 write를 **하나의
  조건부 transaction**으로 쓴다(`DynamoDbPostDeployVerificationStore`): 새 `ASSESSMENT#{verification_id}`
  item(`attribute_not_exists(SK)`, `phase=POST_DEPLOY_VERIFICATION`·`source_assessment_id`·
  `deployment_id`·pin 3종), Deployment `JOB#{job_id}`의 다음 revision(`#revision = :expected`,
  write-once `assessment_id`), `OUTBOX#JOB#{job_id}`의 `ASSESS_RESOURCE` task(overwrite), 그리고
  `DEPLOYMENT#{deployment_id}`의 `verification_assessment_id`(`attribute_not_exists`). 조건 실패는
  같은 apply 완료의 재전달이며 record의 기존 id를 돌려준다. `#PLAN` item은 검증 Assessment Worker가
  원 계획 집합으로 만든다.
- **live plan 경로:** `PlanRequestPort`는 승인 target의 `terraform-plan.yml`만 dispatch하고,
  exact commit과 deterministic display title로 GitHub Actions run을 재조회·완료 확인한 뒤 GitHub
  API artifact ZIP의 canonical plan/state/saved binary를 검증해 저장한다. customer secret과
  protected Environment가 없으면 configuration 단계에서 fail-closed하며 fixture 경로(Mock
  어댑터)는 세 command를 모두 구동한다.
- Post-Deploy Verification은 **새 `assessment_id`**로 저장한다. `ASSESSMENT#{assessment_id}` item에
  `phase`, `source_assessment_id`, `deployment_id`를 추가하고, result/finding SK 구조는 바꾸지 않는다.
  result SK에 phase가 없으므로 같은 Assessment에 재평가 결과를 append하면 immutable 조건부 write가
  충돌한다. 새 Initial Assessment도 `phase=INITIAL`을 명시하며, phase 없는 legacy record는 두
  correlation이 모두 없을 때만 `INITIAL`로 복원하고 누락·부분 값·자기 참조는 fail-closed한다.
- 검증 Assessment item은 재사용할 평가 범위를 함께 **고정 저장**한다: `model_profile_id`,
  `rubric_version`, `policy_profile_version`. 세 값은 correlation과 마찬가지로
  `POST_DEPLOY_VERIFICATION`에만 존재하며 셋 다 있거나 셋 다 없다(부분 저장은 `ValueError`). apply와
  재조회 사이 Profile이 교체되면 pin 없이는 다른 rubric으로 평가돼 조용히 비교 불가가 되므로 durable해야
  한다(ADR-0020 §2·§3). Worker runtime은 저장된 pin이 자신의 승인 Model Profile·rubric과 다르면
  Assessment를 거부하고, planned 집합은 파생하지 않고 원 Assessment의 PLAN item에서 읽어 재사용하며,
  `policy_profile_version` pin을 Policy Context 해석에 넘긴다. `plan_verification_assessment()`가 이
  범위 고정을 생성 경계에서 강제한다.
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
  M3의 `DEPLOYMENT_REQUESTED`, `DEPLOYMENT_REJECTED`, `APPLY_DISPATCHED`, `APPLY_COMPLETED`,
  `APPLY_FAILED`, `POST_DEPLOY_VERIFIED`, `MANUAL_RECONCILIATION_REQUIRED`는 ADR-0019 합의대로
  `AuditEventType`에 들어갔다. 앞의 둘은 Deployment 생성·거절 writer가 이미 쓰고, 나머지 다섯은
  apply/verify 경계가 dev에 배선될 때 같은 어휘로 쓴다 — 조회 경로가 값별 분기 없이 한 vocabulary만
  보도록 어휘를 먼저 고정한다.
- Admin 조회(`GET /audit-events`)는 `CUSTOMER#{customer_id}` partition의 `AUDIT#` prefix를 SK
  역순으로 읽는 단일 query다. scan은 쓰지 않는다. 응답에는 `event_id`/`event_type`/`occurred_at`/
  `customer_id`와 writer별 payload(`details`)만 담고 DynamoDB key·GSI·`entity_type`·`version` 같은
  저장 bookkeeping은 제외한다. 종류 필터는 key 조건이 아니라 filter이므로 필터가 걸린 페이지는
  `limit`보다 짧을 수 있고, 그때도 `next_cursor`가 남으면 이력은 계속된다.

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
| Deployment item 필드와 상태 전이, workflow event key (ADR-0019) | A + D | ~~M3 착수 전~~ **결정됨: ADR-0019 `Accepted`** | Deployment 상태 저장·조회, apply 재검증 (M3 구현 대기) |
| Terraform state bucket/lock table 소유와 state key 분리 (ADR-0019) | D + Security | ~~M3 착수 전~~ **결정됨: ADR-0019 `Accepted`** | plan-apply 재현성, 고객 bootstrap 확장 (M3 구현 대기) |
| 검증 Assessment 필드 확장(`phase`, `source_assessment_id`, `deployment_id`) (ADR-0020) | C + A | M3 착수 전 | Post-Deploy Verification 저장, before/after 비교 |
