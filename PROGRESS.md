# Progress

## Current

- M4 A 관측·비용 기록 조립 경계를 구현했다(ADR-0021 §3, `feature/m4-a-observability`, base=dev).
  데모 폐루프 1회 실행의 일곱 항목(Assessment 성공률, Bedrock 호출, Queue 건전성, Job 재개,
  plan/apply, 감사 이력, 비용)을 immutable하게 묶는 `DemoRunObservability` 계약을 두고, 각 항목은
  "값이 비어 있으면 미충족"을 `meets_gate`로 판정하며 `unmet_items()`가 미충족 항목을 열거한다.
  민감 원문 부재 확인 플래그가 없으면 전체 게이트는 통과가 아니다. 조립은 A의 Admin 전용 경계
  (`DemoRunObservabilityService`, `READ_OBSERVABILITY`)가 주입된 read-only source가 돌려준 사실만
  묶고, source가 사실을 돌려주지 못하면 fail-closed한다(값을 지어내지 않는다). `AuditEventType`을
  게이트의 네 범주(Remediation/Approval/Apply/Verification)로 결정적 매핑하는
  `assemble_audit_trail_metric`을 함께 두었다. D가 live 어댑터를 주입할 진입점을 열도록
  `DemoRunMetricsSource`의 결정적 Mock(`MockDemoRunMetricsSource`, 다른 실행 port Mock과 같은
  scope 강제·register seed 관례)도 함께 제공한다. live CloudWatch/CloudTrail/Cost Explorer adapter
  실제 구현과 HTTP 라우트 배선은 D 배포 통합·M3 A audit-events의 dev 병합 뒤 이어지며, 실제 데모
  실행값 기록은 Shared/D/C 공동에 sandbox 실행(Blocked)에 의존한다. 문서(CONTRACTS/DESIGN) 동기화.
  후속 리뷰 대기
- M3 D customer runtime 배선을 구현했다(`feature/m3-d-deployment-runtime-wiring`). A의 Deployment
  endpoint가 `dev`에 병합돼 차단이 풀린 뒤, Deployment Worker를 구동하는 조각들을 기능별 커밋으로
  추가했다: (1) approval read(`DeploymentApprovalRepository.get_approval`), (2) plan/run/verification
  store 3종(`DEPLOYMENT#` item에 plan facts+`plan_run` conditional update, `#DISPATCH` item,
  `#EVENT#{run_id}` 예약 item을 `VERIFIED`로 확정), (3) record+job+approval+예약 EVENT를 합성하는
  `DynamoDbDeploymentWorkRepository.get_work`, (4) fail-closed `DeploymentRuntimeConfiguration`, (5)
  composition root(`apps/backend/deployment/runtime.py`)의 SQS `parse_tasks`/`run_tasks`/`lambda_handler`.
  **apply 완료 Event 경계를 A/D 공유 계약으로 확정**했다(ADR-0019 §7, DATABASE.md "완료 Event 경계"):
  A/EventBridge가 `#EVENT#{run_id}`를 `PENDING_VERIFICATION`으로 예약 write → D Worker가 그 좌표로
  run을 재조회·대조 후 `VERIFIED`로 확정. D는 이 read/verify 경로를 모두 구현했고, 예약 write는 A 몫이다.
  이어서 `LivePlanRequestPort`(plan run → `PlanExecutionResult` 조립, GitHub I/O는 콜백 seam)와
  `_live_worker`(승인 단일 target으로 D port 4종·store 3종·work repo 조립, I/O seam 주입)를 구현했다.
  fixture 경로(Mock)와 조립 로직은 seam 주입으로 검증된다. 검증: ruff 273 files clean,
  Unit 662 / Contract 135 / Integration 9 / Security 74 OK. **코드로 남은 것은 없다.** 유일하게 실제
  검증이 남은 건 `_live_plan_outputs_fetcher`의 GitHub plan run I/O(dispatch·폴링·artifact 파싱)로,
  이는 실제 sandbox 자격 증명·네트워크가 있어야 동작·검증되며 그전까지 호출 시 명시적으로 막는다.
  A가 공유 계약대로 `#EVENT` 예약 write를 붙이고 sandbox 자격 증명이 준비되면 live E2E가 열린다.
- M4 B/C release-readiness 구현: Demo Policy Coverage manifest/validator가 승인 Profile의 6 Rule,
  5 Control, 12 version-pinned policy locator와 Initial/Post-Deploy 36 Golden 좌표를 교차 검증한다.
  M4 C live gate는 customer-sandbox Post-Deploy 18 Case를 5회 반복한 60 Bedrock IAC/Actual 결과와
  같은 run에서 Code로 파생한 DRIFT 30개를 strict하게 검증하고 aggregate-only report를 만든다.
  Initial FAIL/FAIL Golden DRIFT 기대값도 ADR-0011에 맞춰 PASS/100으로 교정했다. A/D handoff와
  private observation/sanitized report 경계는 ADR-0022(`Accepted`) 및 두 M4 runbook에 기록했다.
  실제 protected sandbox observation·관측/비용·demo run은 외부 승인 대기이며 fixture/dry-run을
  release evidence로 표시하지 않는다.
- M4 D 데모 문서 몫(데모 IaC 참조·폐루프 runbook)을 최신 `dev`에 정합화했다. `docs/M4-DEMO-IAC-REFERENCE.md`·
  `docs/M4-DEMO-RUNBOOK.md`가 병합된 dev의 실제 경계(`ci/terraform/` template, `agent/runtime/live_deployment_ports.py`,
  `apps/backend/deployment/worker.py`, `packages/contracts/terraform_plan.py`)와 정합함을 재확인했다 — 6개 S3 Rule
  위반 토글 1:1 매핑이 `fixtures/rules/remediation.json` eligibility(AUTOMATIC=PUBLIC/ACL/TLS, MANUAL_ONLY=
  POLICY/ENCRYPT/LOGGING)·`rules.s3.json` version `2026-08-31`과 일치. PR #49 리팩터링(D port 파일 이동)은 문서
  참조 파일을 바꾸지 않아 문서 수정 불필요. 검증: ruff 256 files, Unit 538 / Contract 135 / Integration 9 /
  Security 72 OK. A Deployment 생성·상태 endpoint가 `dev`에 병합(PR #50/#52)돼 D의 customer runtime 배선
  차단이 풀렸다. (PR #51 = 이 M4 문서, base `dev`.)
- PR #50을 포함한 M3 API runtime/infrastructure follow-up: API Lambda에
  `DEPLOYMENT_QUEUE_URL`을 주입하고, deployment 생성·조회·검증 조회·거절의 네 HTTP API Gateway
  route를 JWT authorizer와 함께 명시했다. handler branch만 있고 Gateway route가 없는 배포 누락과
  cold-start 환경 변수 누락을 CloudFormation security regression으로 고정했다. Deployment Worker의
  concrete live adapter/customer runtime은 여전히 고객 GitHub/OIDC configuration과 D-owned adapter
  구현에 의존하며, 미구성 상태를 실행 가능하다고 표시하지 않는다.
- 조회 시점 예외 억제 표시를 `GET /assessments/{id}/report`에 배선했다(ADR-0020 §6,
  `feature/m3-a-deployment-endpoints`). `annotate_suppressed_findings()`는 정의만 있고 호출자가
  없었는데, `AssessmentReportApiService`가 report page의 Finding에 고객 예외를 조회 시각 기준으로
  join해 `AssessmentReport.suppressions`(`FindingSuppression`)로 응답한다. 세 갭을 함께 닫았다:
  (1) `_finding_from_item`이 `evaluated_at`/`assessed_commit_sha` provenance를 복원하지 않아 모든
  Finding이 억제에서 제외되던 것, (2) `AssessmentReport`에 `suppressions` 필드/`to_dict`가 없던 것,
  (3) composition root가 예외 reader와 read clock을 report 서비스에 주입하지 않던 것. 예외 reader
  fault는 억제 없이(위반이 보이는 쪽으로) fail-open한다. `GET /deployments/{id}/verification`의
  `AssessmentComparison`은 순수 비교 계약상 예외를 join하지 않는다. 검증: ruff 263 files,
  Unit 619 / Contract 135 / Security 72 / Integration 9 OK.
- M3 A Deployment endpoint를 D 실행 Contract(PR #49, 이제 `dev`에 병합됨) 위에 구현했다(`feature/m3-a-deployment-endpoints`).
  `DeploymentStatus`+`derive_deployment_status()`(저장 안 함, durable 사실 파생), `Action`
  START/REJECT_DEPLOYMENT와 `AuditEventType` DEPLOYMENT_REQUESTED/REJECTED, `DeploymentRecord` store
  (생성=DEPLOYMENT+JOB+OUTBOX(RUN_DEPLOYMENT)+audit 한 transaction, reject=terminal REJECTION+Job
  CANCELLED), 그리고 4개 endpoint(생성·조회·검증조회·Admin reject)와 composition root 배선.
  생성·reject는 durable 배선이 끝났고, approve/get/verification은 facts/comparison reader 조립기
  통합 전까지 fail-closed다. 닫힌 PR #40의 검증 provenance(`plan_verification_assessment`, Assessment
  phase/correlation/scope-pin 영속화, Worker phase 복원)도 이 브랜치에 되살렸다. PR #48 리뷰 3건 반영:
  terminal Job(FAILED/CANCELLED)→`MANUAL_REVIEW`, plan/binary artifact의 customer/repository scope
  강제, plan 투영 fail-closed는 D 정본에서 이미 해결. D 실행 Contract는 PR #49 정본
  (`PlanExecutionResult`/`ApplyDispatchReceipt`/`WorkflowRunFacts`/`WorkflowConclusion`/
  `WorkflowRunReference`)을 그대로 소비하며, 최신 `dev` 병합 시 중복 정의하던 `ApplyRunReference`/
  `VerifiedRunOutcome`/`AwsResourceSnapshot` 초안 심볼은 정본 심볼로 대체했다.
  문서(API/CONTRACTS/DATABASE) 동기화. 후속 리뷰 대기
- `plan_run_id` Contract 갭을 닫았다. apply workflow는 plan run의 saved artifact를 내려받으므로
  그 run 좌표가 필요한데(ADR-0019 §1), 정본 port에 실을 자리가 없어 live apply dispatch가
  GitHub API 422로 거부되던 상태였다. `PlanExecutionResult.plan_run`(`WorkflowRunReference`)을
  추가해 plan 시점 run 좌표를 durable하게 남기고, `DeploymentWork.plan_run` → `dispatch_apply(...,
  plan_run=)` → `plan_run_id` input으로 이어 배선했다. plan과 apply는 사람 승인을 사이에 둔 서로
  다른 실행이라 dispatch 시점에 만들어낼 수 없다. 세 경계(Contract·Worker·live 어댑터)가 각각 run
  좌표의 배포·저장소 scope를 확인해, 다른 배포의 plan artifact를 적용하면서 나머지 승인 값은 전부
  일치하는 상태를 막는다. A 부재로 B가 대행했으므로 A 복귀 시 Contract 확장 재확인 필요

- M3 D 실행 경계를 PR #49로 올렸고 리뷰(P1 5건)를 반영했다(base `dev`,
  `feature/m3-d-execution-ports`). #48(A Contract 동결)을 병합해 정본 Contract를 소비한다 —
  중복 `terraform_plan.py`/D port/반환형을 제거하고 `PlanExecutionResult`/`ApplyDispatchReceipt`/
  `WorkflowRunFacts`/`WorkflowConclusion`/`WorkflowRunReference`를 쓴다. `DeploymentWorker`는
  `APPLY_COMPLETED`에서 apply를 재dispatch하지 않고 저장된 `run_reference`(실제 GitHub run_id)로
  재조회하며, plan 시점 state와 실행 시점 state를 workflow에서 실제 비교하고, apply는 별도 plan
  run의 artifact를 `plan_run_id`로 받는다. 검증: ruff 253 files, Unit 526 / Contract 128 /
  Integration 9 / Security 72. `plan_run_id`를 dispatch input으로 채우는 경로는 정본
  `ApplyDispatchPort` 시그니처에 자리가 없어 A Contract 확장이 필요함을 `ci/terraform/README.md`에
  명시했다. 남은 D 조각(customer runtime 배선)은 A Deployment endpoint의 `dev` 병합 뒤 착수한다.
  2차 리뷰(P1 3건·P2 1건)도 반영했다: (1) `terraform_plan.py` 투영이 `resource_changes`/`change`/
  `actions` 누락을 fail-closed로 거부해 손상된 plan이 destructive 게이트를 우회하지 못하게 하고,
  (2) `PlanExecutionResult`가 binary artifact의 customer_id/repository_id를 plan artifact와 대조하며
  worker도 이를 재확인하고, (3) `LiveActualRereadPort`가 주입된 read-only Resource Tool의
  `list_resources`를 실제 호출(생성자 `resource_types` 추가)해 apply 후 Actual 재조회를 수행하고,
  (4) `derive_deployment_status`가 `job_status`를 반영해 `FAILED`/`CANCELLED` Job을 `MANUAL_REVIEW`로
  표시한다. 검증: ruff 253 files, Unit 532 / Contract 133 OK.
- 예외의 조회 시점 표시 경계를 B가 구현했다(ADR-0020 §6). 예외는 재평가를 막지 않고 Finding도
  그대로 저장되며, `annotate_suppressed_findings()`가 표시용 `FindingSuppression`만 돌려준다.
  억제 술어는 `RemediationPolicy.decide()`와 하나(`select_in_force_exception()`)를 공유하므로
  화면의 억제와 `SUPPRESSED` 판정이 갈리지 않는다. `evaluated_at`이 없는 옛 Finding은 두 시각
  규칙을 적용할 수 없어 억제하지 않는다. 함께 §2 재평가 범위(검증 phase Profile version pin,
  Rule 적용 가능성)를 회귀로 고정했다. 후속 PR 검토 대기
- M1 C→A policy candidate extraction handoff Contract: `PolicyCandidateExtraction`은 exact `READY`
  normalized document, undecided `RuleCandidate`, extractor ID/version을 immutable하게 묶고 source
  version·locator·hash provenance를 fail-closed로 검증한다. 원문/정규화 text는 Contract에 없으며,
  A의 candidate DynamoDB persistence 및 `load_review`/`load_publication` read adapter 구현을 위한
  mockable input이다. 이는 M3 A/D의 ADR-0019 승인 의존성과 무관하다.

- ADR-0020 파생 Contract를 동결했다. Assessment 계획이 개수가 아니라
  `(resource_id, rule_id, perspective)` **집합**으로 저장되므로 C의 비교 경계를 실제로 배선할 수
  있다. `AuditEventType`과 `RemediationSyncTarget` 위치도 같은 변경에서 정리했다. 후속 PR 검토 대기
- ADR-0020(Post-Deploy Verification과 before/after 비교)·ADR-0021(Demo·Release readiness gate)을
  `Accepted`로 확정했다. C는 `FindingResolution`/`AssessmentComparison` Contract와 immutable
  before/after projection을 구현했고 36개(6 Rule × 3 perspective × Initial/Post-Deploy phase) Golden
  fixture gate를 확인했다.
  ADR-0019(승인 배포 실행 경계)를 `Accepted`로 확정했다(2026-09-02). A·D·Security가 서명 PR
  리뷰 approve로 서명했고, 같은 PR에서 상태 전환과 `docs/API.md`·`CONTRACTS.md`·`DATABASE.md`·
  `DESIGN.md`·C4 계획 표기의 구현 표기 이관을 함께 커밋했다. 이 서명은 A(PR #40)와 D 조각이
  병렬로 base 삼도록 독립 PR로 분리했다. 이로써 M2 A audit 정본화, M2/M3 D live plan/apply,
  M3 A Deployment 생성·상태 API의 차단이 풀렸다.
- M1 sandbox readiness 보강 완료: live Worker가 등록된 M1 Model Profile ID를 work에 직접 결합하고,
  deployment workflow가 명시적 live/fixture mode·selector/ARN/account/Region/40자 commit을 customer
  deployment credential 설정 전에 fail-closed 검증함. 실제 고객 배포는 아래 Blocked 해소 전 시작하지 않음
- PR #26 review follow-up은 PR #29로 최신 `dev`에 통합 완료: assessment provenance(commit/time),
  remediation identity, 미래/누락/mismatch provenance 차단을 A→B 흐름과 persistence에 연결
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
- M2 A/B/C mockable flow 완료: A가 B `RemediationPolicy.decide()`를 호출해 decision/context/Job/
  Outbox/audit를 저장하고, C-owned revision-bound Remediation Worker가 injected Patch/Sync port로
  분기한다. D는 GitHub write 제안 경계(`ProposedPullRequest`)까지 완료했고, live GitHub/Terraform
  adapter와 customer runtime 배선, OIDC Terraform Plan(`commit_sha`/`plan_hash`)이 남은 조각이다.
  Plan 조각은 ADR-0019가 `Proposed`인 동안 착수하지 않는다(아래 Blocked)
- **2026-09-02 일정 단축 운영 합의(M4의 `dev` 병합까지):** 남은 범위는 M2 통합 PR → M3 통합 PR →
  M4 통합 PR 순서로 진행하고, 각 브랜치는 앞 마일스톤이 `dev`에 병합된 뒤 최신 `dev`에서 만든다.
  통합 PR 안에서는 기능·Contract·문서·검증 관심사별 Conventional Commit을 보존하고 squash 없이
  merge commit으로 병합한다. 기존 Owner 승인·ADR/Contract checkpoint·필수 CI를 생략하지 않으며,
  M4 구현 PR 뒤의 최종 `dev → main` Release PR은 별도로 유지한다.

## Completed

- M4 B/C repository release gate 준비 완료: `fixtures/m4/demo_policy_coverage.json`과 strict validator/
  CLI로 Demo의 version-pinned Rule·Control·근거·36 Coverage 좌표를 고정하고, Post-Deploy 18 Case ×
  5 run customer observation gate(60 Bedrock + 30 Code-derived DRIFT)를 추가했다. Case/perspective/전체
  90% 기준, score spread 10 이하, 실행 오류 0, exact approved Profile/artifact/run binding을
  fail-closed로 검증하며 공개 report는 민감 원문 없이 aggregate/digest만 가진다. 실제 customer
  evidence는 ADR-0022 handoff에 따라 A/D protected run이 제공할 때만 생성한다. 모든 Bedrock 호출이
  실패한 완전한 입력은 p95 `null`인 품질 FAIL(exit 1)로 보고한다. 전체 검증: Unit 639,
  Contract 136, Security 74, Integration 9, Ruff 275 files.

- M4 D 데모 IaC 참조·시나리오와 폐루프 runbook 문서 완결 (ADR-0021 §1·§3): 데모 Terraform은 별도
  고객 sandbox repository에 두고 이 저장소에는 참조만 남긴다는 결정에 따라, `docs/M4-DEMO-IAC-REFERENCE.md`
  (데모 저장소 식별자·전제조건, 6개 S3 Rule 1:1 위반 토글 매핑, 세 관점 재현)와
  `docs/M4-DEMO-RUNBOOK.md`(Initial→Remediation/PR→plan→승인→apply→Post-Deploy Verification 폐루프,
  ADR-0020 재조회 시점 규칙, ADR-0021 §3 관측·비용 기록 표)를 추가했다. `fixtures/terraform/`은 비어
  있음을 유지하고 README로 이유·참조를 명시했다. runbook은 ci/terraform template·live 어댑터·
  `terraform_plan.py`의 실제 경계와 정합한다. 실제 데모 저장소 생성·sandbox 폐루프 실행·관측/비용
  값 채우기는 protected Environment·OIDC Role·자격 증명 대기(A endpoint·runtime 배선 뒤).

- M3 D live 실행 어댑터·workflow template 완결 (ADR-0019 §5·§6·§7, ADR-0007): 세 주입 port의
  live 어댑터를 `agent/runtime/live_deployment_ports.py`에 추가했다. `LiveApplyDispatchPort`는
  승인 approval로 GitHub Actions `workflow_dispatch`만 호출하고(유일한 write 표면, input은
  deployment_id/commit_sha/plan_hash) run 좌표를 결정적으로 유도한다. `LiveWorkflowRunReader`는
  `run_id`로 run을 GET 재조회하고 404·미완료·`plan_hash` 마커 부재를 예외가 아니라 `not_found`
  결론 값으로 반환해 EventBridge payload를 신뢰하지 않는다(§7). `LiveActualRereadPort`는 M1
  read-only AWS Resource Tool을 재사용해 planned 집합으로 좁힌 리소스만 다시 읽는다(write 표면
  없음). 고객이 1회 설치하는 `ci/terraform/` plan/apply workflow template과, Platform
  `terraform_plan.py`와 같은 canonical 바이트를 내는 `canonical_plan_hash.py`(두 경로 동일 확인)를
  추가했다. apply는 saved plan만 적용하고 protected Environment·OIDC Role 분리(Plan/Deployment)·
  plan_hash와 state `lineage`·`serial` 재검증을 거치며, App에는 `workflows: write`가 없다(§6).
  worker와 어댑터가 같은 apply workflow path allow-list(`APPLY_WORKFLOW_PATHS`) 하나를 공유한다.
  남은 것은 customer runtime 배선(composition root 주입)과 실제 sandbox 실행으로, 이는 A의
  Deployment endpoint(kosa-m3-a)와 보호된 자격 증명에 의존한다.

- M3 D live plan/apply 실행 경로 (ADR-0019 `Accepted` 이후): `plan_hash`의 유일한 산출 근거를
  `packages/contracts/terraform_plan.py`에 두었다. `terraform show -json`을 `resource_changes[]`의
  11개 허용 필드로 투영하고(허용 목록이라 Terraform/Provider가 필드를 늘려도 hash가 안 흔들린다,
  address/key 정렬·compact ASCII·no trailing newline·NaN/Inf 거부) 그 canonical 바이트의 SHA-256을
  낸다. A 승인 재검증·C readiness·D apply 재검증이 같은 함수를 부른다. `has_destructive_changes`는
  `delete` 또는 비어 있지 않은 `replace_paths`로 판정한다(§1). apply용 `TERRAFORM_PLAN_BINARY`
  ArtifactType(hash 대상 아님), D 내부 `PlanRequestPort`와 반환형 `PlanRequestOutcome`
  (plan + state `lineage`·`serial`을 쌍으로 묶는 `TerraformStateVersion` + `PlanReadinessInput`)을
  추가했다. `apps/backend/deployment/worker.py`의 `DeploymentWorker`가 세 command를 소비해 command당
  하나의 injected D port만 부른다 — `RUN_DEPLOYMENT`→plan, `PLAN_COMPLETED`→idempotent apply
  dispatch(§5), `APPLY_COMPLETED`→run 재조회 후 승인 사실(repository/workflow_path/ref/plan_hash/
  conclusion) 대조 뒤에만 Actual 재조회(§7, ADR-0020 §8). work는 queue payload가 아니라
  (job_id, revision)으로 다시 읽고, EventBridge payload만으로 상태를 확정하지 않으며, 승인 사실과
  하나라도 다르면 재시도 없이 차단한다. D는 승인·정책 판정을 하지 않고 상태 전이·`DeploymentStatus`
  파생은 A 소유로 남긴다. live GitHub/Terraform SDK adapter와 customer runtime 배선은 다음 조각이다.

- M3 D 실행 port 계약·Mock 병렬 구현 (ADR-0019 §5·§7 근거, CONTRACTS.md 확정 시그니처): D가
  소유하고 A/C가 주입받는 `ApplyDispatchPort`/`WorkflowRunReader`/`ActualRereadPort` Protocol과
  반환형 `ApplyRunReference`/`VerifiedRunOutcome`/`AwsResourceSnapshot`(`packages/contracts/`),
  결정적 Mock 어댑터를 추가했다. dispatch는 같은 approval로 재호출돼도 새 run을 만들지 않고
  (idempotent), run 재조회 실패는 예외가 아니라 값으로 표현하며, Actual 재조회는 read-only scope
  강제다. live plan/apply 경로와 `plan_hash` 투영은 ADR-0019 `Accepted` 대기로 제외한다.

- M2 D GitHub write 제안 경계 (ADR-0007 read-only 원칙 유지): 승인된 `RemediationPatch` 하나에서
  Branch/Commit/PR 좌표를 결정적으로 *제안*하는 `GitHubWriteTool` port와 `ProposedPullRequest`,
  단일 (customer_id, repository_id) scope 강제(`require_patch_scope`), patch 좌표 기반 결정적
  branch 유도(`derive_head_branch`), 결정적 Mock 어댑터를 추가했다. 실제 write/commit/PR 생성·
  apply 표면은 노출하지 않으며(제안만), Terraform Plan(OIDC)·`commit_sha`/`plan_hash` 산출은
  ADR-0019 서명 이후 다음 조각이다.

- M3 Contract 동결 (ADR-0019 파생 공용 Contract): ADR-0019가 `Accepted`가 되어 열린 A/D-owned
  Contract를 기능별 커밋으로 구현했다. (1) `plan_hash`를 `terraform show -json`의 `resource_changes[]`
  허용 목록(11개 필드) 투영의 SHA-256으로 정의하고(`packages/contracts/terraform_plan.py`),
  A 승인·C readiness·D apply 재검증이 같은 함수를 호출해 값이 어긋날 수 없게 했다.
  `has_destructive_changes`도 같은 투영에서 파생하고 `TERRAFORM_PLAN_BINARY` ArtifactType을 더했다.
  (2) D 실행 port 4종(`PlanRequestPort`/`ApplyDispatchPort`/`WorkflowRunReader`/`ActualRereadPort`)을
  `@runtime_checkable` Protocol로 고정하고 반환형(`PlanExecutionResult`, `ApplyDispatchReceipt`,
  `WorkflowRunFacts`)과 `TerraformStateVersion`(lineage+serial 쌍)을 Contract에 두어 A/C가 fixture로
  병렬 진입할 수 있게 했다. (3) `DeploymentStatus`를 저장하지 않고 durable 사실에서 read 시 계산하는
  순수 함수 `derive_deployment_status()`(`DeploymentFacts` 입력)로 구현했다(ADR-0019 §8, 불변식 #9).
  (4) `Action`에 `START_DEPLOYMENT`(User)·`REJECT_DEPLOYMENT`(Admin), `AuditEventType`에
  `DEPLOYMENT_REQUESTED`·`DEPLOYMENT_REJECTED`를 더했다. `docs/CONTRACTS.md`를 구현에 맞춰 동기화했고
  ruff·Unit·Contract·Security·Integration 검증을 통과했다. A endpoint 배선은 Next다. 후속 PR 검토 대기
- M3 B 조회 시점 억제의 미래 평가 시각 회귀 수정 (PR #47, `dev` 병합): 공용
  `select_in_force_exception()`이 `finding_evaluated_at > at`을 예외 선택 전에 fail-closed로
  거부한다. 조회 뒤에 평가된 것으로 기록된 Finding이 그 사이 승인된 예외로 억제 표시되는 경로를
  막아 `RemediationPolicy.decide()`의 시간 순서 불변식과 일치시켰으며, 해당 시나리오의 단위 회귀
  테스트를 추가했다.
- M2 A `DynamoDbRemediationExceptionRepository` 직렬화 버그 수정 (PR #45 리뷰 대응): `_put`이
  low-level `transact_write_items`에 plain dict를 그대로 넘겨(다른 리포지토리는 `marshal_item`을
  쓰는데 이 파일만 누락) 실제 AWS 호출에서 직렬화가 깨질 상태였다. `_put`이 `marshal_item(item)`을
  쓰도록 고치고, 단위 테스트 기대값을 AttributeValue 형식으로 갱신했으며, 모든 write item 값이
  AttributeValue로 직렬화되는지 확인하는 회귀 테스트를 추가했다. (query 경로는 resource table의
  auto-marshal이라 무관)

- M2 A Remediation 예외 등록 API를 배포 Lambda에 배선: `RemediationExceptionApiService`와
  `DynamoDbRemediationExceptionRepository`는 이미 dev에 있었으나 composition root
  (`runtime.py`)가 주입하지 않아 `POST /remediation-exceptions`가 배포 Lambda에서 404였다.
  `_remediation_exception_components()`를 추가해 예외 record와 audit event를 한 transaction으로
  쓰는 리포지토리(관리자 전용, `(customer_id, rule_id, rule_version)` 바인딩, 만료 필수)를
  구성·주입했다. 배선 unit 테스트 2건 추가. 이어 `m0-foundation.yaml`에
  `POST /remediation-exceptions` API Gateway 라우트(JWT 인가)를 추가해 배포 Lambda에서 도달
  가능해졌다. cfn-lint E-level 0. **RemediationApiService와 DeploymentApiService는 이번 범위에서
  제외** — 전자는 `RemediationContextReader.get_context`/`RemediationTargetReader.get_target`의
  프로덕션 구현이 없고(테스트 fake만), 후자는 `DeploymentPlanReader.get_approval_input` 구현이
  아예 없으며 D의 plan 저장(당시 ADR-0019 `Proposed`, 이후 `Accepted`)에 의존한다.

- M1 A `record_candidate_extraction` 재시도 idempotency (PR #44 리뷰 대응 3): C의 추출 Worker가
  at-least-once로 같은 결과를 재전송하면 `attribute_not_exists` 조건이 transaction을 취소해 정상
  재시도가 오류로 보였다. 이제 충돌 시 이미 저장된 CANDIDATES·PolicySource item이 지금 쓰려는
  것과 같은 내용이면 흡수하고, 다르면 immutability를 지켜 fail-closed한다
  (`DynamoDbPolicyCatalogBootstrap`과 같은 관용구, transaction 맥락). read table이 없으면 확인
  불가하므로 원래 오류를 유지한다. 동일 내용 흡수·상이 내용 거부 unit 테스트 추가.

- M1 A `load_publication` 게시 입력 의미 명확화 (PR #44 리뷰 대응 2): 리뷰는 "필터가
  `RULE_NOT_APPROVED` 게이트를 앞질러 삼킨다"고 지적했다. 확인 결과 `publish_profile`은 넘어온
  후보를 전부 Profile에 넣고 미승인이면 거부하므로, 게시 입력 자체가 "승인된 Rule"이어야 한다
  (미승인 후보를 섞으면 부분 승인 Source가 영영 게시 불가). 따라서 필터는 유지하되 그 의미를
  "게이트 대신이 아니라 게시 대상 집합을 승인 record로 정의"로 주석·docstring에 명확히 하고,
  잘못된 설명("게이트가 거른다")을 실제 동작에 맞게 고쳤다. 부분 승인(후보 2건 중 1건 승인) 시
  게시 입력에 승인된 후보만 온다는 것을 unit 테스트로 고정했다. `publish_profile`의 승인 검사는
  lifecycle과 승인 record 정합성 이중 방어로 남는다.

- M1 A 승인 API 부분 승인 지원 (PR #44 리뷰 대응 1): `PolicyApprovalApiService.approve`가
  승인할 `approved_rules`(`(rule_id, version)` 목록)를 받아 `load_review` 후보 중 그 부분집합만
  골라 `approve_source()`에 넘긴다. 리뷰어가 추출 후보 전량이 아니라 일부만 승인할 수 있어야
  검토 게이트가 형식으로 남지 않는다(`docs/POLICY_INGESTION.md` 인수 조건 4). handler는
  `POST .../approve` body(`{"approved_rules": [...]}`)를 파싱하고, 후보에 없는 Rule·빈 목록·중복은
  거부한다. B 순수 함수(`approve_source`)는 이미 "고른 것만 받는" 형태라 변경하지 않았다.
  부분 승인·미존재 Rule 거부·빈 선택 거부 unit 테스트 추가. `docs/API.md` 갱신.

- M1 A 승인·게시 API를 배포 Lambda에 배선: `runtime.py`의 `_http_handler`에
  `_policy_approval_components()`를 추가해 `PolicyApprovalApiService`(write는 low-level
  `transaction_client`, read는 resource `table` 주입)를 `policy_approvals`로 주입했다.
  `m0-foundation.yaml`에 `POST /policy-sources/{sourceId}/versions/{version}/approve`와
  `POST /policy-profiles` 라우트(JWT 인가)를 추가해 handler의 승인·게시 경로가 배포 Lambda에서
  도달 가능해졌다. 배선 unit 테스트 2건 추가, cfn-lint E-level 0. 다만 후보를 실제 저장하는
  경로(`record_candidate_extraction` 호출자 = C의 AI 후보 추출 실행)는 아직 없어 E2E 승인 흐름은
  그 조각 이후에 완성된다. 이 통합 브랜치는 PR #41의 업로드 배선 커밋 3건을 cherry-pick으로
  흡수해, 업로드→정규화→승인→게시 API 배선을 한 PR로 담는다(PR #41은 이 PR로 대체).

- M1 A 승인·게시 read 어댑터: `DynamoDbPolicyApprovalRepository`에 `load_review`/`load_publication`을
  구현했다. `load_review`는 `POLICY_INGESTION` item(문서)과 `#CANDIDATES` item(후보)을 읽어
  `(NormalizedPolicyDocument, RuleCandidate 튜플)`을 돌려주고, `load_publication`은 후보 규칙 전체와
  승인 record의 `approved_rules`를 조합해 승인된 후보만 APPROVED로 표시한 뒤 approval·PolicySource와
  함께 돌려준다. read는 자동 un/marshal되는 resource `table`을 쓰고(생성자 `table` 주입, 없으면
  read가 fail-closed), write는 기존 low-level `transaction_client`를 그대로 쓴다. 문서 재구성은
  `policy_ingestion.document_from_item`(get_document에서 추출한 공용 함수)을 재사용하되, 그 모듈이
  api 계층을 참조해 패키지 초기화와 순환하므로 `load_review` 안에서 지연 import한다. read 3건
  (문서·후보 복원, 승인 표시, read table 없을 때 fail-closed) unit 테스트를 추가했다.

- M1 A 정책 후보 추출 결과 persistence: C가 PR #42로 넘긴 `PolicyCandidateExtraction`(READY
  정규화 문서 + 미결정 후보 규칙 전체)을 승인·게시 read 경로가 읽을 형태로 저장하는
  `DynamoDbPolicyApprovalRepository.record_candidate_extraction`을 추가했다. 두 item을 조건부
  transaction으로 함께 쓴다 — `POLICY_SOURCE#{sid}#VERSION#{ver}#CANDIDATES`(후보 규칙 전체,
  `load_review`가 읽음)와 `POLICY_SOURCE#{sid}#VERSION#{ver}`(`PolicySource`, `load_publication`이
  반환·대조). `POLICY_INGESTION` item이 `READY`이고 artifact 바인딩이 일치할 때만 저장해 추측
  저장을 막는다. `PolicySource`의 artifact 바인딩은 문서에서 유도하므로 승인 record 바인딩과
  어긋날 수 없다. 원문·정규화 텍스트는 DynamoDB에 담지 않는다. `docs/DATABASE.md`에 candidate
  SK를 문서화하고 unit 테스트를 추가했다.

- M1 A 정책 원문 업로드 세션 API를 배포 Lambda에 배선: 도메인 코드(`PolicySourceApiService`,
  `DynamoDbPolicySourceUploadRepository`)는 이미 dev에 있었으나 composition root
  (`apps/backend/api/runtime.py`)가 이를 `JobHttpHandler`에 주입하지 않아 `POST
  /policy-sources/uploads`·`GET /policy-sources/{id}/versions/{v}`·`.../process` 라우트가 배포
  Lambda에서 404를 반환했다. `_policy_source_components()`를 추가해 `POLICY_SOURCE_BUCKET_NAME`
  버킷과 tenant-scoped 업로드 세션 리포지토리, 정규화 처리용 S3 reader를 구성하고 `policy_sources`
  ·`policy_reader`로 주입했다. 서버가 `source_id`/`source_version`을 발급하므로 client는 저장
  위치를 고를 수 없다. `DynamoDbPolicySourceUploadRepository`는 `policy_ingestion` 모듈에서 직접
  import한다(패키지 export는 순환 import를 활성화). 배선 성공·버킷 미설정 fail-closed를 검증하는
  unit 테스트 2건을 추가했다. 이어 `m0-foundation.yaml`에 업로드 세션 라우트 3건
  (`POST /policy-sources/uploads`, `GET /policy-sources/{sourceId}/versions/{version}`,
  `POST .../process`, JWT 인가)과 `ApiRuntimeFunction`의 `POLICY_SOURCE_BUCKET_NAME`(=ArtifactBucket)
  환경변수, `ApiRuntimeRole`의 tenant-scoped S3 권한(`s3:PutObject`/`s3:GetObject`를 `customers/*`
  prefix로만)을 추가해 라우트가 배포 Lambda에서 실제 도달 가능해졌다. cfn-lint E-level 0,
  CloudFormation 보안 테스트 통과.

- M3 A/C ADR-0020 파생 Contract 동결: Assessment 계획의 정본을 개수에서
  `(resource_id, rule_id, perspective)` 집합으로 옮겼다. `PlannedEvaluation`을 `packages/contracts/`
  에 두고 `AssessmentEvaluationPlan`이 좌표 집합을 가지며 개수는 거기서 파생하므로 둘이 어긋날 수
  없다. `ASSESSMENT#{assessment_id}#PLAN` item에 `planned_coordinates` 속성이 늘고
  (write 수는 그대로), `get_planned_evaluations()`가 비교 경계에 그 집합을 돌려준다.
  `calculate_readiness_score`는 개수 대신 집합을 받아 완료 집합과 비교한다 — 개수 비교는 계획에
  없던 평가가 누락된 평가의 자리를 채운 경우를 통과시켰다(회귀 테스트로 고정). 집합이 없는 옛
  plan은 재구성하지 않고 readiness `null`·조회 거부로 fail-closed한다. 다중 리소스 Assessment는
  `AssessmentResourceWork.planned_coordinates`로 집합을 주입하고, 단일 리소스는 Worker가 Rule ×
  관점으로 파생한다. 함께: 감사 event 종류 필드를 `event_type` 하나로 통일하고 값 어휘를
  `AuditEventType`(`packages/contracts/audit.py`)으로 고정했으며(`action`은 `RemediationAction`
  payload 전용으로 남는다), D의 `SyncAction` 반환형 `RemediationSyncTarget`을
  `packages/contracts/remediation.py`로 옮겼다

- M1 sandbox readiness hardening: live work를 composition root의 승인 Model Profile에 결합하고,
  lowercase 40자 commit, explicit live/fixture mode, exact selector/ARN/account/Profile Region preflight,
  credential Secret 역할 분리와 canonical GitHub `owner/repository` identity 검증, CloudFormation M1
  parameter all-or-none·빈 CSV 요소·live Region 차단 및 runbook 동기화를 완료. Repository identity는
  preflight·runtime config·최종 REST adapter에서 같은 fail-closed guard를 재사용한다. 최신 `dev` 통합 후
  Ruff 247 files, Unit 430, Contract 98, Integration 9, Security 72, `cfn-lint` error 0,
  Assessment 25-call·Policy Catalog 11-item dry-run 통과

- Docs 정합성 점검 및 수정: `evidence_reference` 정규형을 실행 Contract 정본
  `{source_id}@{source_version}#{locator}`(`packages/contracts/policy.py`)로 통일 —
  `docs/API.md`, `docs/CONTRACTS.md`(예시·서술·중복 bullet 제거) 수정. 재점검에서 발견한
  fixture-vs-contract gap 수정: `fixtures/m1/` golden 3파일의 IaC evidence prefix `github:`(14곳)를
  allow-list(`aws:`/`terraform:`/`s3://`)·런타임과 일치하는 `terraform:`로 교정. `docs/DESIGN.md`의
  ADR 열거를 0001~0021로 최신화. 609개 테스트(unit/contract/security/integration)와 ruff 통과 확인.
- M3 C post-deploy comparison pagination hardening: `ComparisonAssessment`는 results 또는 findings의
  `next_cursor`가 남은 `AssessmentReport`를 받지 않아, 첫 페이지로 계산한 누락 좌표/부분 Readiness
  delta를 fail-closed로 차단한다.

- M3 C post-deploy comparison complete-plan hardening: pagination이 끝난 report라도 결과 좌표가
  immutable planned `Resource × Rule × Perspective` 집합과 정확히 같고 Coverage count가 일치해야만
  비교 입력으로 받는다. 누락/계획 밖 결과 또는 손상된 Coverage로 정상 delta를 위장하는 경로를
  fail-closed로 차단했다.

- M3 C Golden fixture evidence hardening: fixture evaluator가 expected evidence를 그대로 echo하므로
  반복 quality gate만으로는 evidence namespace를 검증하지 못한다. 모든 M1 fixture expected evidence를
  runtime resource allow-list(`aws:`/`terraform:`/`s3://`) 또는 canonical policy reference
  (`{source_id}@{source_version}#{locator}`)로 제한해 잘못된 `github:` 재유입을 차단했다.

- PR #37 review follow-up: complete-input validation과 두 complete Assessment 사이의
  `comparable=false`를 ADR-0020/Contract에 구분해 API 오류 변환 경계를 명시했고, post-deploy
  Golden 18개를 16 PASS + 2 unresolved FAIL로 양극화했다. evidence 검증은 Rule fixture의 실제
  `SourceReference` 집합과 대조하고 위반 case/reference를 출력한다.

- M2 A/C Remediation orchestration (ADR-0018 Accepted): `RemediationDecision`을 유일한 action
  정본으로 고정하고 C가 Remediation Agent/Worker를 소유한다. A API는 target/customer exception을
  읽어 B policy를 호출하고 actionable decision은 context/Job/Outbox/audit와 원자 저장하며,
  `MANUAL_REVIEW`/`SUPPRESSED`는 Job 없이 decision/audit만 기록한다. Admin-only expiring exception
  registration/storage와 tenant isolation을 추가했다. C Worker는 exact revision의 stored work를
  다시 읽어 command/action/identity를 검증하고 injected D Patch/Sync port 하나만 호출한다.
  `SYNC_ACTUAL_STATE`는 C queue command이고 D `RUN_DEPLOYMENT`와 분리된다. D live adapter/runtime
  wiring은 구현하지 않았다.

- M2 B Remediation 허용 범위·예외·Manual Review 정책 경계 (ADR-0017): Finding 하나를
  `TERRAFORM_PATCH`/`ACTUAL_SYNC`/`MANUAL_REVIEW`/`SUPPRESSED` 중 하나로 판정하는 순수 함수와
  Contract 추가. 허용 범위는 Rule version 단위로 `fixtures/rules/remediation.json`에 커밋하고,
  등록되지 않은 Rule은 자동 조치가 열리지 않고 `MANUAL_REVIEW`로 떨어진다. 고객 예외는
  `(customer_id, rule_id, rule_version)`에 묶이고 반드시 만료되며 Rule 새 version으로 승계되지
  않는다. 억제 여부는 두 시각으로 갈린다 — 승인은 Finding 평가 시점보다 앞서야 하고 만료는
  판정 시점 기준이므로, 늦게 들어온 조치 요청에서 나중에 승인된 예외가 옛 Finding을 소급
  억제하지 못한다. `AWS_ACTUAL`/`DRIFT` Finding은 같은 `Resource × Rule`의 IaC 판정이 `PASS`일
  때만 `ACTUAL_SYNC`가 되고, 그 판정이 조치 대상 commit에서 나온 것이어야 하며
  (`iac_commit_sha`), `OUT_OF_SCOPE`/`EXECUTION_ERROR`를 안전으로 읽지 않는다
  (예외 등록·저장 API는 A, Patch 생성 연결은 D)
- M1 C Initial Assessment 3-관점 산출 완료 (ADR-0016): Worker가 `perspective_runners`로 IaC
  본문과 AWS Actual을 각각 평가한 뒤 `DRIFT`를 Code로 결정적으로 파생한다. Drift는 두 판정의
  불일치이며 AI 판정이 아니고, score 정합 100 / 이탈 0에 evidence는 두 관점의 합집합이다.
  Coverage 분모는 `Resource × Rule × Perspective`이고, 다중 관점 Assessment는
  `AssessmentResourceWork.planned_coordinates`(ADR-0020 이후 개수가 아니라 집합)로 서버가 계획을
  고정해 첫 task가 분모를 결정하지 못하게 한다. Readiness Score는 정합 여부가 준수 수준이 아니므로 `DRIFT`를 제외한다
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
- PR #18 benchmark review 후속 완료: fenced JSON 오류 분류, Assessment/Remediation validator,
  unified diff, agreement·quality gate·ranking에 25개 unit test를 추가하고 최신 `TerraformPlan`
  artifact digest를 응답 `plan_hash`에 결합. agreement는 유효 출력 내 최소 Case 결정 일치율임을
  report에 명시했으며 모델 ID와 runtime Model Profile 배정은 변경하지 않음

## Next

- **M2 → M3 → M4 순차 통합 PR (M4의 `dev` 병합까지 한시 적용):**
  1. M2 PR은 D의 live GitHub branch/commit/PR·refreshed plan/runtime 배선과
     Shared Approval·Security·Patch/Plan 통합 검증을 묶는다(`AuditEventType` 신설과 audit
     `event_type` 정규화는 M3 A/C Contract 동결에서 이미 끝났다). ADR-0019가 막는 live plan 구현 전
     이 PR을 열어 A·D·Security approve를 받고 `Accepted` 상태·관련 정본 동기화 커밋을 먼저
     완료하며, 이후 구현 커밋을 추가한 뒤 전체 PR을 다시 Review·검증한다.
  2. M3 PR은 M2가 `dev`에 병합된 뒤 시작한다. Contract 커밋은 Producer/Consumer Owner가 먼저
     동결하고, planned 집합 저장과 A/B/C/D/Shared 통합을 세부 커밋으로 이어 붙인 뒤 전체 검증한다.
  3. M4 PR은 M3가 `dev`에 병합되고 M0–M3 Exit criteria가 충족된 뒤 시작한다. protected sandbox의
     Demo·Golden·관측·비용·E2E 증적과 문서 Freshness를 확인해 `dev`에 병합한다. 이후 Release gate
     증적을 첨부한 별도 `dev → main` PR은 사람이 생성한다.
- **M2 PR 내부 선행 checkpoint (ADR-0019):** 별도 회의를 열지 않고 ADR-0019를 담은 M2 PR에
  A·D·Security가 approve하는 것으로 서명을 대신한다. 미정 항목은 없고 Decision 1~8에 결정과
  근거가 모두 들어 있다. 세 Owner의 approve가 모이면 같은 PR에서 상태를 `Accepted`로 바꾸고,
  차단됐던 live plan 구현 커밋은 그 이후에만 시작한다.
- **M3 A (ADR-0020 파생분의 남은 절반):** planned 집합은 저장되지만 검증 Assessment의 선택자는
  아직 없다. Assessment item에 `phase`/`source_assessment_id`/`deployment_id`를 영속화하고
  `apps/backend/assessment/runtime.py`의 `AssessmentPhase.INITIAL` 하드코딩을 인자로 바꾼 뒤,
  `GET /deployments/{deploymentId}/verification`을 `compare_post_deploy_assessments()`에 배선한다
  (ADR-0020 §1·§7). 계획 집합 주입은 `DynamoDbAssessmentReportStore.get_planned_evaluations()`로
  이미 가능하다
- **M3 A endpoint 배선 (Contract 동결 이후):** Deployment 생성 `POST /remediations/{id}/deployments`,
  `GET /deployments/{id}`, `GET /deployments/{id}/verification`, `POST /deployments/{id}/reject`를
  방금 동결한 Contract(`DeploymentStatus`/`derive_deployment_status()`, D port, `plan_hash` 투영,
  `START_DEPLOYMENT`/`REJECT_DEPLOYMENT`, `DEPLOYMENT_REQUESTED`/`DEPLOYMENT_REJECTED`) 위에 올린다.
  검증 조회는 `compare_post_deploy_assessments()`에 complete `ComparisonAssessment` 두 개를
  fail-closed로 배선한다 (ADR-0019 §4·§8, ADR-0020 §1·§7).
- **M2 A:** 감사 event 종류 필드는 `event_type`으로 통일됐다. 남은 것은 그 위에 올릴 Admin
  `GET /audit-events` 조회다. ADR-0019 합의로 `AuditEventType`에 값 7개가 늘 때 같은 어휘를 쓴다.
- **M3 C:** `POST_DEPLOY_VERIFICATION` phase의 18개 Golden Case(6 Rule × 3 perspective)를 추가했다.
  16개 정합 `PASS` snapshot과 logging 설정이 남은 Actual/Drift 2개 `FAIL` snapshot이며 원 Assessment와
  같은 rubric을 쓴다. fixture gate는 통과했고, 실제 Bedrock 반복 평가는 M4 customer sandbox gate로
  남는다 (ADR-0020 §3).
- **M1 실제 검증 선행:** 고객 관리자가 `m1-customer-bootstrap.yaml`을 자신의 sandbox
  계정에 한 번 실행해 exact GitHub Environment OIDC deployment role, versioned Lambda-code
  bucket, foundation-only CloudFormation execution role을 만든다. 이어 현재 저장소에 서로 다른
  protected Environment (`customer-sandbox-artifact`, `customer-sandbox-deploy`)과 Required
  reviewers/같은 `EXPECTED_AWS_ACCOUNT_ID`를 설정하고, deploy Environment에
  `M1_ASSESSMENT_RUNTIME_JSON`, `M1_ASSESSMENT_SECRET_ARNS`,
  `M1_ASSESSMENT_READ_ROLE_ARNS`를 등록한다. 이 설정 전에는 고객 sandbox 배포나 실제
  GitHub/AWS/Bedrock E2E를 시작하지 않는다. IAC 관점이 `git/blobs`를 읽으므로 GitHub App
  installation token에는 승인 repository의 Contents read 권한이 필요하다.
- M2 D/Shared: C Worker의 `PatchAction`/`SyncAction` port에 live GitHub branch/commit/PR와
  Terraform refresh-plan adapter를 연결하고, customer-approved runtime identity로 Remediation
  Lambda/SQS event source를 배선한다. `RUN_DEPLOYMENT`와 Human Approval/Apply는 D Deployment Worker에
  남기며 A/B/C mock flow를 변경하지 않는다
- M1 A/C: 대규모 Assessment 페이지 조회 비용을 줄이기 위해 immutable 결과 저장과 같은
  DynamoDB transaction에서 Assessment plan의 completed counter를 갱신하는 storage migration.
  같은 작업에서 `findings`도 페이지네이션한다 (현재는 페이지마다 전체 Finding을 반환한다)
- M4 C external evidence: A/D가 protected customer sandbox의 Post-Deploy artifact set과 실제 Demo
  실행을 제공하면 `docs/M4-GOLDEN-RELEASE-GATE.md` 절차로 18 Case × 5 run private observation
  bundle을 만들고 `scripts/evaluate_m4_golden_release_gate.py --observations ...`로 sanitized report를
  생성한다. Dry-run의 `EXTERNAL_EVIDENCE_REQUIRED`나 fixture 결과는 release 통과 근거가 아니다.
  같은 `execution_id`와 repository/deployment/artifact digest로 A 관측·비용, D plan/apply 증적을
  결합한다 (ADR-0022).
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

- M1 actual sandbox validation: 두 protected GitHub Environment에 required reviewer가 없고 deploy
  Environment의 `M1_ASSESSMENT_MODE` 및 M1 Secret 3개가 미설정이다. 로컬 AWS credential도 없으므로
  bootstrap/runtime target 생성과 실제 workflow dispatch는 고객 관리자·승인자 작업 대기
- M1/M4 live Golden quality evidence: Initial FAIL/FAIL pair의 DRIFT 기대값은 ADR-0011에 맞춰
  PASS/100으로 교정했고, Post-Deploy 18 Case의 profile/case/run/artifact-bound M4 gate도 구현했다.
  남은 차단은 customer artifact resolver/exporter, protected customer runtime·Demo repository,
  AWS/GitHub 승인과 실제 observation bundle이다. 이 입력 전에는 generic benchmark, fixture evaluator,
  synthetic observation을 M1/M4 release gate 통과로 간주하지 않는다.
- ~~**M2 live plan·audit 정본화 및 M3 착수 전 서명 필요 (ADR-0019 `Proposed`)**~~ **해소됨
  (2026-09-02): ADR-0019 `Accepted`.** A·D·Security가 서명 PR 리뷰 approve로 서명했다. 이로써
  M2 A audit 정본화, M2/M3 D live plan/apply, M3 A Deployment 생성·상태 API가 구현 가능해졌다.
  구현 커밋은 서명 뒤에 A(PR #40)와 D 브랜치에서 병렬로 이어 붙이고, 완료 시 관련 milestone
  항목을 `[x]`로 옮긴다.
- **M3 integration 의존성 (ADR-0020 `Accepted`):** C의 비교 projection은 complete immutable Assessment
  input을 요구하고 부분 report(cursor가 남은 report)를 fail-closed로 거부한다. A는
  `phase`/`source_assessment_id`/`deployment_id`, profile/rubric, 그리고 planned
  `(resource_id, rule_id, perspective)` **집합**의 durable 저장·조회와 endpoint 배선을, D는 apply
  완료 뒤 Actual 재조회 입력을 제공해야 한다. 예외는 조회 시 표시만 하며 평가를 막지 않는다.
  **planned 집합 저장은 해소됐다** — `ASSESSMENT#{id}#PLAN` item의 `planned_coordinates` 속성과
  `DynamoDbAssessmentReportStore.get_planned_evaluations()` 조회가 들어갔다. 남은 차단 요인은
  `phase`/`source_assessment_id`/`deployment_id` 영속화와 D의 apply 후 Actual 재조회 입력이다.
  *Owner:* A + D (+ B exception read). *Blocks:* live M3 verification endpoint와 M4 customer runtime report,
  C의 mock/contract implementation은 차단하지 않는다.

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
  까지 검증 완료; 고객 Bedrock 품질 Gate는 sandbox 실행 대기. Initial 18건과 Post-Deploy Verification
  18건은 각각 6 rule × 3 perspective로 `IAC`/`AWS_ACTUAL`/`DRIFT` Case를 모두 가진다)*
- [x] **D — Remediation/GitHub/Deployment:** 승인 Repository IaC Snapshot과 AWS Resource Read-Only 연결 *(read-only Tool 경계 + Assessment 입력 조합 계층, S3 AssumeRole, GitHub REST commit/tree/blob read adapter 구현 완료. IAC 관점용 `IaCDocument` 본문 read 포함, write 표면 없음; 고객 GitHub App/runtime injection E2E 대기)*
- [x] **Shared:** Contract/Integration Test, Golden Dataset 반복 평가, Score/Coverage 표시 검증 *(3관점 Initial Assessment integration test, Drift 파생 unit test, Coverage/Readiness/Finding 표시 검증 완료; Golden Dataset 반복 평가는 기존 M0 runner 유지, 확대 Rule 재고정은 Next)*

**Dependencies:** C는 B의 승인된 Policy Context와 D의 Snapshot/Read-Only Interface를 사용한다. A가 Job/상태 Contract를 제공한다. 고객 정책 업로드 기능은 A의 upload/storage, B의 normalization/approval, C의 AI extraction quality gate와 Shared 보안·통합 테스트가 모두 필요하다.

### M2 — Remediation and deployment readiness

**Exit criteria:** 선택된 Finding에서 최소 Terraform Patch, Branch/Commit/PR, CI 및 Deployment Readiness Validation/plan까지 이어진다.

- [x] **A — Platform/Backend:** Remediation/Deployment API, Job 재개, Approval 상태 전이와 Audit Log *(B policy gate, customer exception registration/read, canonical decision/context/Job/Outbox/audit transaction, 200/202 public response, authoritative revision work reader까지 mockable 구현 완료; customer runtime wiring 대기)*
- [x] **B — Policy/Governance Boundary:** Remediation 허용 범위·예외·Manual Review 정책 제공 *(Rule version 단위 허용 범위 Registry, 만료되는 고객 예외, 조치 유형·Manual Review 사유 판정 구현 완료. 예외 등록·저장 API는 A, Patch 생성 연결은 D)*
- [x] **C — AI Evaluation & Agent Orchestration:** Finding 근거 기반 Remediation Context, C-owned revision-bound Remediation Worker, Deployment Readiness 평가 *(duplicate strategy 제거, stored decision command matrix와 injected Patch/Sync ports, stale/mismatch fail-closed 검증 완료)*
- [ ] **D — Remediation/GitHub/Deployment:** Patch/Diff, GitHub PR, OIDC Terraform Plan, `commit_sha`/`plan_hash` 생성 *(`plan_hash`의 대상 바이트와 destructive 판정은 ADR-0019 `Accepted`로 확정돼 `packages/contracts/terraform_plan.py`에 공용 함수로 구현됨. GitHub write 제안 경계(`ProposedPullRequest`)까지 완료. 남은 조각은 live GitHub branch/commit/PR·Terraform plan SDK adapter와 customer runtime 배선)*
- [ ] **Shared:** Approval Contract/보안 Review, Patch/Plan Integration Test

**Dependencies:** D의 Plan 결과와 C의 Readiness 결과는 A의 Approval/Deployment 상태에 바인딩한다.

### M3 — Approved apply and post-deploy verification

**Exit criteria:** Human Approval 뒤 승인된 plan만 apply하고, 변경된 AWS Actual을 Post-Deploy Verification으로 재평가해 Finding 및 Readiness Score 변화를 확인한다.

**결정:** ADR-0020은 `Accepted`다. 검증은 새 immutable Assessment, 원 평가 계획 전체 재실행,
동일 Profile/rubric, Code의 Finding Resolution 및 fail-closed comparison을 사용한다. ADR-0019의
plan_hash·state·merge commit·deployment_id·apply 경계는 `Accepted`로 확정됐다(구현 대기).

- [x] **A — Platform/Backend:** Approval 권한 검증, 상태 전이, Audit/Observability, 결과 조회 API
  *(Deployment 생성 `POST /remediations/{id}/deployments`, `GET /deployments/{id}`(파생 상태),
  `GET /deployments/{id}/verification`(비교), Admin `POST /deployments/{id}/reject`와 record store를
  D 실행 Contract(PR #49) 위에 구현. 생성·reject는 durable 배선, approve/get/verification은 D live
  reader 조립기 대기로 fail-closed — ADR-0019 §4·§8, ADR-0020 §7)*
- [x] **B — Policy/Governance Boundary:** 재평가 적용 범위와 예외 처리 검증 *(검증 phase의 Profile
  version pin 해석과 6개 S3 Rule 적용 가능성을 회귀로 고정하고, 예외의 조회 시점 표시 경계
  `annotate_suppressed_findings()`를 조치 판정과 같은 술어로 구현 — ADR-0020 §2, §6. 조회 API
  배선은 A)*
- [x] **C — AI Evaluation:** Before/After 비교, Finding Resolution, Score/Coverage 변화 평가
  *(immutable complete-plan input Contract, Profile/rubric/plan/score fail-closed comparison, 5개
  Resolution의 결정적 projection 및 18개 Post-Deploy Golden fixture 구현. durable Assessment/endpoint
  wiring은 A/D integration 의존성)*
- [ ] **D — Remediation/GitHub/Deployment:** GitHub Actions OIDC Apply, 승인 `commit_sha`/`plan_hash`
  재검증, AWS Actual 재조회 *(D 소유 코드·어댑터·template 완료: `plan_hash` 허용 목록 투영·destructive
  판정 공용 함수(`packages/contracts/terraform_plan.py`), state `lineage`·`serial` 쌍 대조,
  `PlanRequestPort`/`PlanRequestOutcome`, `TERRAFORM_PLAN_BINARY`, 세 command를 injected port로
  분기하는 `DeploymentWorker`, 세 live 어댑터(`agent/runtime/live_deployment_ports.py`)와 고객용
  `ci/terraform/` plan/apply workflow template·canonical `plan_hash` 스크립트 — ADR-0019
  §1·§2·§5·§6·§7. customer runtime 배선도 대부분 완료: approval read, plan/run/verification store 3종
  (`#EVENT#{run_id}` 예약→`VERIFIED` 확정 포함), `DynamoDbDeploymentWorkRepository`(예약 EVENT에서
  `run_reference` 채움), fail-closed `DeploymentRuntimeConfiguration`, Deployment Worker composition
  root. apply 완료 Event 경계는 A/D 공유 계약으로 확정(ADR-0019 §7, DATABASE.md "완료 Event 경계") —
  A/EventBridge가 예약 write, D가 재조회·확정. `LivePlanRequestPort`와 `_live_worker`(D port 4종·
  store 3종·work repo 조립, I/O seam 주입)까지 구현해 **D의 코드 조각은 모두 완료**. 유일하게 실제
  검증이 남은 건 `_live_plan_outputs_fetcher`의 GitHub plan run I/O로, 실제 sandbox 자격 증명·
  네트워크가 있어야 동작·검증된다(그전까지 호출 시 명시적으로 막음). A의 `#EVENT` 예약 write와
  protected Environment/OIDC Role/자격 증명이 준비되면 live E2E가 열린다)*
- [ ] **Shared:** 승인 없는 Write 방지, End-to-End Security/Integration Test

**Dependencies:** Apply는 D의 OIDC 경로만 사용하며, A의 승인 상태와 C의 평가 결과를 우회할 수 없다.

### M4 — Demo and release readiness

**Exit criteria:** WordPress/LAMP Demo에서 폐루프 E2E가 재현되고, 품질·운영·문서 기준을 충족해 사람이 `dev → main` 통합 PR을 만들 수 있다.

**결정:** ADR-0021은 `Accepted`다. 데모 IaC 위치, 차단형 품질 Gate, 관측·비용 기록, `dev → main`
첨부물은 `CONTRIBUTING.md`의 release checklist를 따른다.

- [ ] **A — Platform/Backend:** 배포/운영 점검, 오류·성능·비용 관측 검증 *(통과 기준은 값의 존재로
  정의한다 — ADR-0021 §3)*
- [x] **B — Policy/Governance Boundary:** Demo Policy/Rule/근거와 Coverage 설명 검증
  *(`m4-demo-policy-coverage-v1` manifest와 strict validator/CLI가 `profile-mvp-baseline@v2`의
  6 Rule, 5 Control, 12 version-pinned Rule/Control locator, Initial/Post-Deploy × 3 perspective
  36 Golden case를 Registry/fixture와 교차 검증한다. 외부 Demo repository는 여섯 semantic
  `demo_toggle`을 D runbook에서 매핑하며 실제 repository/IaC/credential은 manifest에 넣지 않는다)*
- [x] **C — AI Evaluation:** Golden Dataset 품질 목표의 executable fixture/customer-observation
  gate 완료 *(Initial/Post-Deploy 각 6 Rule × IAC/AWS_ACTUAL/DRIFT의 36-case fixture gate와,
  Post-Deploy 18 Case × 5 run의 customer-sandbox observation gate 구현. live gate는 60 Bedrock
  IAC/Actual과 같은 run의 30 Code-derived DRIFT, exact Profile/rubric/artifact binding, per-case/
  perspective/overall threshold를 검증하고 sanitized report만 출력한다. 실제 protected run report는
  A/D sandbox 준비 뒤 release 증적으로 생성하며 dry-run/fixture는 근거가 아니다 — ADR-0021/0022)*
- [ ] **D — Remediation/GitHub/Deployment:** Demo IaC, Plan/Apply/검증 runbook 확인 *(데모 IaC는 별도
  고객 sandbox repository — ADR-0021 §1. **문서 몫 완료:** `docs/M4-DEMO-IAC-REFERENCE.md`(별도
  데모 저장소 식별자·전제조건·6개 S3 Rule 1:1 위반 토글 매핑·세 관점 재현), `docs/M4-DEMO-RUNBOOK.md`
  (Initial→Remediation/PR→plan→승인→apply→Post-Deploy Verification 폐루프, 재조회 시점 규칙,
  ADR-0021 §3 관측·비용 기록 표), `fixtures/terraform/README.md`(비어 있음 이유·참조). ADR-0022 §4의
  **D producer 결합 로직도 구현**: `apps/backend/deployment/release_binding.py`의
  `derive_release_binding()`가 demo commit/deployment ID/artifact set을 세 SHA-256(bundle의
  `repository_commit_sha256`/`deployment_id_sha256`/`artifact_set_sha256`)으로 결정적 결합하며, C
  parser의 `_digest` 관문과 정합함을 회귀 테스트로 고정했다. **남은 조각:** 실제 데모 저장소 생성·
  sandbox 폐루프 실행·관측/비용 값 채우기(실제 commit/ID/artifact 주입)는 protected Environment·
  OIDC Role·자격 증명 대기)*
- [ ] **Shared:** C4/ADR/API/Contract Freshness, E2E, Secret Scan, Release/Demo Review

**Dependencies:** 모든 M0–M3 Exit criteria 충족 후에만 `dev → main` PR과 최종 Release 검증을 진행한다.
