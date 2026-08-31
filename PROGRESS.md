# Progress

## Current

- Repository V3 문서 구조와 개발 골격 초기화
- M1 D 진행: AWS Resource Tool + GitHub Integration Tool(둘 다 read-only) 경계와 결정적 Mock 어댑터 완료,
  두 Tool을 소비해 IaC Snapshot + AWS Actual을 함께 읽는 Assessment 입력 조합 계층(collector) 완료
- M1 Initial Assessment MVP 준비: M0 배선·Fixture 검증 완료, 실제 Snapshot/Bedrock 평가 통합 대기
- M1 C 착수: 승인 Model Profile과 제한된 Snapshot Evidence를 사용하는 구조화 Bedrock 평가 어댑터 구현
- M0 deployment readiness: 2단계 protected GitHub Environment 승인, expected-account fail-closed
  검증, Python 3.12/LF-normalized 결정적 패키징, 재실행 가능한 exact SHA-256/S3 Version ID
  Lambda artifact binding 및 customer-approved sandbox CloudTrail delivery/log-file-validation
  절차 문서화 (실제 AWS 배포 승인 대기)
- M1 B 진행: MVP Rule Registry(S3 6건) + Control 매핑 + read-only DynamoDB Policy Catalog 구현,
  C의 Registry 채택(prompt/golden version 재고정)과 A의 테이블 배포 연결 대기
- 고객 사내 정책 수집 진행: B 소유 경계(형식 allow-list, 정규화 Schema, 5개 형식 Parser,
  승인 판정, Profile publication 거부 규칙) 구현 완료. Rule Registry와 `policies-local/`은
  여전히 개발 seed이며, 업로드 세션·저장·상태 write와 승인 API 배선(A), AI 후보 추출(C),
  고객 간 격리·E2E 통합 테스트(Shared)가 `docs/POLICY_INGESTION.md`(ADR-0015) 기준으로 대기

## Completed

- M1 B Policy Source 승인·Profile publication 경계: `RuleLifecycle`, `RuleCandidate`,
  finalization tuple을 인용하는 immutable `PolicySourceApproval` Contract와, `READY` 문서에만
  승인이 붙고 locator/hash가 정규화 결과와 일치할 때만 통과하는 `approve_source()`,
  `docs/POLICY_INGESTION.md`의 게시 거부 조건 3건(승인되지 않은 Source/Rule, 승인과 다른
  Source version, 승인 record의 artifact binding 불일치)을 구현한 `publish_profile()` 추가.
  거부 사유는 `ApprovalRejectionCode` 열거값이며, 승인되지 않은 Rule이 `PolicyContext`에
  도달하지 못함을 테스트로 고정 (승인 API·조건부 write 배선은 A)
- M1 B 고객 Policy Ingestion 정규화 경계: 지원 형식 allow-list(Markdown/Plain text/CSV/XLSX/DOCX)와
  선언 media type·파일 signature·Parser 지원을 함께 대조하는 fail-closed 형식 판정,
  `NormalizedPolicyDocument` Contract, 표준 라이브러리 전용 5개 형식 Parser(XLSX inline string·
  병합 셀·시트 이름 locator 포함), zip 압축 폭탄 한도, 정규화 unit에서 `SourceReference`를
  만드는 Evidence 연결 구현. Contract가 원문·추출 텍스트를 담을 수 없게 만들어 Queue/DynamoDB
  노출 금지를 구조로 강제하고, 실패는 예외가 아니라 `FAILED` 상태와 failure code로 반환한다
  (승인·Profile publication과 업로드 세션은 미구현)
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
- M1 D read-only AWS Resource Tool Port와 결정적 Mock 어댑터 구현: `AwsResourceQuery`
  Contract 소비, READ_RESOURCE/LIST_RESOURCES만 허용, (customer_id, aws_account_id) scope
  강제, 쓰기 표현 불가를 테스트로 고정; S3 AssumeRole code adapter 추가 (고객 Role 설정은 미연결)
- M1 C S3 Initial Assessment 코드 경계: 승인 Region Bedrock structured evaluator, read-only S3
  Actual Evidence, immutable plan-based Coverage, paginated 결과 조회 API와 기본 React 화면 구현
  (고객 Account Role·Bedrock 환경 설정은 D/A deployment 단계에서 주입)
- PR #10 review follow-up: Lambda artifact의 `agent` 포함과 Assessment report HTTP route를
  추가하고, cross-account S3 AssumeRole에 ExternalId·만료 전 credential cache, frontend
  API authentication/configuration·pinned build CI, Evidence reference 정규형을 반영
- M0 Assessment API가 transactionally persisted Outbox를 SQS로 즉시 전송하고, 실패 시
  EventBridge Outbox sweeper가 at-least-once 재시도하도록 보완
- PR #7 infrastructure review 반영: HTTP API `$default` auto-deploy stage, Cognito User Pool
  retention, CI-verified Lambda ZIP과 승인된 GitHub Actions OIDC deployment path 추가
- PR #7 latest deployment review 반영: named IAM CloudFormation capability, pinned AWS OIDC
  credentials action, template-change `cfn-lint` CI를 추가
- M0 storage hardening: account-qualified S3 이름 제약, DynamoDB deletion protection,
  BucketOwnerEnforced/TLS deny, 리소스 태그, storage ARN Outputs와 pinned `cfn-lint` 검증 추가
- PR #5 audit/tenant review 반영: ArtifactBucket CloudTrail S3 data event trail과 별도 retained
  audit bucket, M0 Worker의 미사용 `customers/*` S3 권한 제거, 재현 가능한 YAML 보안 CI 추가
- M0 실행 bootstrap: CloudFormation metadata/artifact/worker queue/IAM skeleton, JWT-derived
  customer Job API, Policy Context allow-list, Assessment/Remediation Contract guard 구현 및 검증
- PR #3 review 반영: Assessment selector 영속화 및 Job 연결, dispatch 실패 보상 전이,
  인증/공개 오류 Contract 단일화, `Evaluator`의 `PolicyRule` 타입 명시
- Assessment·Job·Workflow Outbox의 DynamoDB transactional write와 pending Outbox 재전송 경계 추가
- M1 B MVP Rule Registry: `fixtures/rules/`에 ISMS-P/사내 체크리스트 근거의 S3 Rule 6건과
  Control 매핑 5건을 커밋하고, Profile allow-list·Control/Resource Mapping·Policy Context를
  다중 Rule로 확장. 평가 대상은 S3 단독이며 EC2 Rule은 Registry에만 두어 multi-type 동작을
  테스트로 고정 (Profile 미포함, M2 확장 대상). `SourceReference` digest는
  `scripts/policy_source_digest.py`로 로컬 원문과 대조 검증한다 (원문 미커밋, ADR-0004)
- M0 A deployment wiring: Cognito JWT HTTP API, API Lambda, EventBridge Outbox sweeper,
  Assessment SQS event-source Worker와 CI-provided Lambda ZIP parameters를 CloudFormation에 추가
- M1 D read-only AWS Resource Tool Port와 결정적 Mock 어댑터 구현: `AwsResourceQuery`
  Contract 소비, READ_RESOURCE/LIST_RESOURCES만 허용, (customer_id, aws_account_id) scope
  강제, 쓰기 표현 불가를 테스트로 고정 (Fixture/Mock 단계, 실제 SDK/AssumeRole은 미연결)
- M1 D read-only GitHub Integration Tool Port와 결정적 Mock 어댑터 구현: `IaCSnapshot`
  Contract 소비, IaC snapshot 조회 read만 허용, (customer_id, repository_id) scope 강제,
  write/PR 표현 불가를 테스트로 고정 (Fixture/Mock 단계, 실제 GitHub App/OIDC는 미연결)
- M1 D Assessment 입력 조합 계층(`agent/context/`) 구현: read-only GitHub/AWS Tool을 함께
  소비해 승인된 Repository IaC Snapshot(IAC)과 AWS Actual(AWS_ACTUAL)을 하나의 불변
  Assessment 입력 번들로 묶어 C 평가 경계에 전달, 단일 customer_id로 두 Tool scope를
  구조적으로 강제, write/mutation 표면 없음 (평가/Drift 판정은 out of scope)

## Next

- M1 D: IaC Snapshot + AWS Actual 조합 계층(Fixture/Mock) 완료 →
  실제 AWS SDK/AssumeRole 및 GitHub App/OIDC 통합으로 collector 뒤 어댑터 교체
- M1 C: collector가 만든 Assessment 입력 번들을 소비하는 평가 흐름 연결 (IAC/AWS_ACTUAL 관점)
- M1 A/C: 실제 Snapshot/Bedrock 평가와 Assessment 결과·Coverage 조회 통합
- M1 A/C: 대규모 Assessment 페이지 조회 비용을 줄이기 위해 immutable 결과 저장과 같은
  DynamoDB transaction에서 Assessment plan의 completed counter를 갱신하는 storage migration
- M1 C: Assessment 경로를 M0 단일 Rule Fixture에서 `load_rule_registry()`로 전환하고
  Rule 확대에 맞춰 prompt/rubric/golden dataset version을 재고정 (DESIGN 품질 Gate 재실행)
- M1 A: DynamoDB Policy Catalog 항목 적재 경로와 실제 테이블 연결 (현재는 stub client 검증까지)
- M1 A: 고객 Policy Source 업로드 세션(presigned·1회용), customer-scoped S3/DynamoDB,
  ingestion record 상태 전이와 조회 API. Client는 `PolicySourceUploadRequest`가 담는 값만
  선언할 수 있고 `customer_id`/bucket/key/상태는 Backend가 발급한다
- M1 A: 승인·Profile 게시 API 배선. `approve_source()`/`publish_profile()`을 DynamoDB 조건부
  write 앞에서 호출하고, 거부 시 write를 시도하지 않는다. 승인 record와 audit record는
  finalization tuple에 조건부로 묶는다
- M1 A/B/C Shared: 업로드 → 정규화 → 승인 → Profile → Assessment 통합 테스트와
  고객 간 Artifact 격리 테스트 (`docs/POLICY_INGESTION.md`의 남은 인수 조건)

## Blocked

- **M1 Exit criteria의 `Finding`과 `Readiness Score`에 담당 역할이 없다.** (제기: B, 2026-08-31)

  두 산출물은 Exit criteria와 `docs/PRD.md`의 제품 흐름에 있고 저장 모델까지 정의돼 있지만,
  생산하는 코드가 없고 M1의 A/B/C/D 역할 항목 어디에도 들어 있지 않다.

  | 산출물 | 정의된 것 | 없는 것 |
  | --- | --- | --- |
  | `Finding` | `docs/DATABASE.md` item(`ASSESSMENT#{id}#FINDING#{finding_id}`), Job step `GENERATE_FINDINGS`, M2 소비처(`RemediationPatch.finding_id`, `POST /findings/{findingId}/remediations`) | 생성 코드, `packages/contracts`의 Schema, 조회 API, 담당 역할 |
  | `Readiness Score` | `docs/DATABASE.md`의 Assessment item 예시(`readiness_score`), `docs/PRD.md`가 서비스의 대표 점수로 규정 | 산출 코드, 계산 정의(Score/Severity/Coverage와의 관계), 담당 역할 |

  M2의 `RemediationPatch`가 `finding_id`를 필수로 요구하므로, M1이 Finding을 생산하지 않으면
  M2 Remediation 전체가 입력을 얻지 못한다.

  - Decision: 두 산출물의 담당 역할과 M1 포함 여부
  - Owner: 미정
  - Needed by: M1 Exit criteria 판정 전
  - Blocks: M1 종료 판정, M2 Remediation 착수
  - Proposed options: (1) Finding 생성·조회는 A, Readiness Score 산출은 C가 맡는다
    (2) 둘 다 평가 산출물로 보고 C가 맡는다 (3) M1 Exit criteria에서 빼고 M2로 옮긴다
  - Final record: 미정

## Milestones

각 마일스톤의 상세 Task는 담당자가 로컬 `.ai/task/taskN.md`에 작성한다. 이 문서에는 팀이 공유해야 하는 완료 기준·의존성·차단 사항만 기록한다.

### M0 — Foundation and contracts

**Exit criteria:** 공통 Contract, 데이터 접근 패턴, 검증 진입점이 합의되고 각 역할이 Mock/Fixture로 병렬 개발을 시작할 수 있다.

- [x] **A — Platform/Backend:** Cognito/API Gateway/Lambda 기본 구조, Job API 경계, DynamoDB/S3 인프라 초안 *(CloudFormation/IAM/Queue, Cognito JWT endpoint, request Lambda, Outbox sweeper, Assessment Worker Lambda wiring, Python Job/Auth/Repository/HTTP boundary 및 Fixture 검증 완료)*
- [x] **B — Policy/Governance Boundary:** Policy Source, Rule, Policy Profile, Source Reference의 초기 Contract *(Fixture 기반 read-only Policy Catalog, Rule ID+version pinning, Profile allow-list Policy Context bootstrap 완료; DynamoDB catalog는 M1 통합 대상으로 이관)*
- [x] **C — AI Evaluation:** `EvaluationResult` 출력 Schema, Score/Rubric Version, Golden Dataset 최소 Case *(S3 단일 대상, `us-east-1` Nova Lite 승인 Profile, API → Outbox → revision-checked Assessment worker → immutable DynamoDB result Fixture integration 및 반복 Golden quality gate 구현 완료; Snapshot/Bedrock invoke/Lambda wiring은 M1 통합 대상으로 이관)*
- [x] **D — Remediation/GitHub/Deployment:** GitHub/AWS Resource Tool Interface, IaC Snapshot·Patch·Plan Contract *(IaC snapshot-bound Remediation guard bootstrap 완료)*
- [x] **Shared:** `docs/API.md`, `docs/CONTRACTS.md`, `docs/DATABASE.md` Review 및 PR Gate/기본 CI 구성 *(Python Checks, PR source validation, Secret Scan bootstrap 완료)*

**Dependencies:** B/C/D Contract는 A의 Job/Storage 경계와 합의한다. 구현체가 없는 Interface는 Fixture/Mock으로 진행한다.

### M1 — Initial Assessment MVP

**Exit criteria:** EC2/RDS/ALB/S3 중 첫 대상 범위에서 Repository + 승인된 Policy Profile을 입력해 Initial Assessment, Finding, Evidence, Readiness Score, Coverage를 조회할 수 있다. 정적 seed가 아닌 사용자 업로드 정책을 제품 기능으로 표시하려면 `docs/POLICY_INGESTION.md`의 별도 Delivery gate를 충족해야 한다.

- [ ] **A — Platform/Backend:** Assessment Job 생성·상태 조회, JWT/RBAC/Scope 검증, Metadata/Artifact 저장
- [ ] **B — Policy/Governance Boundary:** MVP Rule Registry, Profile 적용, Control/Resource Mapping, Policy Context 제공 *(Registry·Control 매핑·Context 확장과 read-only DynamoDB Catalog, Policy Ingestion의 형식 allow-list·정규화 Schema·Parser·승인 판정·Profile publication 거부 규칙 구현 완료; C의 Registry 채택, A의 실제 테이블 적재와 API 배선은 대기)*
- [ ] **C — AI Evaluation:** Assessment Graph, Applicable Rule/Evidence 판단, 구조화 결과 검증, Assessment UI 기본 화면
- [ ] **D — Remediation/GitHub/Deployment:** 승인 Repository IaC Snapshot과 AWS Resource Read-Only 연결 *(read-only Tool 경계 + 두 Tool을 함께 소비하는 Assessment 입력 조합 계층 Fixture/Mock 완료; 실제 SDK/GitHub App 통합 대기)*
- [ ] **Shared:** Contract/Integration Test, Golden Dataset 반복 평가, Score/Coverage 표시 검증

**Dependencies:** C는 B의 승인된 Policy Context와 D의 Snapshot/Read-Only Interface를 사용한다. A가 Job/상태 Contract를 제공한다. 고객 정책 업로드 기능은 A의 upload/storage, B의 normalization/approval, C의 AI extraction quality gate와 Shared 보안·통합 테스트가 모두 필요하다.

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
