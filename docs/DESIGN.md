# DESIGN — Cloud Governance & Compliance Agent

## Architecture

```text
React SPA (S3 + CloudFront)
→ Cognito (JWT)
→ API Gateway
→ Function-specific Lambda
→ LangGraph Parent + Subgraphs
→ Bedrock AI Evaluator + constrained tools

DynamoDB: job, workflow, domain metadata
S3: policy originals, IaC/AWS snapshots, reports, patches
GitHub App: customer Terraform repository
GitHub Actions: OIDC plan / approved apply
```

플랫폼은 고객 AWS Account의 `us-east-1`에 배포하며, MVP에서 Backend Lambda와 Agent Runtime은 고객 기존 VPC에 연결하지 않는다.

## Components

- Frontend: React SPA. UI는 권한별 노출만 제어하고 Authorization은 Backend가 강제한다.
- Backend: Auth/User, Job/Assessment, Policy/Rule, GitHub/Remediation, Approval/Deployment Lambda로 분리한다.
- Workflow: LangGraph Parent 아래 `POLICY_QA`, `ASSESSMENT`, `REMEDIATION_DEPLOYMENT` subgraph를 둔다.
- Storage: DynamoDB에는 상태와 메타데이터, S3에는 대형 Artifact를 저장한다.
- Tools: Policy Context, External Evidence, GitHub Integration, AWS Resource(Read Only)로 제한한다.

## Responsibility boundary

Code는 Customer/AWS Account/Repository/Policy Profile 범위, Tool 권한, 출력 스키마, 점수 범위와 (도입 시) Score Anchor, Evidence reference, 상태 저장, Coverage를 검증한다. AI Evaluator는 적용 Rule, 필요한 Evidence, 판정, Severity, 0–100 점수, Rationale 및 Source Score/Risk를 선택한다. 반복 실행 편차가 목표인 ±10점을 지속적으로 넘을 때에만 고정 Anchor 집합에서 점수를 선택하도록 전환한다.

## State and execution

Parent State에는 전체 Artifact가 아닌 `job_id`, `user_id`, `repository_id`, `policy_profile_id`, `assessment_id`, `finding_id`, `remediation_id`, `deployment_id`, `current_step`, `approval_status`만 둔다. 장시간 Assessment/Remediation은 `202 + job_id`로 추적한다.

## Security

- `AgentRuntimeRole`: Customer Workload 읽기 전용 및 필요한 App Data
- `TerraformPlanRole`: plan에 필요한 읽기 중심 권한
- `TerraformDeploymentRole`: 제한된 Infrastructure write
- Apply 전 승인한 `commit_sha`와 `plan_hash`를 재검증한다.
- AI 출력은 Schema/Evidence/Permission/ID 검증과 CI, plan, Human Approval을 통과해야 한다.

## Observability

CloudWatch Metrics/Logs, CloudTrail, X-Ray 또는 OpenTelemetry를 사용한다. 구조화 로그의 상관 키는 `request_id`, `job_id`, `assessment_id`, `rule_id`다. Assessment 성공률, 판정 분포, Lambda 오류/throttle, Bedrock 지연·토큰, Agent Workflow와 Tool 호출, Job 적체, plan/apply 실패를 관측하고 민감한 Prompt·정책 원문·IaC 전체는 로그에서 마스킹하거나 제외한다. 평가마다 Evidence, Tool 호출, Model/Prompt/Rubric/Rule Version, Score, Coverage, Token/Latency/Retry/Validation 결과를 보존한다.

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
