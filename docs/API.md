# API Contract

## Conventions

- 인증: Cognito access-token JWT. Backend는 `sub`, `client_id`, `custom:customer_id`,
  `cognito:groups`를 fail-closed로 검증하고 Role과 Customer/Repository/AWS Account Scope를
  검증한다.
- 장시간 작업: `202 Accepted`와 `job_id`를 반환한다.
- 모든 요청·응답은 버전 관리되는 `packages/contracts/` 스키마를 따른다.
- Client는 `customer_id`, Job ID, Job revision, status, timestamp, TTL 또는 DynamoDB key를
  요청 body에 보낼 수 없다. Backend가 verified JWT와 server state에서 이 값을 결정한다.
- Assessment, Remediation, Deployment의 public API는 Queue·Worker 이름 또는 checkpoint를
  노출하지 않는다. Backend는 검증 뒤 `WorkflowTask` outbox를 영속화하고 내부 dispatcher가 Queue로
  전송하며 Client는 `GET /jobs/{jobId}`로 상태를 조회한다. M0 Assessment API는 전송을 즉시 시도하고,
  전송 실패 시 durable Outbox sweeper가 재시도한다.

## Initial endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/assessments` | Assessment Job 생성 |
| `GET` | `/jobs/{jobId}` | Job 상태와 결과 조회 |
| `GET` | `/assessments/{assessmentId}` | Assessment 및 Coverage 조회 |
| `POST` | `/findings/{findingId}/remediations` | B policy 판정 후 Remediation 시작 또는 non-action decision 보고. `TERRAFORM_PATCH`면 Worker가 patch를 생성·저장한 뒤 승인 repository에 branch/commit/PR을 연다(`DEPLOYMENT_RUNTIME_JSON` 필요) |
| `POST` | `/remediation-exceptions` | Admin이 만료 필수 고객 예외를 승인·등록 |
| `POST` | `/deployments/{deploymentId}/approve` | 승인된 commit/plan으로 배포 승인 |
| `POST` | `/deployments/{deploymentId}/reject` | 배포 거절 |
| `GET` | `/deployments/{deploymentId}/observability` | Admin 전용 데모 실행 관측·비용 조회 |
| `GET` | `/audit-events` | Admin 전용 감사 이력 조회 |
| `POST` | `/orchestrate` | 자연어 메시지를 PolicyQA 답변 또는 워크플로 제안으로 라우팅 (ADR-0012). Parent는 제안·답변만 하고 Job을 만들지 않는다 |

**"배선됨"은 handler branch와 API Gateway route가 **둘 다** 있다는 뜻이다.** API Gateway는 명시적
allow-list이므로 route가 선언되지 않은 경로는 handler에 닿기 전에 404다. handler에 branch만 있고
route가 없는 상태는 문서상 배선이 아니며, 그 불일치는
`tests/security/test_cloudformation_security.py`의 회귀가 handler의 분기 조건을 직접 읽어 막는다
(사람이 유지하는 route 목록은 branch가 늘어날 때 조용히 낡는다 — 실제로 세 endpoint가 그렇게
route 없이 남아 있었다).

## Natural-language orchestration endpoint

`POST /orchestrate`는 인증된 모든 사용자가 호출할 수 있다(`Action.ORCHESTRATE`, ADR-0012). LangGraph
Parent Orchestrator가 메시지를 분류해 다음 중 하나를 돌려준다. **Parent는 워크플로를 시작하지
않는다** — Job 생성·scope 검증·승인은 각 endpoint가 JWT로 다시 검증한다.

- 요청: `{"message": "<자연어>", "policy_profile_id?": "<Profile ID>"}`
  - `policy_profile_id`는 선택이다. 주면 Backend가 **호출자 customer partition 안에서** 그 Profile의
    게시된 Rule을 조회해 Parent에 읽기 전용 grounding으로 넘긴다 — POLICY_QA 답변이 일반 개념이 아니라
    그 Profile의 실제 Rule(rule_id·title·requirement·rubric)에 근거하게 된다. 다른 customer의 Profile을
    지정해도 자기 partition에서만 조회하므로 scope가 넓어지지 않고, 미해결 Profile은 grounding 없이 라우팅만 한다.
- 응답 `200`: `{intent, rationale, answer?, selector?, requires_confirmation}`
  - `intent` ∈ `POLICY_QA` | `ASSESSMENT` | `REMEDIATION` | `DEPLOYMENT` | `UNSUPPORTED`
  - `POLICY_QA`면 `answer`에 직접 답변, 워크플로 intent면 `selector`에 후보
    (`repository_id`, `policy_profile_id`, `finding_id`, `remediation_id` 중 해당 값)와
    `requires_confirmation`이 온다. Client는 사용자 확인 뒤 해당 endpoint를 직접 호출한다.
- 배선됨: handler branch + API Gateway route(`PostOrchestrateRoute`) + SPA 챗봇 UI.
  라이브 반영은 `Deploy M0 Foundation` 재배포 시점을 따른다.

## Customer policy ingestion endpoints

업로드 세션 3개(`uploads`/`process`/status 조회), 후보 추출(`/candidates`), 승인(`/approve`),
Profile 게시(`/policy-profiles`)가 API Gateway 라우트와 Lambda composition root
(`apps/backend/api/runtime.py`)에 배선돼 노출된다. 후보를 저장하는 경로는 Policy Authoring
Worker이며(ADR-0023), 승인·게시의 검토 read는 **READY authoring manifest**가 선언한 후보만
읽는다 — 일부만 쓰인 후보 집합을 완전한 것으로 읽으면 승인 경계가 형식이 된다. 상세 workflow와
인수 조건은 `docs/POLICY_INGESTION.md`를 따른다.

| Method | Path | Status | Purpose |
| --- | --- | --- | --- |
| `POST` | `/policy-sources/uploads` | 배선됨 | JWT-derived customer Scope의 업로드 세션 생성 |
| `GET` | `/policy-sources` | 배선됨 | 호출자 customer의 업로드 문서 목록(요약: `source_id, source_version, filename, status, source_format, byte_size, unit_count`). 원문·units·정규화 text는 반환하지 않는다 |
| `POST` | `/policy-sources/{sourceId}/versions/{version}/process` | 배선됨 | 업로드 검증과 파싱·정규화 실행 |
| `GET` | `/policy-sources/{sourceId}/versions/{version}` | 배선됨 | 처리 상태, 형식 지원 여부와 검토 경고 조회 |
| `DELETE` | `/policy-sources/{sourceId}/versions/{version}` | 배선됨 | 미승인 문서 삭제(DynamoDB record를 먼저 지우고 S3 원본·정규화 아티팩트를 지운다). 승인 record가 있는 Source는 `409`로 거부 — Profile이 참조하는 evidence를 지우지 않는다. 호출자 partition에 그 판본이 없으면 `404` |
| `POST` | `/policy-sources/{sourceId}/versions/{version}/candidates` | 배선됨 | 후보 추출 요청. `202`와 `{authoring_run_id, status}` |
| `GET` | `/policy-sources/{sourceId}/versions/{version}/candidates` | 배선됨 | 실행 상태와 후보/미지원/거절 결과 페이지 |
| `POST` | `/policy-sources/{sourceId}/versions/{version}/approve` | 배선됨 | 검토된 Source/Control/Rule version 승인 |
| `POST` | `/policy-profiles` | 배선됨 | 승인된 Rule version으로 versioned Policy Profile 게시 |
| `POST` | `/policy-profiles/{profileId}/versions` | 대기 | 승인된 Rule version으로 Profile 새 version 게시 |

## Admin console endpoints

관리자 콘솔이 쓰는 read/관리 endpoint다. 모두 JWT authorizer를 거치며, 사용자 관리는
`MANAGE_USERS`(Admin 전용) action을 요구하고 호출자의 `custom:customer_id` scope로만 동작한다.

| Method | Path | 상태 | 설명 |
| --- | --- | --- | --- |
| `GET` | `/scope` | 배선됨 | 호출자 customer의 assessment scope에 연결된 대상 목록(`{customer_id, repositories:[{repository_id, github_repository?, aws_account_id?}]}`). 배포 구성(`ASSESSMENT_SCOPE_JSON`)에서 읽으며, 비밀 아닌 연결 정보(GitHub repo full name·AWS 계정 ID)만 노출하고 secret 참조(role ARN·secret id)는 반환하지 않는다. 다른 customer의 scope는 반환하지 않는다 |
| `POST` | `/admin/users` | 배선됨 | Admin이 customer scope의 사용자 생성(`{email, role, temporary_password}` → `201 {email, role, customer_id}`). role은 `Admin`/`User`, 새 사용자는 호출자의 `custom:customer_id`로 고정. `temporary_password`는 영구 비밀번호로 설정된다(콘솔에 첫 로그인 변경 flow가 없다). pool 전역에서 이미 쓰이는 email이면 `400`. email은 소문자로 정규화해 저장한다(pool username이 대소문자 구분). `temporary_password`는 pool 정책(8자 이상 + 대·소문자·숫자·기호)을 요청 단계에서 검증해 미달이면 `400`. 그룹 추가·비밀번호 설정이 실패하면 방금 만든 사용자를 삭제해 로그인 불가능한 반쪽 계정을 남기지 않는다 |
| `GET` | `/admin/users` | 배선됨 | Admin이 자기 customer의 사용자 목록 조회(`{users:[{username, email, customer_id, profile, status, enabled}]}`). 비밀번호는 반환하지 않는다 |
| `POST` | `/admin/users/profile` | 배선됨 | Admin이 사용자에게 기본 Policy Profile 지정(`{email, policy_profile_id}`). Cognito 표준 `profile` 속성에 저장되어 사용자가 로그인 시 자기 token에서 읽는다. 대상 사용자가 호출자 customer 소속이 아니거나 존재하지 않으면 동일하게 `403` — 존재 여부를 customer 경계 너머로 알리지 않는다 |
| `DELETE` | `/admin/users` | 배선됨 | Admin이 자기 customer scope의 사용자 삭제(`{email}` → `200 {email, deleted:true}`). 삭제 전에 대상의 `custom:customer_id`를 읽어 호출자 customer 소속임을 확인한다. 대상이 호출자 customer 소속이 아니거나 존재하지 않으면 동일하게 `403` — 존재 여부를 customer 경계 너머로 알리지 않는다. 과거 audit event는 지우지 않는다 |

### 후보 조회 응답

`GET .../candidates`는 `limit`(1–50)과 opaque `cursor`로 페이지네이션한다. 완결되지 않은 실행은
상태와 provenance만 돌려주고 후보는 비운다 — 부분 결과를 전체로 착각한 승인을 막는다.

후보 항목은 모델이 쓴 재진술(`requirement`, `requirement_summary`, `mapping_reason`), 매핑된
Control과 실행 유형, evidence capability, 그리고 **서버가 만든** locator + `content_sha256`를
담는다. `proposed_severity`는 Governance Control Catalog가 정한 read-only 값이며, 리뷰어는 그것을
승인하거나 후보를 거절한다 — 화면에서 등급을 고르게 하면 AI가 만든 근거와 사람이 정한 등급이
섞여 나중에 누가 무엇을 정했는지 말할 수 없다.

응답에는 정규화 문서의 원문이 들어가지 않는다. `judgment`·`score`·`source_score`·`anchor`·
`severity` 필드는 존재하지 않는다.

업로드 세션 응답이 후속 호출에 필요한 `sourceId`와 `version`을 돌려준다. Client는 이 값을
그대로 사용하며 스스로 만들지 않는다.

승인과 Profile 게시는 서로 다른 operation이다. `/approve`는 body로 승인할 Rule 목록
(`{"approved_rules": [{"rule_id", "version"}, ...]}`)을 받아 그 부분집합만 확정한다 — 리뷰어가
추출 후보 6건 중 4건만 고를 수 있어야 하므로, AI 후보 전량에 서명을 찍지 않는다
(`docs/POLICY_INGESTION.md` 인수 조건 4). 목록에 없는 후보는 CANDIDATE로 남는다. Profile 게시가
그 승인된 Rule들을 평가 경계로 만든다. 게시는 승인되지 않은 Source·Rule을
참조하거나 승인된 것과 다른 Source version을 가리키는 Profile을 거부한다. 두 단계를 하나의
operation으로 합치더라도 이 거부 조건과 audit record 기록은 동일하게 적용한다.

`/approve`는 `approve_source()`를, Profile 게시는 `publish_profile()`을 호출한다. 두 함수는
아무것도 영속화하지 않는 순수 판정이므로, A가 DynamoDB 조건부 write 앞에서 호출하고 거부
시에는 write를 시도하지 않는다. 거부 사유는 `ApprovalRejectionCode` 열거값이며 응답의 오류
코드로 그대로 쓸 수 있다 — 자유 문장이 아니라서 정책 원문이 응답이나 로그로 새지 않는다.

경로와 wire shape는 구현 PR의 Producer/Consumer Contract Review에서 최종 확정한다. Client는
`customer_id`, S3 bucket/key, checksum 판정, parser/status를 직접 지정할 수 없다. 업로드 성공은
정책 승인이나 Assessment 활성화를 의미하지 않는다.

## Assessment boundary payloads

- Assessment 생성 요청은 승인된 `repository_id`, `policy_profile_id`만 지정한다. Resource/AWS
  Account Scope는 JWT claim과 보호된 Worker runtime 설정에서 판정하며 요청 body에는 포함하지
  않는다. target에 여러 리소스가 승인돼 있으면 Worker가 모두 하나의 평가 계획으로 확장한다.
  **어떤 Profile을 쓸 수 있는지는 고객 partition의 Policy Catalog가 정한다** — 배포 구성이 아니다
  (ADR-0023). 게시되지 않은 Profile을 지정하면 생성 단계에서 거절한다.
- Assessment 생성은 그 시점의 current Profile 판본을 고정한다. Runtime은 latest pointer를 따라가지
  않고 고정된 판본을 직접 조회하므로, 실행 도중 새 Profile이 게시돼도 이 Assessment의 Rule 집합은
  바뀌지 않는다. 판본 고정은 모든 phase에 적용된다(ADR-0020 amendment).
- 승인된 MANUAL Rule은 `MANUAL` 관점 결과를 만든다. 좌표는 `AWS::Governance::Assessment` 유형의
  `governance:{repository_id}`이며 Repository 단위로 안정적이다. 이 결과는 readiness 숫자 평균에서
  제외되지만 Coverage와 plan 완료에는 포함된다. live M1 Worker는 고정된 Profile 판본에 MANUAL Rule이
  있을 때 이 governance work를 자동으로 추가하며, 계획 좌표는 Rule의 `evaluation_type`이 정하는
  Perspective 집합만 담는다(IaC 전용 Rule에 AWS_ACTUAL/DRIFT 좌표를 계획하지 않는다).
- `INSUFFICIENT_EVIDENCE`는 모델의 답일 수도, Code의 답일 수도 있다. AWS_ACTUAL 관점에서 authored
  Rule의 `required_evidence` 문서 경로가 read 결과에 비어 있으면 Runtime이 모델을 부르지 않고 이
  상태를 기록한다. `rationale`에 빠진 경로 이름이, `evidence_references`에 수행한 `aws:` read
  locator가 남는다.
- Policy Profile 조회·평가는 `PolicyProfile.rule_references`로 version이 고정된 Rule만 사용하고,
  정책 Evidence는 `SourceReference.evidence_reference`의 `{source_id}@{source_version}#{locator}`
  정규형으로 반환한다. AWS Actual Evidence는 `aws:` namespace를 사용한다.
- Initial Assessment 결과는 같은 관리 대상의 `IAC`, `AWS_ACTUAL`, `DRIFT`, `MANUAL` 관점을
  구분해 반환한다. 어떤 관점이 생기는지는 Rule의 `evaluation_type`이 정한다 — IaC 전용 Rule은
  `IAC`만, AWS 전용 Rule은 `AWS_ACTUAL`만 만들고 Drift 비교 대상이 아니다. Drift는 Finding 근거일 뿐 API나 AI가 고객 워크로드를 직접 변경할
  권한을 부여하지 않는다.
- `GET /assessments/{assessmentId}`의 Coverage는 서버가 Assessment 시작 시 저장한 적용 가능
  `Resource × Rule × Perspective` 계획을 분모로 사용한다. 응답에는
  `planned_evaluations`, `completed_evaluations`, `percentage`가 포함되며,
  `EXECUTION_ERROR`는 완료 수에 포함하지 않는다.
- 결과 목록은 `limit`(1–100)과 opaque `cursor`로, Findings는 별도 opaque `findings_cursor`로
  독립 페이지네이션한다. 응답의 `next_cursor`와 `findings_next_cursor`가 각각 `null`이면 해당
  목록의 마지막 페이지다. 새 Assessment의 `coverage`는 Result/Finding write와 같은 DynamoDB
  transaction에서 갱신되는 immutable plan 완료 counter를 읽으므로, 진행 중인 대량 report를
  전체 재조회하지 않는다; counter 이전 plan은 호환을 위해 기존 scan 계산을 유지한다.
  M1 React 화면은 `assessment_id` query parameter를 사용해 이 endpoint를 호출하고 결과를
  추가 페이지로 표시한다. 이 Coverage는 통제 수 자체가 아닌 **평가 실행률**이다.
  Frontend는 `VITE_API_BASE_URL`(운영 API origin) 또는 개발용 `VITE_API_PROXY_TARGET`을
  설정하고 Cognito access token을 `Authorization: Bearer <token>`으로 보낸다.
- M1 sandbox SPA는 Cognito Hosted UI authorization-code + PKCE 로그인으로 access token을
  얻는다. `VITE_COGNITO_DOMAIN`, `VITE_COGNITO_CLIENT_ID`, `VITE_COGNITO_REDIRECT_URI`는
  stack output으로 설정하며, token·password는 build artifact나 저장소에 넣지 않는다.
- 같은 응답의 `findings`는 `FAIL`, `MANUAL_REVIEW`, `INSUFFICIENT_EVIDENCE` 결과에서 C가
  결정적으로 만든 actionable projection이다. `readiness_score`는 전체 평가 계획이 완료되기
  전에는 `null`이고, 완료 후에는 `{score, evaluated_evaluations}`를 반환한다. 점수는 severity
  가중 평가 score이며 Coverage와 혼동하지 않는다.
- 같은 응답의 `suppressions`는 이 페이지의 Finding 중 고객의 유효 예외로 덮인 것에 대한
  **조회 시점 표시 전용** 목록이다(ADR-0020 §6). 각 항목은
  `{finding_id, exception_id, reason, expires_at, ticket_reference}`이며 억제된 Finding만
  담는다 — 목록의 부재가 곧 "억제 아님"이다. Finding 자체에는 억제 필드를 넣지 않는다(예외는
  만료되므로 저장하면 과거 사실이 왜곡된다). 억제 판정은 조치 판정과 같은 술어를 공유하고,
  `evaluated_at` provenance가 없는 옛 Finding은 억제하지 않는다. 만료는 조회 시각 기준이므로
  같은 Assessment라도 조회 시점에 따라 억제 표시가 사라질 수 있다.
- Initial Assessment 한 건은 같은 Resource × Rule에 대해 `IAC`, `AWS_ACTUAL`, `DRIFT` 결과를
  모두 반환한다. `IAC`와 `AWS_ACTUAL`은 각각 승인 commit의 Terraform 본문과 read-only AWS
  Actual을 근거로 AI가 판정하고, `DRIFT`는 그 두 판정의 불일치를 Code가 결정적으로 계산한다.
  따라서 Coverage 분모는 `Resource × Rule × Perspective`이며 세 관점을 모두 포함한다.
  `DRIFT` 결과는 `readiness_score` 계산에서 제외한다 — 정합 여부는 준수 수준이 아니므로,
  IaC와 Actual이 똑같이 위험한 리소스의 대표 점수를 올려서는 안 된다. Drift는 자체 결과와
  Finding으로 사용자에게 전달된다.
- IaC 변경이 필요한 Remediation 결과는 `IaCSnapshot`과 `RemediationPatch` Artifact
  reference를 반환한다. IaC가 이미 안전한 Actual Drift 동기화는 Patch 없이 IaC Snapshot의
  commit을 Plan 대상으로 사용한다. Artifact bytes 또는 공개 S3 URL은 반환하지 않는다.
- Deployment 승인 요청은 `commit_sha`와 `plan_hash`를 포함한다. Backend는 저장된
  `TerraformPlan`과 정확히 일치할 때만 승인 상태를 기록하며, Apply 직전에 다시 검증한다.
  `plan_hash`가 어떤 바이트의 digest인지(`resource_changes[]`를 허용 목록으로 투영한 canonical
  JSON), Terraform state `lineage`·`serial`을 함께 재검증한다는 점, apply 대상 commit이 default
  branch의 merge commit이라는 점은 ADR-0019에서 `Accepted`로 확정됐다. 구현은 이 정의를 따른다.

세부 wire shape와 runtime validation은 `packages/contracts/`가 정본이고 M0 예시는
`fixtures/m0/`에 둔다.

## M0 A implementation ownership

`POST /assessments`는 Backend가 Job을 생성하고 `202`와 public `JobResponse`를 반환한다.
Workflow만 Job의 `status`, `current_step`, 연결된 domain ID를 변경할 수 있으며 모든
변경은 현재 revision을 조건으로 한다. `GET /jobs/{jobId}`는 JWT-derived customer key의
base-table read 뒤 Job owner/administrator authorization을 적용한다. GSI1로 ID를 먼저
찾아 authorization을 우회해서는 안 된다.

명시적 Assessment·Remediation·Deployment 요청은 대응 Workflow를 직접 시작한다. 자연어
요청의 Parent는 30초 안에 Policy Q&A 응답 또는 실행 제안만 반환한다. Job을 만들거나
실행을 시작하는 것은 사용자 확인 뒤의 Backend API뿐이다. GitHub Actions의 Plan/Apply 완료는
OIDC EventBridge Event를 통해 Deployment Worker를 재개하며, Client callback은 사용하지 않는다.

## M2 A/C remediation API

`POST /findings/{findingId}/remediations`는 body를 받지 않는다. Backend가 JWT customer scope에서
C `RemediationContext`, A `RemediationTarget`, 고객 예외를 읽고 B
`RemediationPolicy.decide()`를 호출한다. Client는 Finding, action, customer, Job lifecycle,
revision을 지정할 수 없다.

응답은 `RemediationStartResponse`다.

- `TERRAFORM_PATCH`/`ACTUAL_SYNC`: `202`, `{decision, job}`. A가 decision/context/Job/Outbox/audit를
  원자 저장하고 C Remediation Queue로 dispatch한다.
- `MANUAL_REVIEW`/`SUPPRESSED`: `200`, `{decision, "job": null}`. decision/audit만 저장하고
  Job/Outbox는 만들지 않는다.

`POST /remediation-exceptions`는 Admin 전용이다. body allow-list는 `rule_id`, `rule_version`,
선택적 `resource_id`, enum `reason`, 필수 `expires_at`, 선택적 `ticket_reference`다. Backend가
`customer_id`, `exception_id`, `approved_by`, `approved_at`을 발급하고 immutable exception과 audit를
같이 기록한다. 자유 문장 reason이나 만료 없는 예외는 허용하지 않는다.

`POST /deployments/{deploymentId}/approve`도 injected service가 있을 때 handler에 노출되며 exact
`commit_sha`/`plan_hash` binding과 Admin RBAC를 강제한다. D live Patch/Sync/GitHub/Terraform
adapter와 customer Lambda runtime composition은 아직 연결 대상이다. 현재 A/C API·repository·Worker
경계는 mock/fixture로 통합 가능하지만 외부 실행이 live라고 주장하지 않는다.

## M3 approved-apply and verification endpoints

ADR-0020 비교 Contract와 ADR-0019의 Deployment 생성/Apply 경계는 모두 `Accepted`다.
`POST /remediations/{id}/deployments`, `POST /deployments/{id}/reject`, `GET /deployments/{id}`,
`GET /deployments/{id}/verification`은 durable 저장으로 완결 배선됐다.
`POST /deployments/{id}/approve`도 배선됐다 — D가 plan 요약(`refreshed`, `mapped_resource_ids`,
destructive 여부)을 plan facts와 함께 저장하므로(ADR-0019 §1-a) 승인 plan reader가 저장된 plan과
C의 readiness 판정을 함께 돌려준다. `GET /deployments/{id}`의 readiness도 같은 reader의 같은 판정을
쓰므로 "승인 대기" 표시와 실제 승인 가능 여부가 어긋나지 않는다.
`GET /audit-events`는 구현·배선됐다(아래 "Admin 감사 이력 조회" 참조).

| Method | Path | 상태 | Purpose |
| --- | --- | --- | --- |
| `POST` | `/remediations/{remediationId}/deployments` | 배선됨 | 승인된 IaC commit으로 Deployment를 만들고 `RUN_DEPLOYMENT`를 발행 |
| `GET` | `/deployments/{deploymentId}` | 배선됨 | plan 요약, readiness 사유, 승인 상태, apply/검증 진행 상태 조회 |
| `GET` | `/deployments/{deploymentId}/verification` | 배선됨 | Post-Deploy Verification의 before/after 비교 projection 조회. 검증 Assessment는 D Deployment Worker가 apply run을 승인 사실과 대조해 확정한 직후 A 경계(`PostDeployVerificationService`)가 자동 생성·발행하며, `GET /deployments/{id}`의 `verification_assessment_id`가 그 시점에 채워진다 |
| `POST` | `/deployments/{deploymentId}/reject` | 배선됨 | Admin 전용 배포 거절, Job `CANCELLED` 전이 |
| `POST` | `/deployments/{deploymentId}/approve` | 배선됨 | 저장된 plan과 파생 readiness로 승인. 요청의 `commit_sha`/`plan_hash`가 저장된 plan과 다르면 거절 |
| `GET` | `/deployments/{deploymentId}/observability` | 배선됨(live source 대기) | Admin 전용 데모 폐루프 관측·비용 기록 조회 (ADR-0021 §3). live metric source가 주입되지 않은 배포에서는 route가 없다(404) |
| `GET` | `/audit-events` | 배선됨 | Admin 전용 감사 이력 조회 |

- `deployment_id`는 Backend가 발급한다. Client는 Deployment를 만들 때 ID, 상태, commit, plan을
  지정하지 않는다. A는 저장된 `RemediationDecision`이 actionable인지, C Worker 결과가 있는지,
  `TERRAFORM_PATCH`의 대상 commit이 default branch에서 도달 가능한지를 확인한 뒤에만 Deployment를
  만든다. 하나라도 어긋나면 Deployment를 만들지 않는다. 고객 repository CI의 성공 여부는 별도로
  검사하지 않는다 — CI가 실패하면 PR이 merge되지 않고, 그러면 도달 가능성 검사에서 걸린다
  (ADR-0019).
- 승인 화면이 `commit_sha`와 `plan_hash`를 보내려면 먼저 그 값을 읽어야 하므로
  `GET /deployments/{deploymentId}`가 승인 요청의 선행 호출이다. 응답은 plan 요약과 hash를
  포함하지만 plan artifact bytes나 공개 S3 URL은 포함하지 않는다.
- `POST /deployments/{deploymentId}/reject`는 Admin 전용이고 body allow-list는 enum `reason`과
  선택적 `ticket_reference`다. 자유 문장 사유는 허용하지 않는다. 거절은 Deployment를 terminal
  `REJECTED`로, Job을 `CANCELLED`로 전이시키고 audit event를 같은 transaction에 쓴다. 같은
  `plan_hash`의 재승인은 허용하지 않으며 재시도는 새 plan과 새 Deployment로만 한다.
- 승인은 apply를 트리거하지 않는다. A는 승인 record와 dispatch outbox만 쓰고, D Deployment Worker가
  승인·`commit_sha`·`plan_hash`·Terraform state `lineage`·`serial`을 재검증한 뒤 GitHub Actions를
  dispatch한다.
- Plan/Apply 완료는 OIDC EventBridge Event로 Deployment Worker를 재개하며 Client callback은 사용하지
  않는다. Event 값은 신뢰 대상이 아니라 신호이고, D가 `run_id`로 Actions run을 다시 읽어 workflow,
  repository, `ref`, conclusion, plan artifact digest를 대조한다.
- `GET /deployments/{deploymentId}/verification` 응답은 Finding Resolution
  (`RESOLVED`/`UNRESOLVED`/`REGRESSED`/`INDETERMINATE`/`NO_LONGER_APPLICABLE`)과 점수·Coverage
  비교를 포함한다. 비교는 두 `readiness_score`가 모두 non-null이고 planned 평가 집합과
  `model_profile_id`/`rubric_version`이 동일할 때만 delta를 반환하며, 그렇지 않으면
  `comparable: false`와 이유 코드를 반환한다 (ADR-0020). 이 비교는 순수 projection이라 고객 예외를
  join하지 않는다 — 예외의 조회 시점 억제 표시는 `GET /assessments/{assessmentId}`의 `suppressions`
  소관이며, 억제는 재평가나 비교의 계획·Coverage·Readiness에 영향을 주지 않는다(ADR-0020 §6).
- 검증 결과는 원 Assessment를 덮어쓰지 않는다. Post-Deploy Verification은 `phase`,
  `source_assessment_id`, `deployment_id`를 가진 **새 `assessment_id`**로 조회된다.

경로와 wire shape는 이 A endpoint 구현에서 확정됐다.

비교에 쓰이는 `model_profile_id`/`rubric_version`은 두 Assessment의 **결과에서 파생한다**. Initial
Assessment는 그 pin을 item에 저장하지 않으므로(pin은 검증 Assessment 전용 — ADR-0020 §3) 원본 쪽은
파생 말고는 근거가 없고, 양쪽을 같은 방법으로 읽어야 비교 축이 한 종류가 된다. 한 Assessment의
결과가 서로 다른 Profile/rubric을 섞고 있으면 비교 이전에 fail-closed한다.

### Admin 감사 이력 조회 (`GET /audit-events`)

- Admin 전용(`READ_AUDIT_EVENTS`)이며 범위는 항상 호출자의 verified `custom:customer_id`다. 조회
  대상 고객을 query로 지정할 수 없다 — 지정 가능한 순간 이 endpoint가 전체 tenant 이력을 읽는
  유일한 경로가 된다.
- Query: `limit`(1–100, 기본 25), `cursor`(불투명 페이지 토큰), `event_type`(`AuditEventType` 값).
  알 수 없는 `event_type`은 빈 페이지가 아니라 `400`이다. 빈 페이지는 "그런 일이 없었다"로
  읽히므로 오탈자를 성공으로 표시하지 않는다.
- 응답은 `occurred_at` 역순(최신 우선)이며 `{ "events": [...], "next_cursor": string|null }`이다.
  각 event는 `event_id`, `event_type`, `occurred_at`, `customer_id`와 writer별 payload를 담은
  `details`를 가진다. `details`에는 DynamoDB key·GSI·`entity_type`·`version` 같은 저장 bookkeeping이
  들어가지 않는다.
- `event_type` 필터는 key 조건이 아니라 읽은 페이지에 적용되는 filter다. 필터가 걸린 페이지는
  `limit`보다 짧거나 비어 있으면서도 `next_cursor`를 가질 수 있으므로, Client는 짧은 페이지를
  끝으로 보지 말고 `next_cursor`를 따라가야 한다.
- `cursor`는 Client가 되돌려주는 값이므로 Backend가 호출자의 customer scope에 속하는지 검증하고,
  벗어나면 `400`으로 거절한다.

## Error envelope

```json
{
  "error": {
    "code": "SCOPE_DENIED",
    "message": "The requested resource is outside the approved scope"
  }
}
```

`packages/contracts.ApiErrorResponse`가 error envelope의 실행 가능한 정본이다. 초기 오류 코드:
`UNAUTHORIZED`, `SCOPE_DENIED`, `VALIDATION_ERROR`, `NOT_FOUND`, `CONFLICT`,
`EXECUTION_ERROR`.
