# ADR-0019: 승인 배포 실행 경계 (plan/apply 재현성과 트리거 소유권)

> **상태: Proposed (2026-09-02)** — 팀 합의 전이다. 이 ADR이 `Accepted`가 되기 전에는 D가
> live plan/apply 경로를, A가 Deployment 생성·승인 후속 전이를 구현하지 않는다.
>
> **결정 대상:** `plan_hash`가 무엇의 digest인지, Terraform state를 누가 어떻게 보유하는지,
> apply 대상 commit이 무엇인지, `deployment_id`를 누가 발급하는지, apply를 누가 트리거하는지,
> 고객 repository의 workflow 파일을 누가 소유하는지, Plan/Apply 완료 Event를 어디까지 믿는지.
>
> **관련:** ADR-0007, ADR-0013, ADR-0014, ADR-0017, ADR-0018

## Context

ADR-0007은 "Apply는 Human Approval 뒤 GitHub Actions OIDC로만 실행하고 승인된 `commit_sha`와
`plan_hash`를 apply 직전 재검증한다"를 정했다. M3를 시작하려는 지금, 이 문장을 구현으로 옮길 수
없는 공백이 남아 있다.

1. **`plan_hash`의 대상이 정의되지 않았다.** `packages/contracts/deployments.py`의
   `TerraformPlan`은 `plan_hash == artifact.content_sha256`만 강제하고, 그 artifact가 binary
   plan인지 `terraform show -json` 출력인지 규정하지 않는다. D가 생성하고 A가 승인 시 대조하고
   C가 readiness에 바인딩하는 값이므로, 계산 방식이 다르면 재검증이 상시 실패한다.
2. **Terraform state의 소유·잠금 전략이 문서에 없다.** `docs/` 어디에도 state backend나 lock
   table 언급이 없다. plan과 apply 사이에 state가 바뀌면 `plan_hash`는 통과하면서 실제 적용
   결과가 달라지므로, hash 재검증만으로는 ADR-0007의 의도를 만족하지 못한다.
3. **apply 대상 commit이 PR head인지 merge commit인지 정해지지 않았다.** PR head를 apply하면
   default branch는 위반 상태로 남아 Post-Deploy Verification이 다시 `FAIL`을 낸다.
4. **Deployment 생성 진입점이 없다.** `TerraformPlan`은 `deployment_id`를 필수로 요구하지만
   `docs/API.md`에는 `/deployments/{deploymentId}/approve|reject`만 있다. D는 plan을 만들 때
   ID를 어디서 얻는지 알 수 없다.
5. **apply 트리거 주체가 없다.** 승인 저장과 동시에 A가 트리거하면 재시도가 이중 apply가 된다.
6. **고객 repository의 plan/apply workflow 파일 소유자가 없다.** `ci/terraform/`은 비어 있고
   GitHub App의 권한 범위도 문서화되지 않았다.
7. **Plan/Apply 완료 Event의 payload와 위조 방어가 없다.** `WorkflowCommand`에
   `PLAN_COMPLETED`/`APPLY_COMPLETED`는 있으나 Event가 무엇을 담고 누가 무엇을 검증하는지 없다.
8. **Deployment 상태 기계가 없다.** `DeploymentStatus`가 Contract에 없고 `docs/DATABASE.md`는
   Deployment item을 "Plan, approval, apply, verification state"로만 서술한다. `/reject`는
   endpoint 표에 한 줄 있을 뿐 권한·body·결과가 없다.

## Decision

### 1. Plan artifact와 `plan_hash`

- 승인 대상 plan artifact는 `terraform show -json <saved plan>` 출력을 **canonical JSON**으로
  정규화한 바이트다. 정규화 규칙: UTF-8, key 정렬, 구분자 `(",", ":")`, 비-ASCII escape,
  trailing newline 없음, 실행마다 변하는 `timestamp` 필드 제외.
- `plan_hash`는 그 바이트의 SHA-256이며 `TerraformPlan.artifact.content_sha256`과 같다
  (현재 Contract가 이미 강제한다).
- `prior_state`는 정규화에서 제외하지 않는다. refresh된 실제 상태가 hash에 반영되는 것이
  "refreshed plan"의 의미이며, 그래야 plan 이후의 Actual 변화가 재검증에서 드러난다.
- binary saved plan은 `TERRAFORM_PLAN_BINARY` artifact로 따로 보관하고 hash 대상이 아니다.
  apply는 이 binary를 사용하지만 사람이 승인한 대상은 사람이 읽을 수 있는 JSON plan이다.
- 고객 repository는 `.terraform.lock.hcl`을 커밋해야 하고 workflow는 Terraform version을
  고정한다. Provider 버전이 흔들리면 같은 commit에서 다른 plan이 나와 재현성이 깨진다.

### 2. Terraform state backend와 lock

- 고객 관리자가 실행하는 bootstrap stack이 state 저장소를 만든다: versioned·encrypted·
  TLS-only·bucket-owner-enforced S3 bucket과 DynamoDB lock table. Platform은 이 두 리소스에
  대한 권한만 갖고 고객 workload write 권한을 얻지 않는다.
- state key는 `(repository_id, workspace)`로 분리한다. 여러 Repository가 한 state를 공유하면
  한 Deployment의 apply가 다른 Repository의 리소스를 계획 밖에서 바꿀 수 있다.
- plan job은 plan 시점의 **state serial**을 Deployment record에 기록한다. apply job은 dispatch
  직전과 실행 시작 시 serial 일치를 재검증하고, 다르면 apply하지 않고 `MANUAL_REVIEW`로 보낸다.
  `plan_hash` 일치는 "같은 plan"을 보장하지만 "같은 state"를 보장하지 않는다.
- apply는 `terraform apply -input=false <saved plan>`으로 saved plan만 적용한다. apply 시점
  재계산(`terraform apply` 단독 실행)은 금지한다. 승인 대상과 적용 대상이 달라진다.

### 3. Apply 대상 commit은 default branch의 merge commit

승인·apply 흐름을 다음으로 고정한다.

```text
Finding → RemediationDecision → Patch → branch/commit/PR → 고객 CI
→ 사람이 PR merge → merge commit에서 refreshed plan → Human Approval → 그 merge commit apply
→ Post-Deploy Verification
```

- PR head commit에서 만든 plan은 CI 참고용이며 승인 대상이 아니다. artifact로 보관하지 않고
  `plan_hash`를 발급하지 않는다.
- `ACTUAL_SYNC`는 새 Patch를 만들지 않으므로 PR/merge 단계 없이 현재 default branch commit을
  대상으로 삼는다. 이때도 Deployment는 default branch commit에 바인딩된다.
- 이유: apply 결과가 default branch 상태와 일치해야 이후 `DRIFT` 관점이 거짓 이탈을 내지 않고,
  Post-Deploy Verification의 IaC 관점이 사람이 merge한 코드와 같은 것을 읽는다.

### 4. `deployment_id` 발급과 Deployment 생성

- 발급자는 A다. Client는 `deployment_id`를 만들 수 없다 (`docs/DATABASE.md`의 ID 발급 규칙).
- 진입점은 `POST /remediations/{remediationId}/deployments`이며 다음을 fail-closed로 검증한 뒤
  `DEPLOYMENT`, `JOB`, `OUTBOX`(`RUN_DEPLOYMENT`), `DEPLOYMENT_REQUESTED` audit를 하나의 조건부
  transaction으로 쓴다.

| 전제조건 | 실패 시 |
| --- | --- |
| 저장된 `RemediationDecision`이 `TERRAFORM_PATCH` 또는 `ACTUAL_SYNC` | `CONFLICT`, Deployment 생성 없음 |
| C Remediation Worker 결과가 존재 (patch 또는 sync target) | `CONFLICT` |
| `TERRAFORM_PATCH`는 PR이 merge되어 대상 commit이 default branch에 있음 | `CONFLICT` |
| 고객 repository CI가 그 commit에서 성공 | `CONFLICT` (아래 8번) |
| JWT customer scope와 승인 Repository 일치 | `SCOPE_DENIED` |

- D Deployment Worker가 `RUN_DEPLOYMENT`를 소비해 plan을 실행한다. Queue payload는 ADR-0013대로
  `job_id`, `expected_revision`, `command`만 담는다.

### 5. Apply 트리거는 A가 아니라 D가 한다

- A의 `POST /deployments/{deploymentId}/approve`는 승인 record와 audit만 쓰고 GitHub를 호출하지
  않는다. 현재 `DynamoDbDeploymentApprovalRepository`가 `approval-{deployment_id}` 결정적 key로
  단일 승인을 보장하므로 이 성질을 그대로 apply idempotency의 기반으로 쓴다.
- 승인 transaction은 apply dispatch용 Outbox를 함께 쓴다. D Deployment Worker가 이를 소비해
  다음을 재검증한 뒤 `workflow_dispatch`를 호출한다: 저장된 approval, `commit_sha`,
  `plan_hash`, state serial, Deployment status.
- dispatch 전에 `APPROVED → APPLYING` 조건부 전이로 실행 소유권을 얻는다. 이미 `APPLYING`
  이상이면 새 run을 만들지 않는다. at-least-once 전달(ADR-0013)에서 이중 apply를 막는 지점은
  Queue가 아니라 이 조건부 전이다.
- `workflow_dispatch` input은 `deployment_id`, `commit_sha`, `plan_hash`이며 workflow는 이
  값으로 자신이 적용할 plan artifact를 조회·검증한다.

### 6. 고객 repository workflow 소유권과 GitHub App 권한

- plan/apply workflow는 저장소 `ci/terraform/`의 template으로 제공하고 **고객 관리자가 1회
  수동 설치**한다. Platform은 workflow 파일을 만들거나 수정하지 않는다.
- GitHub App은 `contents: write`(branch/commit)와 `pull_requests: write`만 요청하고
  `workflows: write`는 요청하지 않는다. App이 workflow를 쓸 수 있으면 승인 경계를 우회해 고객
  계정에서 임의 코드를 실행할 수 있으므로, 이것은 편의가 아니라 권한 경계 문제다.
- apply job은 protected Environment와 required reviewers를 2차 게이트로 둔다. OIDC trust는
  exact repository와 exact environment subject로 제한하고, plan job은 `TerraformPlanRole`,
  apply job은 `TerraformDeploymentRole`을 사용한다 (ADR-0007의 Role 분리 유지).

### 7. Plan/Apply 완료 Event는 신호이고 정본이 아니다

- GitHub Actions는 OIDC로 EventBridge에 `deployment_id`, `commit_sha`, `plan_hash`, `run_id`,
  `conclusion`만 게시한다. 정책 원문·IaC 본문·plan 본문은 Event에 담지 않는다.
- EventBridge는 `deployment_id`로 A partition의 Job을 찾아
  `WorkflowTask(job_id, expected_revision, PLAN_COMPLETED | APPLY_COMPLETED)`만 Deployment Queue에
  넣는다. Event detail 자체는 `DEPLOYMENT#{deployment_id}#EVENT#{run_id}` item에 조건부로
  저장한다. Queue 최소 payload 원칙(ADR-0013)을 유지하면서 event 중복을 key로 흡수한다.
- D Worker는 Event 값을 신뢰하지 않는다. `run_id`로 Actions run을 다시 읽어 workflow path
  allow-list, repository, `ref`가 승인 commit인지, conclusion, plan artifact digest를 대조한다.
  하나라도 다르면 재시도하지 않고 `MANUAL_REVIEW`로 보낸다.
- GitHub Actions에 DynamoDB write 권한을 주지 않는다. 외부 CI가 상태 정본을 직접 쓰면 승인
  경계 밖에서 Deployment 상태를 바꿀 수 있다.

### 8. Deployment 상태 기계, reject, CI gate, apply 실패

- `DeploymentStatus` 전이표를 A가 소유하는 순수 함수 + 조건부 write로 구현한다.

```text
PLAN_REQUESTED → PLAN_COMPLETED → READINESS_EVALUATED → WAITING_APPROVAL
→ APPROVED → APPLYING → APPLIED → VERIFYING → VERIFIED
```

| 분기 | 전이 | 의미 |
| --- | --- | --- |
| readiness `BLOCKED` | `READINESS_EVALUATED → BLOCKED` | 새 plan 또는 재수정이 필요하다 |
| readiness `MANUAL_REVIEW` | `READINESS_EVALUATED → MANUAL_REVIEW` | 사람 판단 없이 승인 대기로 가지 않는다 |
| 사람 거절 | `WAITING_APPROVAL → REJECTED` | terminal |
| apply 실패·불명확 | `APPLYING → MANUAL_REVIEW` | 자동 재시도하지 않는다 |
| 검증 불명확 | `VERIFYING → VERIFICATION_INDETERMINATE` | 위반 판정이 아니라 사람 확인 대상 |

- `POST /deployments/{deploymentId}/reject`는 Admin 전용이고 body는 enum `reason`(자유 문장
  금지, remediation 예외와 같은 원칙)과 선택적 `ticket_reference`만 받는다. Deployment는
  terminal `REJECTED`, Job은 `CANCELLED`, `DEPLOYMENT_REJECTED` audit를 같은 transaction에 쓴다.
  같은 `plan_hash`의 재승인은 허용하지 않는다. 다시 하려면 새 plan과 새 Deployment가 필요하다.
- 고객 repository CI(`terraform fmt`/`validate`, TFLint, Checkov) 실패는 fail-closed다.
  Deployment를 만들지 않고 Job을 `FAILED`로 종료한다.
- apply 실패나 부분 적용은 자동 재시도하지 않는다. `MANUAL_RECONCILIATION_REQUIRED` audit와
  Deployment `MANUAL_REVIEW`로 남기고, 재시도는 새 plan·새 Deployment·새 승인으로만 한다
  (`docs/DESIGN.md`의 Apply 무재시도 규칙을 상태 값으로 구체화한 것이다).

## Consequences

- `plan_hash`가 사람이 읽을 수 있는 JSON plan의 digest이므로 승인 화면에 보여준 것과 검증
  대상이 같다. binary plan은 적용 수단으로만 쓰인다.
- `plan_hash` + state serial + saved plan의 세 겹으로 plan-apply 사이의 변화가 fail-closed된다.
- apply 대상이 항상 default branch commit이므로 Post-Deploy Verification과 이후 `DRIFT` 판정이
  사람이 merge한 코드와 같은 것을 읽는다.
- A는 GitHub·Terraform을 호출하지 않고 D는 정책·승인 판정을 하지 않는다. ADR-0018의 역할 분리가
  Deployment 단계까지 이어진다.
- Event를 신뢰하지 않으므로 D Worker는 GitHub read 권한이 반드시 필요하다. Event만으로 상태를
  전이시키는 구현보다 호출이 한 번 늘어난다.
- 고객 관리자에게 workflow 수동 설치라는 1회 작업이 추가된다. 이것은 App에
  `workflows: write`를 주지 않기 위한 의도된 비용이다.

## Rejected alternatives

- **PR head commit을 apply 대상으로 사용:** apply 후에도 default branch가 위반 상태로 남아
  재평가가 `FAIL`을 내고 데모 폐루프가 성립하지 않으므로 거부한다.
- **binary plan의 digest를 `plan_hash`로 사용:** 사람이 승인 화면에서 읽은 내용과 hash 대상이
  달라지고, Terraform/Provider 버전에 따라 같은 계획이 다른 값을 내므로 거부한다.
- **A가 승인 직후 `workflow_dispatch`를 호출:** 승인 저장과 외부 호출이 한 transaction이 될 수
  없어 재시도가 이중 apply가 되고, A가 GitHub 실행 경계를 갖게 되므로 거부한다.
- **완료 Event의 `plan_hash`/`conclusion`을 그대로 신뢰:** EventBridge에 게시할 수 있는 주체가
  상태를 위조할 수 있으므로 거부한다.
- **GitHub App에 `workflows: write` 부여:** 승인 경계를 우회한 임의 코드 실행 경로가 생기므로
  거부한다.
- **plan 없이 apply 시점에 재계산:** 승인 대상과 적용 대상이 달라지므로 거부한다.

## Open decision

- **Owner:** D(plan/apply 실행, workflow template) + A(Deployment 상태·API) + Security(state
  backend·OIDC·Environment 승인)
- **Needed by:** M3 착수 전. 이 결정 없이 D가 live 경로를 구현하면 A/C가 사후에 그 구현을
  따라가야 한다.
- **Blocks:** M3 A(Deployment/Approval 상태 전이, 결과 조회 API), M3 C(Deployment Readiness의
  plan 입력), M3 D(plan/apply/Event 처리), M3 Shared(승인 없는 Write 방지 E2E),
  `DeploymentStatus`/`AuditAction` Contract 추가.
- **Proposed options:** 위 Decision 8개 항목. 각 항목의 대안과 거부 이유는 Rejected alternatives에
  있다.
- **Final record:** 미정. 합의 시 이 ADR의 상태를 `Accepted`로 바꾸고 `docs/API.md`,
  `docs/CONTRACTS.md`, `docs/DATABASE.md`, `docs/DESIGN.md`의 계획 표기를 같은 PR에서 구현
  표기로 옮긴다.
