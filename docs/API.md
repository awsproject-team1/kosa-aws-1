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
| `POST` | `/findings/{findingId}/remediations` | Terraform Remediation 생성 |
| `POST` | `/deployments/{deploymentId}/approve` | 승인된 commit/plan으로 배포 승인 |
| `POST` | `/deployments/{deploymentId}/reject` | 배포 거절 |

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
- 결과 목록은 `limit`(1–100)과 opaque `cursor`로 페이지네이션한다. `coverage`는 페이지와
  무관하게 전체 Assessment 결과를 기준으로 계산하며, `next_cursor`가 `null`이면 마지막 페이지다.
  M1 React 화면은 `assessment_id` query parameter를 사용해 이 endpoint를 호출하고 결과를
  추가 페이지로 표시한다. 이 Coverage는 통제 수 자체가 아닌 **평가 실행률**이다.
  Frontend는 `VITE_API_BASE_URL`(운영 API origin) 또는 개발용 `VITE_API_PROXY_TARGET`을
  설정하고 Cognito access token을 `Authorization: Bearer <token>`으로 보낸다.
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
