# Progress

## Current

- Repository V3 문서 구조와 개발 골격 초기화

## Completed

- `main`, `dev` 브랜치 생성
- PRD, DESIGN, 협업 운영 기준을 저장소 문서 정본으로 이전
- 3차 멘토링의 평가 품질 목표, 점수 정책, 의존성·Contract Review 운영 규칙 반영
- M0 B/C/D 실행 Contract와 결정적 Fixture 확정: Policy Source/Rule/Profile, Golden Dataset,
  IaC Snapshot/Patch/Plan, Read-Only AWS Query, Approval의 commit/plan binding
- Initial Assessment의 IaC Compliance, AWS Actual Compliance, Drift를 분리하고, IaC
  Remediation → refresh된 Plan 검증 → 승인 Apply → Actual 재검증 흐름을 Contract/ADR로 확정
- 명시 요청의 직접 Assessment/Remediation/Deployment Subgraph 진입, 자연어 Parent
  Orchestrator(Policy Q&A 포함) routing, 역할별 Golden Dataset 승인 Model Profile 사용 경계를
  ADR-0012로 확정
- Assessment·Remediation·Deployment별 SQS Worker Queue, DynamoDB/S3 checkpoint 재개,
  3분 전 재큐잉, 작업별 재시도/DLQ와 Apply 수동 reconciliation, EventBridge GitHub 완료 Event를
  ADR-0013으로 확정
- M0 A 병렬 개발 전 공통 기준 확정: CloudFormation parameter naming, DynamoDB/GSI/30일
  terminal Job TTL, Job API ownership·revision·tenant scope 규칙 (ADR-0010)
- M0 실행 bootstrap: CloudFormation metadata/artifact/worker queue/IAM skeleton, JWT-derived
  customer Job API, Policy Context allow-list, Assessment/Remediation Contract guard 구현 및 검증

## Next

- M0 A CloudFormation/IAM, injected DynamoDB/S3 adapter, Job API Handler 구현
- M0 Contract를 사용하는 Policy Context, Assessment, Remediation 구현을 Fixture/Mock으로 시작

## Blocked

- 없음

## Milestones

각 마일스톤의 상세 Task는 담당자가 로컬 `.ai/task/taskN.md`에 작성한다. 이 문서에는 팀이 공유해야 하는 완료 기준·의존성·차단 사항만 기록한다.

### M0 — Foundation and contracts

**Exit criteria:** 공통 Contract, 데이터 접근 패턴, 검증 진입점이 합의되고 각 역할이 Mock/Fixture로 병렬 개발을 시작할 수 있다.

- [ ] **A — Platform/Backend:** Cognito/API Gateway/Lambda 기본 구조, Job API 경계, DynamoDB/S3 인프라 초안 *(CloudFormation/IAM/Queue skeleton, Python Job/Auth/Repository/HTTP boundary 및 테스트 완료; deployment wiring 대기)*
- [ ] **B — Policy/Governance Boundary:** Policy Source, Rule, Policy Profile, Source Reference의 초기 Contract *(Profile allow-list Policy Context bootstrap 완료)*
- [ ] **C — AI Evaluation:** `EvaluationResult` 출력 Schema, Score/Rubric Version, Golden Dataset 최소 Case *(V3 Output Contract와 Policy Context-bound Assessment runner bootstrap 완료)*
- [ ] **D — Remediation/GitHub/Deployment:** GitHub/AWS Resource Tool Interface, IaC Snapshot·Patch·Plan Contract *(IaC snapshot-bound Remediation guard bootstrap 완료)*
- [ ] **Shared:** `docs/API.md`, `docs/CONTRACTS.md`, `docs/DATABASE.md` Review 및 PR Gate/기본 CI 구성 *(Python Checks, PR source validation, Secret Scan bootstrap 완료)*

**Dependencies:** B/C/D Contract는 A의 Job/Storage 경계와 합의한다. 구현체가 없는 Interface는 Fixture/Mock으로 진행한다.

### M1 — Initial Assessment MVP

**Exit criteria:** EC2/RDS/ALB/S3 중 첫 대상 범위에서 Repository + Policy Profile을 입력해 Initial Assessment, Finding, Evidence, Readiness Score, Coverage를 조회할 수 있다.

- [ ] **A — Platform/Backend:** Assessment Job 생성·상태 조회, JWT/RBAC/Scope 검증, Metadata/Artifact 저장
- [ ] **B — Policy/Governance Boundary:** MVP Rule Registry, Profile 적용, Control/Resource Mapping, Policy Context 제공
- [ ] **C — AI Evaluation:** Assessment Graph, Applicable Rule/Evidence 판단, 구조화 결과 검증, Assessment UI 기본 화면
- [ ] **D — Remediation/GitHub/Deployment:** 승인 Repository IaC Snapshot과 AWS Resource Read-Only 연결
- [ ] **Shared:** Contract/Integration Test, Golden Dataset 반복 평가, Score/Coverage 표시 검증

**Dependencies:** C는 B의 승인된 Policy Context와 D의 Snapshot/Read-Only Interface를 사용한다. A가 Job/상태 Contract를 제공한다.

### M2 — Remediation and deployment readiness

**Exit criteria:** 선택된 Finding에서 최소 Terraform Patch, Branch/Commit/PR, CI 및 Deployment Readiness Validation/plan까지 이어진다.

- [ ] **A — Platform/Backend:** Remediation/Deployment API, Job 재개, Approval 상태 전이와 Audit Log
- [ ] **B — Policy/Governance Boundary:** Remediation 허용 범위·예외·Manual Review 정책 제공
- [ ] **C — AI Evaluation:** Finding 근거 기반 Remediation Context와 Deployment Readiness 평가
- [ ] **D — Remediation/GitHub/Deployment:** Patch/Diff, GitHub PR, OIDC Terraform Plan, `commit_sha`/`plan_hash` 생성
- [ ] **Shared:** Approval Contract/보안 Review, Patch/Plan Integration Test

**Dependencies:** D의 Plan 결과와 C의 Readiness 결과는 A의 Approval/Deployment 상태에 바인딩한다.

### M3 — Approved apply and post-deploy verification

**Exit criteria:** Human Approval 뒤 승인된 plan만 apply하고, 변경된 AWS Actual을 Post-Deploy Verification으로 재평가해 Finding 및 Readiness Score 변화를 확인한다.

- [ ] **A — Platform/Backend:** Approval 권한 검증, 상태 전이, Audit/Observability, 결과 조회 API
- [ ] **B — Policy/Governance Boundary:** 재평가 적용 범위와 예외 처리 검증
- [ ] **C — AI Evaluation:** Before/After 비교, Finding Resolution, Score/Coverage 변화 평가
- [ ] **D — Remediation/GitHub/Deployment:** GitHub Actions OIDC Apply, 승인 `commit_sha`/`plan_hash` 재검증, AWS Actual 재조회
- [ ] **Shared:** 승인 없는 Write 방지, End-to-End Security/Integration Test

**Dependencies:** Apply는 D의 OIDC 경로만 사용하며, A의 승인 상태와 C의 평가 결과를 우회할 수 없다.

### M4 — Demo and release readiness

**Exit criteria:** WordPress/LAMP Demo에서 폐루프 E2E가 재현되고, 품질·운영·문서 기준을 충족해 사람이 `dev → main` 통합 PR을 만들 수 있다.

- [ ] **A — Platform/Backend:** 배포/운영 점검, 오류·성능·비용 관측 검증
- [ ] **B — Policy/Governance Boundary:** Demo Policy/Rule/근거와 Coverage 설명 검증
- [ ] **C — AI Evaluation:** Golden Dataset 품질 목표(정확도 90%, Score 편차 ±10점) 확인
- [ ] **D — Remediation/GitHub/Deployment:** Demo IaC, Plan/Apply/검증 runbook 확인
- [ ] **Shared:** C4/ADR/API/Contract Freshness, E2E, Secret Scan, Release/Demo Review

**Dependencies:** 모든 M0–M3 Exit criteria 충족 후에만 `dev → main` PR과 최종 Release 검증을 진행한다.
