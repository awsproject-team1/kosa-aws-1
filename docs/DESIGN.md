# DESIGN — Cloud Governance & Compliance Agent

## Architecture

```text
React SPA (S3 + CloudFront)
→ Cognito (JWT)
→ API Gateway

Customer policy upload
→ customer-scoped S3 original
→ validation + format-specific parser
→ normalized policy artifact
→ Control/Rule review and approval
→ version-pinned Policy Profile

Natural-language request
→ Parent Router Lambda (Policy Q&A + intent/scope proposal; sync, max 30s)
→ response or user-confirmed Job creation

Explicit Assessment / Remediation / Deployment request
→ corresponding API Lambda (JWT/scope validation, Job + WorkflowTask creation)
→ corresponding SQS Standard Queue + DLQ
→ corresponding Worker Lambda (one LangGraph subgraph)
→ Bedrock role-specific Model Profile + constrained tools

DynamoDB: Job, revision-bound checkpoint, domain metadata
S3: policy originals, IaC/AWS snapshots, reports, patches, plans
GitHub App: customer Terraform repository
GitHub Actions: CI / plan / approved apply
GitHub Actions completion event → EventBridge → Deployment Queue → Deployment Worker
```

Parent Router는 Queue를 소비하거나 장시간 실행을 이어가지 않는다. 역할별 Worker는 Assessment,
Remediation, Deployment 중 자신의 subgraph만 실행한다. SQS는 전달 수단이며 상태 정본은 DynamoDB와
S3이다. 플랫폼은 고객 AWS Account의 `us-east-1`에 배포하며, MVP에서 Backend Lambda와 Worker는
고객 기존 VPC에 연결하지 않는다.

```text
Customer AWS Account (platform deployment; existing Workload VPC not connected)
├── Edge / Frontend: S3 + CloudFront React SPA, Cognito User Pool
├── API: API Gateway, Parent Router Lambda, Assessment/Remediation/Deployment API Lambda
├── Asynchronous Workflow
│   ├── Assessment SQS + DLQ → Assessment Worker Lambda (ASSESSMENT)
│   ├── Remediation SQS + DLQ → Remediation Worker Lambda (REMEDIATION)
│   └── Deployment SQS + DLQ → Deployment Worker Lambda (DEPLOYMENT)
├── AI / Tools: Bedrock role-specific Model Profiles, Policy Context, AWS Resource Tool (RO), GitHub App
├── State / Artifact / Observability: DynamoDB checkpoint/metadata, S3 artifacts, CloudWatch/CloudTrail
└── Deployment integration: GitHub Actions OIDC CI/Plan/approved Apply → EventBridge → Deployment Queue

Customer Workload (EC2 / RDS / ALB / S3)
└── AWS Resource Tool read-only access or approved GitHub Actions Apply only
```

## Components

- Frontend: React SPA. UI는 권한별 노출만 제어하고 Authorization은 Backend가 강제한다.
- Backend: Auth/User, Job/Assessment, Policy/Rule, GitHub/Remediation, Approval/Deployment Lambda로 분리한다.
- Workflow: LangGraph Parent는 자연어 Orchestration과 Policy Q&A를 맡고, 아래에
  `ASSESSMENT`, `REMEDIATION`, `DEPLOYMENT` subgraph를 둔다. C가 Assessment/Remediation Agent와
  Worker orchestration을 소유하고, D는 injected GitHub/Terraform 실행 port와 Deployment Worker를
  소유한다. 명시적 UI/API 요청은 대응 Subgraph로 직접 진입하고, 자연어 요청만 Parent
  Orchestrator Agent를 거쳐 의도·후보 scope에 맞는 Subgraph로 라우팅한다.
- Storage: DynamoDB에는 상태와 메타데이터, S3에는 대형 Artifact를 저장한다.
- Policy ingestion: 고객 정책 원본은 immutable S3 Artifact로 보존하고 비동기 검증·형식별 Parser가
  공통 Policy Document로 정규화한다. 원본 업로드만으로 Assessment에 활성화하지 않으며, 사람이
  승인한 Source/Rule/Profile version만 Policy Context가 사용한다. 상세 경계는
  `docs/POLICY_INGESTION.md`가 정본이다.
- Async execution: Assessment, Remediation, Deployment Worker는 역할별 SQS Standard
  Queue에서 `WorkflowTask`를 받아 실행한다. GitHub Actions의 Plan/Apply 완료 Event는
  EventBridge가 Deployment Queue로 전달한다.
- Tools: Policy Context, External Evidence, GitHub Integration, AWS Resource(Read Only)로 제한한다.

## Responsibility boundary

Code는 Customer/AWS Account/Repository/Policy Profile 범위, Tool 권한, 출력 스키마, 점수 범위와 (도입 시) Score Anchor, Evidence reference, 상태 저장, Coverage를 검증한다. AI Evaluator는 적용 Rule, 필요한 Evidence, 판정, Severity, 0–100 점수, Rationale 및 Source Score/Risk를 선택한다. 반복 실행 편차가 목표인 ±10점을 지속적으로 넘을 때에만 고정 Anchor 집합에서 점수를 선택하도록 전환한다.

Parent Orchestrator Agent는 자연어 요청의 의도와 후보 selector를 해석한다. 정책 질의는
Parent 안에서 Policy Q&A로 처리하고, 실행 의도는 Assessment, Remediation, Deployment 중
하나를 제안한다. Parent는 직접 Job을 생성하거나 권한을 결정하지 않는다. Backend가 JWT
scope와 입력 Contract를 검증하고, Assessment·Remediation·Deployment 실행에는 사용자
확인을 요구한다. 명시적 UI/API 요청에는 Parent를 거치지 않는 결정적 진입 경로를 사용한다.
Parent(Policy Q&A 포함), Assessment, Remediation, Deployment에는 역할별로 승인된 Model
Profile을 고정한다. 각 Profile은 Model ID/Version, Prompt/Rubric Version, Golden Dataset
평가 결과와 승인 이력을 가지며, 변경 시 품질 Gate를 다시 통과해야 한다.

## State and execution

Parent State에는 전체 Artifact가 아닌 `job_id`, `user_id`, `repository_id`, `policy_profile_id`, `assessment_id`, `finding_id`, `remediation_id`, `deployment_id`, `current_step`, `approval_status`만 둔다. 장시간 Assessment/Remediation은 `202 + job_id`로 추적한다.

Initial Assessment는 Terraform Snapshot과 Read-Only AWS Snapshot을 같은 관리 대상에
매핑한 뒤, `IAC`, `AWS_ACTUAL`, `DRIFT` 관점의 `Resource × Rule` 결과를 별도로
생성한다. `DRIFT`는 IaC와 Actual의 불일치를 나타내는 Finding 근거이며, AI나 Runtime에
직접 Write 권한을 주지 않는다. IaC가 안전하지 않으면 Remediation은 IaC를 원하는 안전한
상태로 수정한다. IaC가 이미 안전하고 Actual만 drift된 경우에는 Patch 없이 현재 IaC
commit을 동기화 대상으로 삼는다. Deployment Readiness는 refresh된 Terraform Plan으로
Patch 또는 동기화 대상이 현재 Actual에 적용 가능한지 검증한다. Human Approval 뒤 Apply가
Actual을 변경하며, Post-Deploy Verification이 Actual Compliance와 Drift 해소를 확인한다.
Terraform 관리 밖이거나 안전한 매핑이 없는 리소스는 `MANUAL_REVIEW`로 남긴다.

Remediation action의 정본은 B의 `RemediationDecision` 하나다. A는 customer-scoped target과
만료되는 예외를 읽어 판정하고 context/decision/Job/Outbox/audit를 저장한다. C Remediation Worker는
revision-bound authoritative work를 다시 읽어 `TERRAFORM_PATCH`에는 injected Patch port,
`ACTUAL_SYNC`에는 injected Sync port 하나만 호출한다. `MANUAL_REVIEW`와 `SUPPRESSED`는 Job을 만들지
않는다. D의 `RUN_DEPLOYMENT`는 이 단계와 분리된 Deployment Worker 명령이다.

M3 승인 배포 경계는 ADR-0019가 `Proposed`로 남아 있고, Post-Deploy Verification 비교 경계는
ADR-0020이 `Accepted`로 정의한다. Deployment는 A가 발급한
`deployment_id`로 시작해 `PLAN_REQUESTED → PLAN_COMPLETED → READINESS_EVALUATED →
WAITING_APPROVAL → APPROVED → APPLYING → APPLIED → VERIFYING → VERIFIED`를 조건부 write로
전이하고, `BLOCKED`, `MANUAL_REVIEW`, `REJECTED`, `VERIFICATION_INDETERMINATE`로 분기한다. 하나의
Deployment는 하나의 Job이며 Job의 write-once `assessment_id`는 검증 Assessment를 가리킨다. 원
Assessment는 Deployment record가 참조한다. Post-Deploy Verification은 원 Assessment를 덮어쓰지 않고
`phase`, `source_assessment_id`, `deployment_id`를 가진 새 Assessment로 저장하며, 원 Assessment와 같은
Policy Profile version·`model_profile_id`·`rubric_version`으로 같은 평가 계획을 다시 평가한다. 변화가
인프라 개선인지 모델 차이인지 구분할 수 없게 되므로 최신 Profile로 재평가하지 않는다. 재평가는 apply
완료 확인 후 30초 지연 뒤 시작하고, 기대와 다른 Actual은 총 세 번까지 재조회한 뒤에도 다르면
`VERIFICATION_FAILED`가 아니라 `VERIFICATION_INDETERMINATE`로 사람에게 보낸다. AWS 전파 지연을 정책
위반으로 확정하지 않는다. Finding 해소 여부와 점수·Coverage 변화는 AI 판정이 아니라 두 immutable
Assessment의 결정적 비교이며, planned 평가 집합이나 Profile/rubric이 다르면 delta를 만들지 않고
비교 불가로 표시한다.

Assessment, Remediation, Deployment API는 검증·Job 저장 후 대응 Queue에 최소
`WorkflowTask(job_id, expected_revision, command)`만 전송하고 `202 + job_id`를 반환한다.
Queue는 진행 상태를 보관하지 않는다. Worker는 DynamoDB에서 revision-bound checkpoint를
읽고 S3 Artifact 참조를 복원한 뒤, Assessment는 리소스 하나의 Rule 묶음만 처리한다.
Lambda timeout은 15분이되, 남은 시간이 3분이 되면 Worker는 새 작업을 시작하지 않고
checkpoint를 조건부 저장한 뒤 다음 `WorkflowTask`를 Queue에 전송하고 종료한다.

Parent는 긴 Policy Q&A Job을 만들지 않는다. Policy Q&A와 자연어 routing은 30초 이내의
동기 응답 예산으로 제한하며, 이를 충족할 수 없는 질문에는 범위를 좁혀 달라고 요청한다.
일시적인 AWS/Bedrock/GitHub 오류는 작업별로 총 세 번 시도한 뒤 DLQ와 `FAILED` Job으로
전환한다. Contract·scope·권한 오류는 재시도하지 않는다. Terraform Apply는 자동 재시도하지
않고, 결과가 불명확하면 AWS Actual과 Terraform 상태를 재조회한 뒤 `MANUAL_REVIEW`와 새
승인으로 처리한다. Admin 재시도는 실패 이력을 보존한 새 Job revision과 Queue 메시지를 만든다.

## Security

- `AgentRuntimeRole`: Customer Workload 읽기 전용 및 필요한 App Data
- `TerraformPlanRole`: plan에 필요한 읽기 중심 권한
- `TerraformDeploymentRole`: 제한된 Infrastructure write
- 고객 AWS Account의 Read-only Role AssumeRole은 customer-role 연결마다 생성한 랜덤
  `ExternalId`를 trust policy와 요청 양쪽에서 일치시켜 confused deputy를 방지하며, 임시
  자격증명은 만료 60초 전까지만 메모리에 재사용한다.
- `GitHubWorkflowEventRole`: GitHub Actions OIDC에서 Plan/Apply 완료 Event만 EventBridge에
  게시하는 최소 권한
- 고객 관리자는 첫 배포 전에 제공된 bootstrap stack을 한 번 실행해 GitHub OIDC deployment role,
  versioned Lambda-code bucket, CloudFormation execution role을 만든다. GitHub OIDC trust는
  정확한 repository와 두 protected Environment subject로 제한하며, bootstrap은 customer workload
  접근 권한을 만들지 않는다.
- M0 foundation 배포는 서로 다른 두 protected GitHub Environment를 사용한다. 첫 job은 expected
  AWS account를 OIDC action, STS caller identity, S3 expected-owner 조건으로 검증하고 Lambda ZIP을
  commit-qualified key에 조건부 생성 또는 exact metadata/checksum으로 재사용한다. 두 번째
  Environment에서 사람이 commit/key/SHA-256/S3 Version ID를 승인한 뒤, 배포 job이 exact version을
  재검증하고 모든 Lambda에 고정한다.
- Apply 전 승인한 `commit_sha`와 `plan_hash`를 재검증한다.
- (ADR-0019, `Proposed`) 승인 대상 plan artifact는 `terraform show -json`을 canonical JSON으로
  정규화한 바이트이며 `plan_hash`는 그 SHA-256이다. binary saved plan은 별도 artifact로 두고 apply는
  그 saved plan만 적용한다. Apply 직전에는 `plan_hash`와 함께 plan 시점의 Terraform **state serial**도
  재검증한다. hash 일치는 같은 계획을 보장하지만 같은 state를 보장하지 않는다.
- (ADR-0019, `Proposed`) Terraform state는 고객 bootstrap stack이 만드는 별도 S3 bucket과 DynamoDB
  lock table에 두고 state key를 `(repository_id, workspace)`로 분리한다. apply 대상 commit은 default
  branch의 merge commit이며 PR head commit의 plan은 승인 대상이 아니다.
- (ADR-0019, `Proposed`) 고객 repository의 plan/apply workflow는 `ci/terraform/` template으로 제공하고
  고객 관리자가 1회 수동 설치한다. GitHub App은 `contents`/`pull_requests` write만 요청하고
  `workflows: write`는 요청하지 않는다. App이 workflow를 쓸 수 있으면 승인 경계를 우회한 임의 코드
  실행 경로가 생긴다.
- (ADR-0019, `Proposed`) 승인은 apply를 트리거하지 않는다. A는 승인 record와 dispatch outbox만 쓰고 D
  Deployment Worker가 재검증 뒤 dispatch한다. 이중 apply는 `APPROVED → APPLYING` 조건부 전이로 막는다.
  Plan/Apply 완료 Event는 신호일 뿐이며 D가 `run_id`로 Actions run을 다시 읽어 대조한다. GitHub
  Actions에 DynamoDB write 권한을 주지 않는다.
- M0 Worker는 packaged synthetic fixture만 처리하며 ArtifactBucket 접근 권한을 갖지 않는다.
  고객 artifact 접근은 tenant-scoped runtime identity가 구현·검토된 뒤에만 허용한다
  (ADR-0014).
- AI 출력은 Schema/Evidence/Permission/ID 검증과 CI, plan, Human Approval을 통과해야 한다.

## Observability

CloudWatch Metrics/Logs, CloudTrail, X-Ray 또는 OpenTelemetry를 사용한다. 구조화 로그의 상관 키는 `request_id`, `job_id`, `assessment_id`, `rule_id`다. Assessment 성공률, 판정 분포, Lambda 오류/throttle, Bedrock 지연·토큰, Agent Workflow와 Tool 호출, Job 적체, Queue age/DLQ depth, checkpoint·재개 횟수, plan/apply 실패를 관측하고 민감한 Prompt·정책 원문·IaC 전체는 로그에서 마스킹하거나 제외한다. ArtifactBucket의 S3 object read/write는 별도 retained CloudTrail data-event trail과 audit bucket에 기록하며, selector는 artifact bucket만 포함하고 audit destination은 제외한다. Audit event에는 object-key metadata가 남을 수 있으므로 object key에 민감 원문을 넣지 않는다. 실제 고객 배포 전에는 승인된 sandbox에서 controlled artifact Get/Put과 delivered CloudTrail record/log-file validation을 확인한다. 평가마다 Evidence, Tool 호출, Model/Prompt/Rubric/Rule Version, Score, Coverage, Token/Latency/Retry/Validation 결과를 보존한다.

## Evaluation quality gate

Model, Prompt, Rubric, Rule, Policy Document, Context Retrieval 또는 Tool이 바뀌면 Golden Dataset과 반복 평가를 실행한다. 목표는 PASS/FAIL 정확도, Evidence Reference 정확도, 동일 Case 판정 일치율 각각 90% 이상과 Score 반복 편차 ±10점 이내다.

2026-08-31 Bedrock 전체 모델 선별 및 역할별 반복 호출에 따른 현재 추천과 재현 방법은
`docs/evaluations/BEDROCK_MODEL_SELECTION.md`에 기록한다. 모델 배정은 고정 불변값이 아니며
Golden Case 확장 또는 위 품질 입력 변경 시 같은 절차로 재평가한다.

(ADR-0020) Deployment Readiness는 결정적 Code이며 모델을 호출하지 않는다. Post-Deploy
Verification은 Assessment Profile을 재사용하므로 MVP에서 Deployment 역할 Model Profile을 배정하지
않는다. 벤치마크의 Deployment 후보는 근거 기록으로만 남기고, 결정적 판정 단계를 임의로 LLM화하지
않는다.

(ADR-0021) `dev → main` 통합 PR은 Golden Dataset 반복 평가 리포트를 첨부하며 목표
미달이면 릴리스를 진행하지 않는다. 미달 시 선택지는 rubric/prompt/Golden Case 재고정 또는 ADR-0003
절차에 따른 Anchor 전환이며, 목표치를 낮추는 판단은 개인이 하지 않는다. 세 관점(`IAC`,
`AWS_ACTUAL`, `DRIFT`) 중 하나라도 Golden Case가 없어 측정하지 못한 리포트는 Gate 통과 근거로 쓰지
않는다.

## Development ownership and repository boundaries

| Role | Primary responsibility | Main areas |
| --- | --- | --- |
| **A — Platform/Backend** | 플랫폼 기반, 사용자, API와 durable workflow state | Cognito, API Gateway, 기능별 Lambda, Remediation policy 호출, Job/Outbox/Queue/revision, Exception/Approval/Audit, 공통 Storage, Frontend Skeleton |
| **B — Policy/Governance Boundary** | AI 평가와 remediation 허용 범위의 정책 Boundary 제공 | 지원 문서 형식, Policy Source lifecycle, Rule Registry/Profile/Context, Remediation eligibility/exception/manual-review 판정 |
| **C — AI Evaluation & Agent Orchestration** | Assessment/Remediation Agent와 평가 품질·evidence orchestration | Assessment Graph/Worker, Remediation Context/Worker, AI Evaluator, Evidence/Severity/Score, Deployment Readiness, Assessment UI |
| **D — Integration & Deployment Execution** | 결정적 외부 실행 adapter와 Deployment Worker | GitHub/AWS Tool, injected Patch/Sync port, Branch/Commit/PR, Terraform Plan/Apply, Deployment Worker, Post-Deploy read |
| **Shared** | 여러 영역의 호환성과 릴리스 품질 유지 | Contracts, Integration Test, C4/ADR, E2E |

- 역할 경계를 넘는 API·Schema 변경은 해당 Contract의 Producer와 Consumer Owner가 검토한다.
- 구현체가 없지만 Contract가 확정된 의존성은 Fixture/Mock으로 병렬 개발한다.
- 다른 역할의 기능은 작업 Branch에 직접 의존하지 않고, `dev`에 Merge된 Contract/구현을 기준으로 통합한다.
- `fixtures/rules/`는 개발 seed다. 고객 정책 기능을 구현하는 Agent는 정적 Fixture를 운영 입력으로
  연결하지 말고 `docs/POLICY_INGESTION.md`의 A/B/C 책임과 승인 Gate를 먼저 확인한다.

## Data model

Operational and domain metadata uses DynamoDB while large immutable artifacts use S3. The table strategy, access patterns, PK/SK, GSI, TTL, tenant isolation, and S3 reference contract are defined in `docs/DATABASE.md`.

## Decision records

장기 영향을 주는 선택은 `docs/decisions/`에서 관리한다. 현재 ADR은 Repository/Delivery, AI Evaluation Boundary, Scoring Reliability, Policy Knowledge, Serverless Workflow, Persistence/Artifact Storage, Approved Deployment Boundary, Customer Deployment Topology, Customer Policy Ingestion을 다룬다.

ADR-0019(승인 배포 실행 경계)는 `Proposed`로 남아 있다. ADR-0020(Post-Deploy Verification과
before/after 비교)과 ADR-0021(Demo·Release readiness gate)은 `Accepted`다. 따라서 D의 live
plan/apply와 A의 Deployment 생성·후속 전이는 ADR-0019 합의 전 구현하지 않지만, C의 비교 projection은
ADR-0020을 따른다.
