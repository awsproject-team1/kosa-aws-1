# API Contract

## Conventions

- 인증: Cognito JWT. Backend가 Role과 Customer/Repository/AWS Account Scope를 검증한다.
- 장시간 작업: `202 Accepted`와 `job_id`를 반환한다.
- 모든 요청·응답은 버전 관리되는 `packages/contracts/` 스키마를 따른다.
- Client는 `customer_id`, Job ID, Job revision, status, timestamp, TTL 또는 DynamoDB key를
  요청 body에 보낼 수 없다. Backend가 verified JWT와 server state에서 이 값을 결정한다.

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

- Assessment 생성 요청은 승인된 `repository_id`, `policy_profile_id`와 Resource/AWS
  Account Scope를 지정한다. Backend는 요청 값이 아니라 JWT claim으로 Customer와 허용
  scope를 판정한다.
- Policy Profile 조회·평가는 `PolicyProfile.rule_ids`의 versioned Rule만 사용하고,
  `SourceReference`를 Evidence locator로 반환한다.
- Remediation 생성 결과는 `IaCSnapshot`과 `RemediationPatch` Artifact reference를
  반환한다. Artifact bytes 또는 공개 S3 URL은 반환하지 않는다.
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

## Error envelope

```json
{
  "code": "SCOPE_DENIED",
  "message": "The requested repository is outside the approved scope.",
  "request_id": "req_..."
}
```

초기 오류 코드: `UNAUTHORIZED`, `FORBIDDEN`, `SCOPE_DENIED`, `VALIDATION_ERROR`, `NOT_FOUND`, `CONFLICT`, `EXECUTION_ERROR`.
