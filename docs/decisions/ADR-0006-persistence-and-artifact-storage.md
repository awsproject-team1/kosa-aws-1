# ADR-0006: Persistence and artifact storage boundary

## Context

Job, Assessment, Finding, Remediation, Deployment, Approval은 상태 전이와 조회가 필요하고, 정책 원문·Terraform/AWS Snapshot·Report·Patch는 큰 Artifact다. 서로 다른 저장 특성을 하나의 저장소에 혼합하면 비용과 접근 제어가 복잡해진다.

## Decision

DynamoDB는 Job/Workflow/Domain Metadata와 상태 전이를 저장하고, S3는 Policy Original, Terraform Snapshot, AWS Snapshot, Report Artifact, Remediation Patch/Diff, Golden Dataset Artifact를 저장한다. DynamoDB Item은 S3 Artifact의 식별자, version, content hash, 접근 Scope만 참조한다.

## Consequences

테이블 수, PK/SK, GSI, TTL, 보존 기간, 테넌트 격리 키와 접근 패턴은 구현 전 `docs/DATABASE.md`에서 확정한다. S3 Artifact는 직접 공개하지 않고 Scope 검증을 거쳐 접근한다.
