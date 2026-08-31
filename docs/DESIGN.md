# DESIGN — Cloud Governance & Compliance Agent

## Architecture

```text
React SPA (S3 + CloudFront)
→ Cognito (JWT)
→ API Gateway

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
  `ASSESSMENT`, `REMEDIATION`, `DEPLOYMENT` subgraph를 둔다. 명시적 UI/API 요청은
  대응 Subgraph로 직접 진입하고, 자연어 요청만 Parent Orchestrator Agent를 거쳐
  의도·후보 scope에 맞는 Subgraph로 라우팅한다.
- Storage: DynamoDB에는 상태와 메타데이터, S3에는 대형 Artifact를 저장한다.
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
- `GitHubWorkflowEventRole`: GitHub Actions OIDC에서 Plan/Apply 완료 Event만 EventBridge에
  게시하는 최소 권한
- Apply 전 승인한 `commit_sha`와 `plan_hash`를 재검증한다.
- M0 Worker는 packaged synthetic fixture만 처리하며 ArtifactBucket 접근 권한을 갖지 않는다.
  고객 artifact 접근은 tenant-scoped runtime identity가 구현·검토된 뒤에만 허용한다
  (ADR-0014).
- AI 출력은 Schema/Evidence/Permission/ID 검증과 CI, plan, Human Approval을 통과해야 한다.

## Observability

CloudWatch Metrics/Logs, CloudTrail, X-Ray 또는 OpenTelemetry를 사용한다. 구조화 로그의 상관 키는 `request_id`, `job_id`, `assessment_id`, `rule_id`다. Assessment 성공률, 판정 분포, Lambda 오류/throttle, Bedrock 지연·토큰, Agent Workflow와 Tool 호출, Job 적체, Queue age/DLQ depth, checkpoint·재개 횟수, plan/apply 실패를 관측하고 민감한 Prompt·정책 원문·IaC 전체는 로그에서 마스킹하거나 제외한다. ArtifactBucket의 S3 object read/write는 별도 retained CloudTrail data-event trail과 audit bucket에 기록하며, selector는 artifact bucket만 포함하고 audit destination은 제외한다. Audit event에는 object-key metadata가 남을 수 있으므로 object key에 민감 원문을 넣지 않는다. 실제 고객 배포 전에는 승인된 sandbox에서 controlled artifact Get/Put과 delivered CloudTrail record/log-file validation을 확인한다. 평가마다 Evidence, Tool 호출, Model/Prompt/Rubric/Rule Version, Score, Coverage, Token/Latency/Retry/Validation 결과를 보존한다.

## Evaluation quality gate

Model, Prompt, Rubric, Rule, Policy Document, Context Retrieval 또는 Tool이 바뀌면 Golden Dataset과 반복 평가를 실행한다. 목표는 PASS/FAIL 정확도, Evidence Reference 정확도, 동일 Case 판정 일치율 각각 90% 이상과 Score 반복 편차 ±10점 이내다.

## Development ownership and repository boundaries

| Role | Primary responsibility | Main areas |
| --- | --- | --- |
| **A — Platform/Backend** | 플랫폼 기반과 사용자·Job·상태 관리 | Cognito, API Gateway, 기능별 Lambda, Job/State, 공통 Storage, Frontend Skeleton |
| **B — Policy/Governance Boundary** | AI가 평가할 수 있는 정책·통제 Boundary 제공 | Policy Source, Rule Registry/Validation, Policy Profile, Control/Resource Mapping, Source Reference, Policy Context |
| **C — AI Evaluation** | Resource × Rule 평가와 AI 품질 관리 | Assessment Graph, AI Evaluator, Applicable Rule Selection, Evidence 판단, Severity, Score, Source Score/Risk, Assessment UI |
| **D — Remediation/GitHub/Deployment** | Finding을 승인된 IaC 변경과 배포 검증으로 연결 | GitHub Integration Tool, AWS Resource Tool 연결, Terraform Remediation, PR/Plan/Approval/Apply/Post-Deploy |
| **Shared** | 여러 영역의 호환성과 릴리스 품질 유지 | Contracts, Integration Test, C4/ADR, E2E |

- 역할 경계를 넘는 API·Schema 변경은 해당 Contract의 Producer와 Consumer Owner가 검토한다.
- 구현체가 없지만 Contract가 확정된 의존성은 Fixture/Mock으로 병렬 개발한다.
- 다른 역할의 기능은 작업 Branch에 직접 의존하지 않고, `dev`에 Merge된 Contract/구현을 기준으로 통합한다.

## Data model

Operational and domain metadata uses DynamoDB while large immutable artifacts use S3. The table strategy, access patterns, PK/SK, GSI, TTL, tenant isolation, and S3 reference contract are defined in `docs/DATABASE.md`.

## Decision records

장기 영향을 주는 선택은 `docs/decisions/`에서 관리한다. 현재 ADR은 Repository/Delivery, AI Evaluation Boundary, Scoring Reliability, Policy Knowledge, Serverless Workflow, Persistence/Artifact Storage, Approved Deployment Boundary, Customer Deployment Topology를 다룬다.
