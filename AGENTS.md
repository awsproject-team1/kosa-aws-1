# Agent Guide

이 문서는 Coding Agent의 빠른 진입점이다. 제품·설계의 세부 내용이나 협업 규칙을 여기 중복하지 말고 아래 정본 문서를 사용한다.

## Stable invariants

- 저장소 문서가 정본이며, Notion은 회의·논의 기록이다.
- GitHub Issue/Project는 사용하지 않는다. 팀 공용 진행·의존성·차단 사항은 `PROGRESS.md`에서 관리한다.
- `.ai/`는 프로젝트를 받은 개발자/Agent가 **최초 1회 로컬에서 생성**하는 Git 제외 상태 디렉터리다. 개인 Agent 상태는 `.ai/task/taskN.md`에 기능별로 누적하며 기존 Task는 덮어쓰지 않는다.
- 작업 브랜치는 최신 `dev`에서 만들며 일반 PR의 base는 `dev`다. `main` 직접 push와 개발 중 `main` 대상 PR은 금지한다.
- AI와 Tool은 Customer, AWS Account, Repository, Policy Profile Scope 밖으로 접근할 수 없다.
- 정책 원문은 저장소에 커밋하지 않는다. 원문은 로컬 `policies-local/`에 두고, 저장소에는 Rule 정의와 `SourceReference` locator만 둔다. 이 저장소는 공개다.
- AWS Resource Tool은 읽기 전용이다. 실제 인프라 변경은 승인된 `commit_sha`와 `plan_hash`를 검증한 뒤 Human Approval 및 GitHub Actions를 통해서만 수행한다.
- AI 평가는 기본적으로 0–100 연속 점수를 사용한다. Golden Dataset 반복 평가에서 편차가 ±10점을 지속 초과할 때만 Anchor 정책을 적용한다.

## Session start

새 세션에서는 필요한 범위만 순서대로 읽는다.

1. 이 파일
2. 현재 작업의 `.ai/task/taskN.md` (없으면 생성)
3. `.ai/PROGRESS.md`와 루트 `PROGRESS.md`
4. 아래 문서 맵에서 작업에 필요한 정본
5. 관련 코드, Contract, Fixture, Test

같은 세션에서 이미 읽은 안정 문서는 실제 변경·충돌 확인이 필요한 경우에만 다시 읽는다. `git status`, diff, 테스트 결과처럼 변하는 정보는 필요할 때 재확인한다.

## Document map

| Need | Source of truth |
| --- | --- |
| 제품 가치, 사용자, MVP 범위, 평가 의미 | `docs/PRD.md` |
| Architecture, AWS, Security, Workflow, Observability | `docs/DESIGN.md` |
| HTTP API와 오류 형식 | `docs/API.md` |
| Domain/structured-output Schema | `docs/CONTRACTS.md` 및 `packages/contracts/` |
| DynamoDB/S3 Artifact model and access patterns | `docs/DATABASE.md` |
| System Context/Container | `docs/architecture/` |
| 장기 기술 결정과 이유 | `docs/decisions/` |
| Branch, PR, Review, Done, 문서 Freshness | `CONTRIBUTING.md` |
| 팀 Current/Completed/Next/Blocked/Milestone | 루트 `PROGRESS.md` |
| 개인 Agent 세부 진행, 검증, 다음 세션 메모 | `.ai/PROGRESS.md` |
| 공용 Task/Handoff/ADR 형식 | `templates/` |
| 정책 원문(인증기준, 사내 점검 문서) | 로컬 `policies-local/` (Git 제외, B 역할이 보관). 저장소에는 없다 |

실행 가능한 Contract와 문서가 충돌하면 Contract를 우선하고 문서를 같은 변경에서 동기화한다.

## Work flow

1. Task에 Goal, Scope, Acceptance Criteria, Out of Scope를 기록한다.
2. 최신 `dev`에서 목적이 드러나는 short-lived branch를 만든다.
3. 영향받는 Contract의 Producer/Consumer Owner를 확인한다.
4. 의존성을 `Blocked`, `Mockable`, `Integrated`로 분류한다. Mockable은 Fixture/Mock으로 병렬 구현할 수 있다.
5. 구현 후 필요한 검증을 실행하고, 실패 로그를 확인해 수정·재검증한다.
6. 결과와 의존성은 `PROGRESS.md`에 짧게 갱신하고, 지속적인 Architecture/Contract 결정은 ADR로 남긴다.

## Local `.ai/` bootstrap and progress

프로젝트를 처음 설정할 때 한 번만 `.ai/task/`, `.ai/PROGRESS.md`, `.ai/HANDOFF.md`, `.ai/PLAN.md`를 생성한다. `.ai/`는 `.gitignore`에 포함되므로 각 개발자/Agent가 자신의 로컬 상태를 관리한다.

- 루트 `PROGRESS.md`: Git으로 공유하는 팀 진행·의존성·차단 사항·마일스톤
- `.ai/PROGRESS.md`: Git으로 공유하지 않는 개인 세부 진행·검증 명령/결과·다음 세션 메모

기능이 완료되면 개인 진행의 핵심 결정·결과 한 줄만 루트 `PROGRESS.md`의 Completed에 옮긴다. 개인 Task 상세, 대화 맥락, 임시 계획은 `.ai/`에만 남긴다.

## Change-to-document rule

| Change | Update in the same PR |
| --- | --- |
| 제품 범위·사용자 가치·MVP | `docs/PRD.md` |
| Architecture, AWS, Security, Storage, Deployment | `docs/DESIGN.md`, 필요 시 C4 및 ADR |
| Endpoint, Request/Response, Error | `docs/API.md`, `packages/contracts/`, Contract Test |
| Domain/AI structured output schema | `docs/CONTRACTS.md`, `packages/contracts/`, Contract Test |
| Branch/PR/CI/Done 운영 | `CONTRIBUTING.md`, 필요 시 PR Template |
| 팀 진행·의존성·차단 상태 | `PROGRESS.md` |

## Validation

- 문서 변경: Markdown 및 관련 링크/형식 확인
- Python 변경: `python3 -m ruff check .`, `python3 -m ruff format --check .`와 관련 Unit/Contract Test
- Frontend 변경: Test와 Build
- Terraform 변경: `fmt`, `validate`, TFLint, Checkov
- 여러 컴포넌트 영향: Integration Test
- Demo/Release 또는 `dev → main`: E2E/Release 검증

Python PR은 `.github/workflows/python-checks.yml`에서 Unit/Contract/Integration/Security Test를 실행한다. 모든 PR은 `.github/workflows/validate-pr-source.yml`의 source gate와 `.github/workflows/secret-scan.yml`을 통과해야 한다. Frontend와 Terraform Workflow는 해당 실행 환경을 추가할 때 구성한다.

## Before handoff or PR

- [ ] Task Acceptance Criteria 충족
- [ ] 변경 범위에 맞는 Test/Lint/Build/Validate 실행
- [ ] API, Contract, Architecture 영향과 관련 문서 확인
- [ ] Secret·Access Key·Token·민감한 Prompt/IaC 원문 미포함
- [ ] `PROGRESS.md`와 필요 시 ADR 갱신
- [ ] PR에는 Scope, Validation, Contract 영향, Producer/Consumer Reviewer를 기록
