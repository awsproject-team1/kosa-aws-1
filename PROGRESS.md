# Progress

## Current

- Customer sandbox OIDC diagnosis: the deployment workflow logs selected non-secret OIDC
  claim identifiers before role assumption; bootstrap uses the confirmed immutable owner/repository
  IDs, and its deployment role permits fixed-Version ID artifact revalidation. The foundation now
  omits the optional M1 input policy when live assessment configuration is absent; failed deployments
  emit scoped diagnostics without exposing protected parameter values.
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
- 고객 사내 정책 수집 진행: B의 형식 allow-list·정규화 Schema·5개 형식 Parser 구현 완료.
  Rule Registry와 `policies-local/`은 여전히 개발 seed이며, 업로드 세션·저장·상태 write(A),
  Rule 후보 검토·승인과 Profile publication(B 후속), AI 추출(C)은 `docs/POLICY_INGESTION.md`
  (ADR-0015) 기준으로 대기

## Completed

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
- M1 C Finding·Readiness projection: follow-up Evaluation Result를 immutable Finding으로
  idempotent 저장하고, 완료된 Assessment plan에서 severity-weighted Readiness Score를 계산해
  Assessment API·React report로 조회
- M1 A auth bootstrap: Cognito local-user `Admin`/`User` group과 Hosted UI PKCE를 구성하고,
  React 로그인·Assessment 시작 화면 및 고객 sandbox E2E handoff 절차를 추가
- M1 통합 개발: Worker가 M0 단일 Rule fixture 대신 승인된 6개 S3 MVP Rule Registry를
  사용하도록 전환하고, API → Outbox → structured evaluator → immutable Result/Finding →
  Coverage/Readiness report의 multi-rule fixture integration test를 고정
- M1 A/D integration boundary: 승인된 Registry를 고객별 DynamoDB Policy Catalog에
  immutable/idempotent하게 publish하는 Bootstrap과, 지정 commit의 Terraform blob manifest만
  GitHub REST `GET`으로 읽는 scoped Snapshot adapter를 구현·unit test로 고정
- M1 customer-sandbox wiring: protected Environment Secret의 exact customer/repository/profile
  target만 Worker가 해석하도록 하고, GitHub revision preflight → Secrets Manager short-lived
  installation token/External ID → STS S3 read-only → Bedrock AWS_ACTUAL 평가를 조건부 M1
  runtime으로 연결. M0 fixture 모드는 live configuration이 없을 때만 유지한다.
- M1 customer-operated deployment bootstrap: 고객 관리자가 1회 실행하는 CloudFormation으로
  exact GitHub OIDC Environment subject만 신뢰하는 deployment role, private versioned Lambda-code
  bucket, foundation 전용 CloudFormation execution role을 생성하고, 기존 workflow가 bootstrap
  output만 사용해 M1 foundation을 배포하도록 연결 (실제 customer sandbox 실행 대기)
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
- Bedrock 실측 모델 평가 기반 구축 및 최종 강화 평가 완료: 활성 Text→Text 45개를
  6개 Case에서 5회 반복한 최종 1,350회 결과(세션 전체 3,115회)에서
  Parent=`Gemma 3 4B IT`(routing+Q&A 20/20), Assessment=`Nova Micro`,
  Remediation=`Devstral 2 123B`, Deployment=`Nova Lite`를 역할별 추천 후보로 도출했으며,
  승인된 runtime Model Profile 배정은 변경하지 않음

## Next

- **M1 실제 검증 선행:** 고객 관리자가 `m1-customer-bootstrap.yaml`을 자신의 sandbox
  계정에 한 번 실행해 exact GitHub Environment OIDC deployment role, versioned Lambda-code
  bucket, foundation-only CloudFormation execution role을 만든다. 이어 현재 저장소에 서로 다른
  protected Environment (`customer-sandbox-artifact`, `customer-sandbox-deploy`)과 Required
  reviewers/같은 `EXPECTED_AWS_ACCOUNT_ID`를 설정하고, deploy Environment에
  `M1_ASSESSMENT_RUNTIME_JSON`, `M1_ASSESSMENT_SECRET_ARNS`,
  `M1_ASSESSMENT_READ_ROLE_ARNS`를 등록한다. 이 설정·PR #16 승인/병합 전에는 고객 sandbox
  배포나 실제 GitHub/AWS/Bedrock E2E를 시작하지 않는다.
- M1 D: IaC Snapshot + AWS Actual 조합 계층(Fixture/Mock) 완료 →
  실제 AWS SDK/AssumeRole 및 GitHub App/OIDC 통합으로 collector 뒤 어댑터 교체
- M1 C: collector가 만든 Assessment 입력 번들을 소비하는 평가 흐름 연결 (IAC/AWS_ACTUAL 관점)
- M1 A/C: 실제 Snapshot/Bedrock 평가와 Assessment 결과·Coverage 조회 통합
- M1 A/C: 대규모 Assessment 페이지 조회 비용을 줄이기 위해 immutable 결과 저장과 같은
  DynamoDB transaction에서 Assessment plan의 completed counter를 갱신하는 storage migration
- M1 C: Assessment 경로를 M0 단일 Rule Fixture에서 `load_rule_registry()`로 전환하고
  Rule 확대에 맞춰 prompt/rubric/golden dataset version을 재고정 (DESIGN 품질 Gate 재실행)
- M1 A: DynamoDB Policy Catalog 항목 적재 경로와 실제 테이블 연결 (현재는 stub client 검증까지)
- M1 B: 정규화 결과에서 Rule 후보를 검토·승인하고, 승인된 Source/Rule version만 참조하는
  Profile publication 거부 규칙 구현 (승인과 게시는 서로 다른 operation)
- M1 A: 고객 Policy Source 업로드 세션(presigned·1회용), customer-scoped S3/DynamoDB,
  ingestion record 상태 전이와 조회 API. Client는 `PolicySourceUploadRequest`가 담는 값만
  선언할 수 있고 `customer_id`/bucket/key/상태는 Backend가 발급한다
- M1 A/B/C Shared: 업로드 → 정규화 → 승인 → Profile → Assessment 통합 테스트와
  고객 간 Artifact 격리 테스트 (`docs/POLICY_INGESTION.md`의 남은 인수 조건)
- M1 A: 승인된 고객 sandbox에 Auth bootstrap을 배포하고, controlled local user의 Hosted UI
  로그인·Assessment 시작·결과 조회 E2E를 실행한다.
- M1 Shared: Rule 확대에 맞춰 prompt/rubric/golden dataset version을 재고정하고
  DESIGN 품질 Gate를 재실행한다. (현재 fixture integration은 Registry의 6개 S3 Rule을 사용)
- M1 A/D: 고객 sandbox의 Metadata Table에 승인 Registry를 publish하고, GitHub App installation
  token/승인 repository 및 AWS read Role을 runtime configuration에 주입해 actual adapter E2E를
  실행한다. 이 단계는 고객 자격 증명·보호된 배포 승인 없이는 실행하지 않는다.
- M1 A/B/C Shared: 고객 Policy Source 업로드·정규화 Contract와 지원 형식 allow-list 확정 후,
   tenant-scoped S3/API, Parser Adapter, Rule 검토·승인, Profile 게시 경로 구현.
  지원 형식은 Markdown/Plain text/CSV/XLSX/DOCX이며 서드파티 런타임 의존성 없이 처리한다.

## Blocked

- 없음

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

- [ ] **A — Platform/Backend:** Assessment Job 생성·상태 조회, JWT/RBAC/Scope 검증, Metadata/Artifact 저장,
  고객 AWS Account용 Auth bootstrap *(고객 소유 IdP federation 또는 Cognito local user 결정, 초기
  Admin 인수, Admin/User claim·group, 로그인 UI 및 고객 측 사용자 관리 절차 E2E 검증; Registry의
  customer-scoped DynamoDB immutable Bootstrap 구현 완료, sandbox publish/E2E 대기)*
- [x] **B — Policy/Governance Boundary:** MVP Rule Registry, Profile 적용, Control/Resource Mapping, Policy Context 제공 *(Registry·Control 매핑·Context 확장과 read-only DynamoDB Catalog 구현 완료; Worker가 Registry를 채택한 multi-rule fixture integration 완료. 실제 고객 정책 업로드·테이블 적재는 별도 Delivery gate)*
- [x] **C — AI Evaluation:** Assessment Graph, Applicable Rule/Evidence 판단, 구조화 결과 검증,
  Finding·Readiness Score projection, Assessment UI 기본 화면 *(6개 S3 Rule의 fixture integration으로
   결과·Finding·Coverage·Readiness까지 검증 완료; 고객 Bedrock 품질 Gate는 sandbox 실행 대기)*
 - [ ] **D — Remediation/GitHub/Deployment:** 승인 Repository IaC Snapshot과 AWS Resource Read-Only 연결 *(read-only Tool 경계 + 두 Tool을 함께 소비하는 Assessment 입력 조합 계층 Fixture/Mock 완료; S3 AssumeRole 및 GitHub REST commit/tree read adapter 구현, 고객 GitHub App/runtime injection E2E 대기)*
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
