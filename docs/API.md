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
| `POST` | `/findings/{findingId}/remediations` | B policy 판정 후 Remediation 시작 또는 non-action decision 보고 |
| `POST` | `/remediation-exceptions` | Admin이 만료 필수 고객 예외를 승인·등록 |
| `POST` | `/deployments/{deploymentId}/approve` | 승인된 commit/plan으로 배포 승인 |
| `POST` | `/deployments/{deploymentId}/reject` | 배포 거절 |

## Planned customer policy ingestion endpoints

아래 endpoint는 아직 노출되지 않았다. B의 정규화·승인·게시 Contract와 판정은
`apps/backend/policy/ingestion/`에 구현됐고(`normalize_upload`, `approve_source`,
`publish_profile`), A의 API/Storage 배선과 C의 AI 추출 품질 Gate가 남아 있다. 상세 workflow와
인수 조건은 `docs/POLICY_INGESTION.md`를 따른다.

| Method | Planned path | Purpose |
| --- | --- | --- |
| `POST` | `/policy-sources/uploads` | JWT-derived customer Scope의 업로드 세션 생성 |
| `POST` | `/policy-sources/{sourceId}/versions/{version}/process` | 업로드 검증과 비동기 파싱·정규화 시작 |
| `GET` | `/policy-sources/{sourceId}/versions/{version}` | 처리 상태, 형식 지원 여부와 검토 경고 조회 |
| `POST` | `/policy-sources/{sourceId}/versions/{version}/approve` | 검토된 Source/Control/Rule version 승인 |
| `POST` | `/policy-profiles` | 승인된 Rule version으로 versioned Policy Profile 게시 |
| `POST` | `/policy-profiles/{profileId}/versions` | 승인된 Rule version으로 Profile 새 version 게시 |

업로드 세션 응답이 후속 호출에 필요한 `sourceId`와 `version`을 돌려준다. Client는 이 값을
그대로 사용하며 스스로 만들지 않는다.

승인과 Profile 게시는 서로 다른 operation이다. `/approve`는 Source/Control/Rule version을
확정하고, Profile 게시가 그 Rule들을 평가 경계로 만든다. 게시는 승인되지 않은 Source·Rule을
참조하거나 승인된 것과 다른 Source version을 가리키는 Profile을 거부한다. 두 단계를 하나의
operation으로 합치더라도 이 거부 조건과 audit record 기록은 동일하게 적용한다.

`/approve`는 `approve_source()`를, Profile 게시는 `publish_profile()`을 호출한다. 두 함수는
아무것도 영속화하지 않는 순수 판정이므로, A가 DynamoDB 조건부 write 앞에서 호출하고 거부
시에는 write를 시도하지 않는다. 거부 사유는 `ApprovalRejectionCode` 열거값이며 응답의 오류
코드로 그대로 쓸 수 있다 — 자유 문장이 아니라서 정책 원문이 응답이나 로그로 새지 않는다.

경로와 wire shape는 구현 PR의 Producer/Consumer Contract Review에서 최종 확정한다. Client는
`customer_id`, S3 bucket/key, checksum 판정, parser/status를 직접 지정할 수 없다. 업로드 성공은
정책 승인이나 Assessment 활성화를 의미하지 않는다.

## M0 boundary payloads

- Assessment 생성 요청은 승인된 `repository_id`, `policy_profile_id`를 지정한다. Resource/AWS
  Account Scope는 이후 Contract 확장 전까지 JWT claim과 승인된 Repository 설정에서 판정하며,
  현재 M0 요청 body에는 포함하지 않는다.
- Policy Profile 조회·평가는 `PolicyProfile.rule_references`로 version이 고정된 Rule만 사용하고,
  정책 Evidence는 `SourceReference.evidence_reference`의 `{source_id}#{locator}` 정규형으로
  반환한다. AWS Actual Evidence는 `aws:` namespace를 사용한다.
- Initial Assessment 결과는 같은 관리 대상의 `IAC`, `AWS_ACTUAL`, `DRIFT` 관점을
  구분해 반환한다. Drift는 Finding 근거일 뿐 API나 AI가 고객 워크로드를 직접 변경할
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
