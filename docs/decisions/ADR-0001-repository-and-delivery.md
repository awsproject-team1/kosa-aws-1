# ADR-0001: Repository and delivery workflow

## Decision

Governance Platform은 Monorepo로 관리하고 Customer IaC는 별도 Repository로 둔다. `main + dev + short-lived branch` 전략을 사용하며 모든 일반 PR은 `dev`를 대상으로 한다. GitHub Issue/Project는 사용하지 않고 공용 진행은 `PROGRESS.md`, 로컬 Agent 작업은 `.ai/task/taskN.md`에서 관리한다.

## Consequences

기능이 모두 완료되고 최종 E2E/Release 검증이 가능한 시점에만 사람이 `dev → main` PR을 한 번 생성한다.
