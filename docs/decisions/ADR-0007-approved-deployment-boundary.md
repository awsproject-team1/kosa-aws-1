# ADR-0007: Approved deployment boundary

## Context

Governance Platform은 고객 IaC를 평가하고 수정안을 만들지만, AI 또는 Platform이 고객 인프라에 직접 Write 권한을 가지면 안전하지 않다. Plan과 Apply 사이의 변경도 방지해야 한다.

## Decision

GitHub App은 승인된 Customer IaC Repository에만 최소 권한으로 접근한다. Remediation은 Branch/Commit/PR까지만 생성하며, Plan은 GitHub Actions OIDC와 TerraformPlanRole로 실행한다. Apply는 Human Approval 뒤 GitHub Actions OIDC와 TerraformDeploymentRole로만 실행한다. Approval은 승인된 `commit_sha`와 `plan_hash`에 바인딩한다.

## Consequences

AgentRuntimeRole과 AWS Resource Tool은 Customer Workload에 Read-Only 권한만 가진다. Apply 전에는 Approval 대상의 commit과 plan hash를 재검증하고, 모든 AssumeRole/Plan/Apply 행위는 감사 로그로 남긴다.
