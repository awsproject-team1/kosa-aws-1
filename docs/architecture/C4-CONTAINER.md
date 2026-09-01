# C4 Container

- React SPA: S3 + CloudFront 정적 호스팅
- Cognito: 사용자 인증 및 JWT 발급
- API Gateway + Lambda: JWT/RBAC, Domain API, Job orchestration
- LangGraph + Bedrock: 자연어 Orchestrator와 Policy Q&A를 함께 맡는 Parent, 명시적
  요청도 직접 진입할 수 있는 Assessment, Remediation, Deployment Subgraph. 각 역할은
  Golden Dataset으로 승인된 Model Profile을 사용한다.
- SQS + Worker Lambda: Assessment, Remediation, Deployment Queue별 resumable Task 실행;
  DynamoDB checkpoint에서 재개하고 3분 전 checkpoint 후 재큐잉. C는 Assessment/Remediation
  Agent와 Worker orchestration을 소유하고, Remediation Worker는 stored `RemediationDecision`으로
  injected D Patch/Sync port만 호출한다. D는 live GitHub/Terraform adapter와 Deployment Worker를 소유한다.
- EventBridge: GitHub Actions OIDC의 Plan/Apply 완료 Event를 Deployment Queue에 전달. Event는
  신호이며 상태 정본이 아니다. D Deployment Worker가 `run_id`로 Actions run을 다시 읽어 workflow,
  repository, `ref`, conclusion, plan artifact digest를 대조한 뒤에만 상태를 전이한다
  (ADR-0019, `Proposed`)
- Terraform state (M3 계획): 고객 bootstrap stack이 만드는 별도 S3 state bucket과 DynamoDB lock
  table. state key는 `(repository_id, workspace)`로 분리하고, plan 시점 state serial을 Deployment
  record에 기록해 apply 전 재검증한다 (ADR-0019, `Proposed`)
- 고객 repository workflow (M3 계획): plan/apply workflow는 `ci/terraform/` template을 고객 관리자가
  1회 수동 설치한다. GitHub App은 `contents`/`pull_requests` write만 갖고 `workflows: write`는 갖지
  않는다 (ADR-0019, `Proposed`)
- Policy Ingestion: 고객 정책 원본을 customer-scoped S3에 보관하고, Worker Lambda의
  형식 판정·Parser(`apps/backend/policy/ingestion/`, 표준 라이브러리 전용)가 정규화 Artifact를
  만든다. 추출 텍스트는 정규화 Artifact에만 존재하고 Queue/DynamoDB에는 hash만 흐른다.
  `DynamoDbPolicySourceUploadRepository`가 server-issued upload session, exact S3 version
  finalization, normalized Artifact와 customer-scoped ingestion state write를 담당한다. public
  API Gateway/async dispatcher는 아직 이 경계에 연결되지 않았다 (`docs/POLICY_INGESTION.md`).
- DynamoDB/S3: 상태·메타데이터 및 Artifact 저장
- CloudWatch/CloudTrail: 운영·감사 관측
- GitHub App/Actions: Customer IaC PR, OIDC plan/apply
