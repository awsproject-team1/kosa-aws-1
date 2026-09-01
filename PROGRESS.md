# Progress

## Current

- M1 Initial Assessment MVP의 코드 경계 완료: 하나의 Assessment가 `IAC`, `AWS_ACTUAL`,
  `DRIFT` 세 관점을 모두 산출하고 Finding·Coverage·Readiness Score까지 조회된다.
  실제 고객 sandbox 배포와 Bedrock 품질 Gate 실행만 대기한다.
- M0 deployment readiness: 2단계 protected GitHub Environment 승인, expected-account fail-closed
  검증, Python 3.12/LF-normalized 결정적 패키징, 재실행 가능한 exact SHA-256/S3 Version ID
  Lambda artifact binding 및 customer-approved sandbox CloudTrail delivery/log-file-validation
  절차 문서화 (실제 AWS 배포 승인 대기)
- 고객 사내 정책 수집 진행: B 소유 경계(형식 allow-list, 정규화 Schema, 5개 형식 Parser,
  승인 판정, Profile publication 거부 규칙) 구현 완료. Rule Registry와 `policies-local/`은
  여전히 개발 seed이며, 업로드 세션·저장·상태 write와 승인 API 배선(A), AI 후보 추출(C),
  고객 간 격리·E2E 통합 테스트(Shared)가 `docs/POLICY_INGESTION.md`(ADR-0015) 기준으로 대기
- M2 착수: B의 Remediation 조치 판정 경계(허용 범위·예외·Manual Review)와 D의 결정적 Patch
  생성 경계(PR #21)가 각각 올라와 있고, 둘을 잇는 호출부(A의 Remediation API)가 다음 조각이다

## Completed

- M2 B Remediation 허용 범위·예외·Manual Review 정책 경계 (ADR-0017): Finding 하나를
  `TERRAFORM_PATCH`/`ACTUAL_SYNC`/`MANUAL_REVIEW`/`SUPPRESSED` 중 하나로 판정하는 순수 함수와
  Contract 추가. 허용 범위는 Rule version 단위로 `fixtures/rules/remediation.json`에 커밋하고,
  등록되지 않은 Rule은 자동 조치가 열리지 않고 `MANUAL_REVIEW`로 떨어진다. 고객 예외는
  `(customer_id, rule_id, rule_version)`에 묶이고 반드시 만료되며 Rule 새 version으로 승계되지
  않는다. `AWS_ACTUAL`/`DRIFT` Finding은 같은 `Resource × Rule`의 IaC 판정이 `PASS`일 때만
  `ACTUAL_SYNC`가 되고, `OUT_OF_SCOPE`/`EXECUTION_ERROR`를 안전으로 읽지 않는다
  (예외 등록·저장 API는 A, Patch 생성 연결은 D)
- M1 C Initial Assessment 3-관점 산출 완료 (ADR-0016): Worker가 `perspective_runners`로 IaC
  본문과 AWS Actual을 각각 평가한 뒤 `DRIFT`를 Code로 결정적으로 파생한다. Drift는 두 판정의
  불일치이며 AI 판정이 아니고, score 정합 100 / 이탈 0에 evidence는 두 관점의 합집합이다.
  Coverage 분모는 `Resource × Rule × Perspective`이고, 다중 관점 Assessment는
  `AssessmentResourceWork.planned_evaluations`로 서버가 계획을 고정해 첫 task가 분모를
  결정하지 못하게 한다. Readiness Score는 정합 여부가 준수 수준이 아니므로 `DRIFT`를 제외한다
- M1 D IAC 관점용 read-only Terraform 본문 read 추가: `IaCSnapshot`은 Artifact reference라
  IaC 준수 판정에 부족하므로 `IaCDocument`와 `IaCDocumentReader` port를 추가하고 GitHub REST
  adapter(`git/blobs`, GET only, 1MB 상한)와 Mock에 구현했다. 본문 read는
  `SnapshotReadRequest.include_iac_document`로 명시할 때만 수행하고, 지원하지 않는 tool에는
  관점을 조용히 빼지 않고 실패한다. write/PR 표면은 여전히 없다
- M1 A 승인 Registry 게시 진입점 추가: `scripts/publish_policy_catalog.py`가 조건부 write
  기반 `DynamoDbPolicyCatalogBootstrap`을 호출하고, `--dry-run`은 AWS 자격 증명 없이 Registry를
  검증한다. 같은 key에 다른 내용이 있으면 이미 평가에 쓰인 정책을 바꾸지 않고 fail-closed한다
- PR #16 review 반영: 로그인 직후 `assessment_id`가 없을 때 결과 조회를 시도해 오류 화면이
  Assessment 시작 폼을 가리던 문제 수정, 일회성 OIDC claim 진단 step 제거, live M1 Worker의
  Job/Assessment 중복 read 제거
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
  `M1_ASSESSMENT_READ_ROLE_ARNS`를 등록한다. 이 설정 전에는 고객 sandbox 배포나 실제
  GitHub/AWS/Bedrock E2E를 시작하지 않는다. IAC 관점이 `git/blobs`를 읽으므로 GitHub App
  installation token에는 승인 repository의 Contents read 권한이 필요하다.
- M2 A: Remediation API가 `RemediationPolicy.decide()`를 Patch 생성 **앞에서** 호출하고,
  `MANUAL_REVIEW`/`SUPPRESSED` 결정은 Job을 만들지 않고 사유와 함께 보고한다. 고객 예외의
  등록·승인·저장(만료 포함)과 감사 record도 A 경계다
- M2 D: `FixturePatchGenerator` 호출부를 `RemediationDecision`이 `TERRAFORM_PATCH`인
  Finding으로 제한하고, `ACTUAL_SYNC` 결정은 Patch 없이 현재 commit을 배포 대상으로 넘긴다
- M2 A/C/D: `RemediationTarget`의 `terraform_managed`와 IaC 판정을 누가 채우는지 확정한다.
  IaC 관점 결과는 C가, Terraform 관리 여부는 D의 Snapshot이 안다
- M1 A/C: 대규모 Assessment 페이지 조회 비용을 줄이기 위해 immutable 결과 저장과 같은
  DynamoDB transaction에서 Assessment plan의 completed counter를 갱신하는 storage migration.
  같은 작업에서 `findings`도 페이지네이션한다 (현재는 페이지마다 전체 Finding을 반환한다)
- M1 C: Rule 6건 × 3관점으로 확대된 평가 범위에 맞춰 prompt/rubric/golden dataset version을
  재고정하고 DESIGN 품질 Gate를 재실행 (IAC/DRIFT 관점 Golden Case 추가 필요)
- M1 A: 고객 Policy Source 업로드 세션(presigned·1회용), customer-scoped S3/DynamoDB,
  ingestion record 상태 전이와 조회 API. Client는 `PolicySourceUploadRequest`가 담는 값만
  선언할 수 있고 `customer_id`/bucket/key/상태는 Backend가 발급한다
- M1 A: 승인·Profile 게시 API 배선. `approve_source()`/`publish_profile()`을 DynamoDB 조건부
  write 앞에서 호출하고, 거부 시 write를 시도하지 않는다. 승인 record와 audit record는
  finalization tuple에 조건부로 묶는다
- M1 A/B/C Shared: 업로드 → 정규화 → 승인 → Profile → Assessment 통합 테스트와
  고객 간 Artifact 격리 테스트 (`docs/POLICY_INGESTION.md`의 남은 인수 조건)
- M1 A: 승인된 고객 sandbox에 Auth bootstrap을 배포하고, controlled local user의 Hosted UI
  로그인·Assessment 시작·결과 조회 E2E를 실행한다.
- M1 A/D: 고객 sandbox의 Metadata Table에 `scripts/publish_policy_catalog.py`로 승인 Registry를
  publish하고, GitHub App installation token/승인 repository 및 AWS read Role을 runtime
  configuration에 주입해 actual adapter E2E를 실행한다. 이 단계는 고객 자격 증명·보호된
  배포 승인 없이는 실행하지 않는다.
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
- [x] **B — Policy/Governance Boundary:** MVP Rule Registry, Profile 적용, Control/Resource Mapping, Policy Context 제공 *(Registry·Control 매핑·Context 확장과 read-only DynamoDB Catalog, Policy Ingestion의 형식 allow-list·정규화 Schema·Parser·승인 판정·Profile publication 거부 규칙 구현 완료; Worker가 Registry를 채택한 multi-rule fixture integration 완료. 실제 고객 정책 업로드·테이블 적재는 별도 Delivery gate)*
- [x] **C — AI Evaluation:** Assessment Graph, Applicable Rule/Evidence 판단, 구조화 결과 검증,
  `IAC`/`AWS_ACTUAL`/`DRIFT` 3관점 산출, Finding·Readiness Score projection, Assessment UI 기본
  화면 *(6개 S3 Rule × 3관점 = 18개 평가의 fixture integration으로 결과·Finding·Coverage·Readiness
  까지 검증 완료; 고객 Bedrock 품질 Gate와 IAC/DRIFT Golden Case는 sandbox 실행 대기)*
- [x] **D — Remediation/GitHub/Deployment:** 승인 Repository IaC Snapshot과 AWS Resource Read-Only 연결 *(read-only Tool 경계 + Assessment 입력 조합 계층, S3 AssumeRole, GitHub REST commit/tree/blob read adapter 구현 완료. IAC 관점용 `IaCDocument` 본문 read 포함, write 표면 없음; 고객 GitHub App/runtime injection E2E 대기)*
- [x] **Shared:** Contract/Integration Test, Golden Dataset 반복 평가, Score/Coverage 표시 검증 *(3관점 Initial Assessment integration test, Drift 파생 unit test, Coverage/Readiness/Finding 표시 검증 완료; Golden Dataset 반복 평가는 기존 M0 runner 유지, 확대 Rule 재고정은 Next)*

**Dependencies:** C는 B의 승인된 Policy Context와 D의 Snapshot/Read-Only Interface를 사용한다. A가 Job/상태 Contract를 제공한다. 고객 정책 업로드 기능은 A의 upload/storage, B의 normalization/approval, C의 AI extraction quality gate와 Shared 보안·통합 테스트가 모두 필요하다.

### M2 — Remediation and deployment readiness

**Exit criteria:** 선택된 Finding에서 최소 Terraform Patch, Branch/Commit/PR, CI 및 Deployment Readiness Validation/plan까지 이어진다.

- [ ] **A — Platform/Backend:** Remediation/Deployment API, Job 재개, Approval 상태 전이와 Audit Log
- [x] **B — Policy/Governance Boundary:** Remediation 허용 범위·예외·Manual Review 정책 제공 *(Rule version 단위 허용 범위 Registry, 만료되는 고객 예외, 조치 유형·Manual Review 사유 판정 구현 완료. 예외 등록·저장 API는 A, Patch 생성 연결은 D)*
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
