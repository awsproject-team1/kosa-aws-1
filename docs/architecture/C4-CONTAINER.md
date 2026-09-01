# C4 Container

- React SPA: S3 + CloudFront 정적 호스팅
- Cognito: 사용자 인증 및 JWT 발급
- API Gateway + Lambda: JWT/RBAC, Domain API, Job orchestration
- LangGraph + Bedrock: 자연어 Orchestrator와 Policy Q&A를 함께 맡는 Parent, 명시적
  요청도 직접 진입할 수 있는 Assessment, Remediation, Deployment Subgraph. 각 역할은
  Golden Dataset으로 승인된 Model Profile을 사용한다.
- SQS + Worker Lambda: Assessment, Remediation, Deployment Queue별 resumable Task 실행;
  DynamoDB checkpoint에서 재개하고 3분 전 checkpoint 후 재큐잉
- EventBridge: GitHub Actions OIDC의 Plan/Apply 완료 Event를 Deployment Queue에 전달
- Policy Ingestion: 고객 정책 원본을 customer-scoped S3에 보관하고, Worker Lambda의
  형식 판정·Parser(`apps/backend/policy/ingestion/`, 표준 라이브러리 전용)가 정규화 Artifact를
  만든다. 추출 텍스트는 정규화 Artifact에만 존재하고 Queue/DynamoDB에는 hash만 흐른다.
  `DynamoDbPolicySourceUploadRepository`가 server-issued upload session, exact S3 version
  finalization, normalized Artifact와 customer-scoped ingestion state write를 담당한다. public
  API Gateway/async dispatcher는 아직 이 경계에 연결되지 않았다 (`docs/POLICY_INGESTION.md`).
- DynamoDB/S3: 상태·메타데이터 및 Artifact 저장
- CloudWatch/CloudTrail: 운영·감사 관측
- GitHub App/Actions: Customer IaC PR, OIDC plan/apply
