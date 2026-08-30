# ADR-0005: Serverless workflow execution

## Context

Assessment와 Remediation은 장시간 실행, 외부 Tool 호출, 승인 대기를 포함할 수 있다. 초기부터 Container/EC2를 도입하면 운영 부담이 커지고, Lambda만으로 모든 작업을 동기 처리하면 제한 시간과 재시도 문제가 생긴다.

## Decision

기능별 Lambda를 기본 실행 단위로 하고, LangGraph Parent/Subgraph와 `job_id` 기반 상태 관리로 workflow를 추적한다. Parent의 Policy Q&A와 자연어 routing은 최대 30초의 동기 응답 예산만 사용한다. 제한을 넘길 수 있는 질문은 같은 Job으로 비동기 전환하지 않고, 범위를 좁혀 다시 요청하도록 안내한다. Assessment와 Remediation은 처음부터 `202 Accepted + job_id` 방식으로 시작한다.

## Consequences

Lambda 제약을 실제로 초과하는 구간에서만 Container 또는 Step Functions 확장을 검토한다. Assessment, Remediation, Deployment의 재시도, DLQ, Checkpoint/재개 정책은 ADR-0013의 SQS Worker 경계를 따른다.
