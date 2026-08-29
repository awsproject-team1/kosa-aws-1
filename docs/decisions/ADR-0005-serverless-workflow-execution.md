# ADR-0005: Serverless workflow execution

## Context

Assessment와 Remediation은 장시간 실행, 외부 Tool 호출, 승인 대기를 포함할 수 있다. 초기부터 Container/EC2를 도입하면 운영 부담이 커지고, Lambda만으로 모든 작업을 동기 처리하면 제한 시간과 재시도 문제가 생긴다.

## Decision

기능별 Lambda를 기본 실행 단위로 하고, LangGraph Parent/Subgraph와 `job_id` 기반 상태 관리로 workflow를 추적한다. Policy Q&A는 가능한 Sync Timeout 안에서 처리하되, 제한을 넘으면 같은 Job을 비동기로 이어서 처리한다. Assessment와 Remediation은 처음부터 `202 Accepted + job_id` 방식으로 시작한다.

## Consequences

Lambda 제약을 실제로 초과하는 구간에서만 Container 또는 Step Functions 확장을 검토한다. 재시도, Backoff, DLQ, Checkpoint/재개 정책은 구현 시 명시하고 운영 지표로 관측한다.
