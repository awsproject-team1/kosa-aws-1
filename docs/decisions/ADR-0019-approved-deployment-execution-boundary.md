# ADR-0019: 승인 배포 실행 경계 (plan/apply 재현성과 트리거 소유권)

> **상태: Accepted (2026-09-02)** — A·D·Security 세 Owner가 이 ADR을 담은 서명 PR의 리뷰
> approve로 서명했다. 이 상태 이후 D는 live plan/apply 경로를, A는 Deployment 생성·승인 후속
> 전이를 구현할 수 있다.
>
> **승인 방법:** 아래 Decision 1~8은 미정 항목 없이 모두 결정과 근거를 담고 있다. 별도 회의를
> 열지 않고 **이 PR에 대한 A·D·Security의 리뷰 approve를 서명으로 삼았다** (CONTRIBUTING:
> Issue/Project를 쓰지 않으므로 PR 스레드가 결정 기록이다). A·D는 Deployment/API와 plan/apply
> 경계를, Security는 state backend·OIDC·Environment 승인 경계를 확인했다. 세 Owner의 approve가
> 모여 같은 PR에서 상태를 `Accepted`로 바꾼다. 이 서명은 A(PR #40)와 D의 M3 조각이 병렬로
> base 삼도록 독립 PR로 분리했다.
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

- 승인 대상 plan artifact는 `terraform show -json <saved plan>` 출력을 **허용 목록으로 투영한**
  canonical JSON 바이트다. 투영 대상은 `resource_changes[]`이며 남기는 필드는 다음 열한 개다.
  `address`, `mode`, `type`, `name`, `index`, `provider_name`, `change.actions`, `change.before`,
  `change.after`, `change.after_unknown`, `change.replace_paths`.
- 정규화 규칙: `address` 기준 정렬, key 정렬, UTF-8, 구분자 `(",", ":")`, 비-ASCII escape,
  trailing newline 없음, NaN/Infinity 금지.
- `plan_hash`는 그 바이트의 SHA-256이며 `TerraformPlan.artifact.content_sha256`과 같다
  (현재 Contract가 이미 강제한다). **저장하는 `TERRAFORM_PLAN` artifact가 이 투영 바이트이므로**
  기존 불변식을 고치지 않아도 정의상 성립한다.
- **제외 목록이 아니라 허용 목록으로 정의하는 이유:** 제외 목록은 열린 집합이다. Terraform이나
  Provider가 출력 필드를 하나 늘리면 그 필드가 조용히 hash에 들어와 재현성이 깨지고, 깨진 사실은
  나중에 승인 재검증 실패로만 드러난다. 허용 목록은 닫힌 집합이고 Contract test로 고정된다.
  `timestamp`·`format_version`·`terraform_version`·`prior_state`는 투영에 없으므로 자동으로 빠진다.
- `prior_state`를 hash에서 빼도 plan 이후의 Actual 변화는 놓치지 않는다. 그 방어는 hash가 아니라
  아래 2번의 state `lineage`·`serial` 재검증과 saved plan 강제가 담당한다. `prior_state`를 넣으면
  refresh할 때마다 hash가 흔들려 승인 재검증이 상시 실패하는 쪽 비용이 더 크다.
- `show -json` **원본 전체**는 감사용 별도 artifact로 보관한다. 사람이 승인 화면에서 읽는 것은
  이 원본이고, hash 대상은 그 원본에서 결정적으로 투영한 바이트다.
- binary saved plan은 `TERRAFORM_PLAN_BINARY` artifact로 따로 보관하고 hash 대상이 아니다.
  apply는 이 binary를 사용한다. Terraform이 binary plan의 바이트 안정성을 보장하지 않으므로
  digest 대상으로 쓰지 않는다.
- 이 투영이 `PlanReadinessInput.has_destructive_changes`
  (`packages/contracts/remediation.py`)의 **유일한 산출 근거**다. `change.actions`에 `delete`가
  있거나 `change.replace_paths`가 비어 있지 않으면 `True`다. 게이트 자체는 이미 있고
  (`apps/backend/remediation/readiness.py`의 `DESTRUCTIVE_CHANGE_REQUIRES_MANUAL_REVIEW`)
  없는 것은 이 bool의 계산 규칙이었다.
- 투영 함수와 destructive 판정 함수는 `packages/contracts`에 두고 A의 승인 검증, C의 readiness
  바인딩, D의 apply 직전 재검증이 **같은 함수**를 호출한다. 역할별로 재구현하지 않는다.
- 고객 repository는 `.terraform.lock.hcl`을 커밋해야 하고 workflow는 Terraform version을
  고정한다. Provider 버전이 흔들리면 같은 commit에서 다른 plan이 나와 재현성이 깨진다.

#### 1-a. Plan 요약(`PlanSummary`)과 resource id 투영 — 2026-09-03 보완

`plan_hash`는 "이 plan이 무엇인가"를 고정하지만 C의 readiness 판정이 묻는 세 가지를 담지 않는다:
refresh 여부, 파괴적 변경 여부, **그 plan이 건드리는 AWS 리소스**. `PlanReadinessInput`은 D를
producer로 지목했지만 그 값의 산출 규칙도 저장 경로도 정해져 있지 않아, 승인 경로가 열리지 못하고
있었다. 여기서 셋 다 확정한다.

- **`PlanSummary`(`refreshed`, `has_destructive_changes`, `mapped_resource_ids`)를
  `PlanExecutionResult`에 싣고 plan facts와 함께 `DEPLOYMENT#{deployment_id}` item에 저장한다.**
  승인은 plan보다 나중 invocation에서 일어나므로 이 값들은 durable해야 한다. plan facts와 마찬가지로
  all-present-or-all-absent다.
- **`mapped_resource_ids`는 hash된 바로 그 canonical 투영에서 파생한다.** Terraform address와
  Finding의 `resource_id`는 서로 다른 어휘이므로 둘을 잇는 규칙이 필요하다. 규칙은 **resource type별
  identity 속성의 허용 목록**이며, 투영 자체와 같은 이유로 허용 목록이다 — provider가 identity처럼
  보이는 필드를 하나 늘렸다고 해서 "이 plan이 어느 리소스를 건드리는가"의 답이 조용히 바뀌면 안 된다.
  S3 범위에서는 모든 항목이 `bucket`이다(bucket 리소스는 이름을 `bucket` 인자로 받고, 하위 리소스는
  같은 속성으로 부모를 참조한다). 이 값이 곧 `AwsResourceQuery(resource_type="AWS::S3::Bucket")`의
  `resource_id`이자 `Finding.resource_id`다.
- **EC2/RDS/ALB 확장 (2026-09-03 보완).** type별 identity 속성은 그 type의
  `AwsResourceQuery.resource_id`와 같은 값을 담는 속성으로 정한다.
  `aws_instance` → `id`(`i-…` 인스턴스 id, 생성 중이면 computed이므로 건너뛴다 — 아직 존재하지 않는
  리소스에는 Finding도 없다), `aws_db_instance` → `identifier`(고객이 선언하는 값이라 plan에서 알 수
  있다. computed인 `arn`이 아니다), `aws_lb`/`aws_alb` → `arn`, `aws_lb_listener`/`aws_alb_listener`
  → `load_balancer_arn`. ALB만 ARN을 쓰는 이유는 리스너가 부모를 ARN으로만 지목할 수 있어서다.
  ARN을 투영하면 load balancer와 리스너가 한 어휘로 모이고, 그 값은 ELBv2 read adapter가 조회하는
  키와도 같다.
- **`aws_ebs_volume`/`aws_ebs_snapshot`/독립 `aws_security_group*`은 허용 목록에 넣지 않는다.**
  그 리소스들의 plan-side identity는 볼륨/스냅샷/보안 그룹 id인데, EC2 Finding은 인스턴스를 대상으로
  올라간다(Task 9 범위 결정: 인스턴스 read adapter가 연결된 볼륨·보안 그룹 상태를 함께 담는다).
  그 id를 투영하면 readiness가 묻는 것과 다른 질문에 답하게 된다. 그 리소스만 바꾸는 plan은
  readiness가 `BLOCKED`로 남는다 — fail-closed이며, 조용한 불일치가 아니라 문서화된 경계다.
- `after`를 먼저 읽고 `before`를 그다음에 읽는다. 삭제되는 리소스도 자기 id를 밝혀야 하기 때문이다.
  값이 없거나 계산값(`after_unknown`)이면 건너뛴다 — 알 수 없는 id는 "plan이 그 Finding을 건드린다"는
  근거가 아니다.
- **허용 목록 밖의 resource type은 아무것도 기여하지 않는다.** 그 결과 finding 리소스가 매핑되지
  않아 readiness가 `BLOCKED`가 되며, 이는 fail-closed 방향이다. 관련성을 확인하지 못한 plan을
  승인 가능으로 표시하지 않는다. resource type 추가는 그것을 필요로 하는 Rule과 같은 변경에서 한다.
- **`refreshed`는 D가 어느 workflow를 실행했는지에서 나온다.** 승인 대상 plan은 `ci/terraform/
  terraform-plan.yml`(refreshed saved plan을 만드는 승인 template)이 만든 것이며, apply 직전
  재검증이 run의 workflow path를 대조한다. terraform 플래그를 사후에 관측할 방법은 없으므로 근거는
  "어느 workflow가 돌았는가"다.
- **어댑터는 회수한 canonical 바이트로 `plan_hash`를 다시 계산해 대조한 뒤에만 요약을 만든다.**
  승인 게이트는 hash를 재검증하지 요약을 재검증하지 않으므로, 요약이 다른 plan을 설명하면 그 뒤로는
  아무도 잡을 수 없다.
- 투영 함수(`mapped_resource_ids`)는 `packages/contracts/terraform_plan.py`에 두고 A/C/D가 같은
  함수를 호출한다. `plan_hash`·destructive 판정과 같은 원칙이다.
- **readiness 판정 자체는 저장하지 않는다.** 저장된 사본은 입력이 바뀌면 조용히 낡고, 낡은
  `READY_FOR_APPROVAL`은 C가 막았을 plan을 승인 가능으로 보여준다. 승인 조회와 상태 조회는 같은
  reader가 같은 입력으로 판정한다.

### 2. Terraform state backend와 lock

- 고객 관리자가 실행하는 bootstrap stack이 state 저장소를 만든다: versioned·encrypted·
  TLS-only·bucket-owner-enforced S3 bucket과 DynamoDB lock table. Platform은 이 두 리소스에
  대한 권한만 갖고 고객 workload write 권한을 얻지 않는다.
- state key는 `(repository_id, workspace)`로 분리한다. 여러 Repository가 한 state를 공유하면
  한 Deployment의 apply가 다른 Repository의 리소스를 계획 밖에서 바꿀 수 있다. workspace 이름은
  `{customer_id}-{repository_id}`이며, 이름이 항상 유효하도록 **Repository 승인 시점에** 두 ID를
  `^[A-Za-z0-9_-]+$`로 검증한다. 배포 시점에 실패하지 않게 하려는 것이다.
- plan job은 plan 시점의 state **`lineage`와 `serial`을 함께** Deployment record에 기록한다.
  apply job은 dispatch 직전과 실행 시작 시 두 값이 **모두** 일치할 때만 실행하고, 다르면
  apply하지 않고 `MANUAL_REVIEW`로 보낸다. `plan_hash` 일치는 "같은 plan"을 보장하지만
  "같은 state"를 보장하지 않는다.
- **`serial` 단독 대조로는 부족하다.** state가 재생성되면 `lineage`가 새로 발급되고 `serial`은
  낮은 값으로 초기화되므로, 전혀 다른 state가 우연히 같은 `serial`을 갖고 통과할 수 있다.
  두 값을 쌍으로 대조해야 이 경우가 걸린다.
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
- `ACTUAL_SYNC`는 새 Patch를 만들지 않으므로 PR/merge 단계가 없다. 대상은
  `RemediationSyncTarget.commit_sha` — 이미 `IAC` 관점을 통과한 현재 default branch commit이며,
  Deployment는 그 commit에 바인딩된다.
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
| `TERRAFORM_PATCH`는 대상 commit이 **default branch에서 도달 가능**함 | `CONFLICT` |
| JWT customer scope와 승인 Repository 일치 | `SCOPE_DENIED` |

- D Deployment Worker가 `RUN_DEPLOYMENT`를 소비해 plan을 실행한다. Queue payload는 ADR-0013대로
  `job_id`, `expected_revision`, `command`만 담는다.
- **고객 repository의 CI 성공 여부를 우리 상태로 관측하지 않는다.** 고객 CI가 실패하면 PR이
  merge되지 않고, merge되지 않으면 위 도달 가능성 검사에서 걸린다. 검사할 사실이 하나로 줄고
  고객의 merge 판단을 우리 상태 기계로 끌고 들어오지 않는다.
- Remediation Job은 `CREATE_PR`에서 정상 `COMPLETED`로 끝난다. 고객의 merge를 기다리려고 Job을
  열어두거나 `FAILED`로 만들지 않는다.
- `JobCurrentStep.CI_VALIDATION`은 고객 CI가 아니라 plan workflow 안에서 도는 platform 측
  `fmt`/`validate` 단계를 뜻한다. 두 개념에 같은 값을 쓰지 않는다.
- `Action` enum에 `START_DEPLOYMENT`를 추가하고 `_USER_ACTIONS`에 넣는다.

### 5. Apply 트리거는 A가 아니라 D가 한다

- A의 `POST /deployments/{deploymentId}/approve`는 승인 record와 audit만 쓰고 GitHub를 호출하지
  않는다. 현재 `DynamoDbDeploymentApprovalRepository`가 `approval-{deployment_id}` 결정적 key로
  단일 승인을 보장하므로 이 성질을 그대로 apply idempotency의 기반으로 쓴다.
- 승인 transaction은 apply dispatch용 Outbox를 함께 쓴다. D Deployment Worker가 이를 소비해
  다음을 재검증한 뒤 `workflow_dispatch`를 호출한다: 저장된 approval, `commit_sha`,
  `plan_hash`, state `lineage`·`serial`, Deployment status.
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
- **A/D 구현 경계(공유 계약, 상세는 `docs/DATABASE.md` "완료 Event 경계"):** `#EVENT#{run_id}` item은
  `status`로 두 단계를 갖는다. A/EventBridge가 완료 Event 수신 시 `run_id`만 담아
  `PENDING_VERIFICATION`으로 예약 write하고(재조회할 좌표 포인터일 뿐 사실 기록이 아니다), D Worker가
  그 좌표로 run을 재조회·대조한 뒤 검증된 `WorkflowRunFacts`로 같은 item을 `VERIFIED`로 확정한다.
  예약 item이 없으면 D는 `APPLY_COMPLETED`를 fail-closed한다. A PR과 D PR이 이 계약을 각자 구현하되
  같은 문장을 인용한다.

### 8. Deployment 상태 기계, reject, apply 실패

`DeploymentStatus`는 `packages/contracts`의 enum으로 추가하되 **DynamoDB에 저장하지 않는다.**
API 응답 shape을 위한 표현 타입이고, 값은 A가 소유하는 순수 함수 `derive_deployment_status(...)`가
**이미 durable한 사실들**에서 read 시 계산한다: `JobStatus`, `JobCurrentStep`, approval record,
rejection record와 그 `reason`, apply run reference와 conclusion, verification assessment 결과.

**저장하지 않는 이유.** `WAITING_APPROVAL`은 `JobStatus`에 이미 있고 `APPLYING`은
`JobStatus.RUNNING`과 겹친다. `JobCurrentStep`에는 `PRE_DEPLOY_VALIDATION`·`TERRAFORM_PLAN`·
`APPLY`·`POST_DEPLOY_VERIFICATION`이 이미 있다. 배포 생애주기 위치는 이미 저장돼 있으므로 새 enum을
저장하면 같은 사실의 두 번째 사본이 되고, 두 사본이 어긋날 때 어느 쪽이 이기는지를 규칙으로 또
만들어야 한다. 이 저장소는 같은 트레이드오프를 Readiness Score에서 이미 한 번 판단했다 —
"report read 시 결정적으로 계산한다 … 진행 중 Assessment에 오래된 점수가 남지 않는다"
(`docs/DATABASE.md`). 파생으로 두면 조건부 write 조정도 마이그레이션도 없다.

**게이트 판정은 파생값을 읽지 않는다.** 승인 여부와 apply 허용 여부는 5·7번대로 사실을 직접
재조회해 판단한다. 파생값은 화면과 API 응답 전용이다. 이중 apply를 막는 정본은 아래 표의 전이가
아니라 5번의 `APPROVED → APPLYING` 조건부 전이다.

상태별 목록 조회는 M3·M4 API 범위에 없다. 필요해지면
`GSI2PK = CUSTOMER#{customer_id}#DEPLOYMENT_STATUS#{status}`로 materialize하며, 그 시점은
`PROGRESS.md`의 "completed counter storage migration"과 같은 작업이다. 그때까지 저장하지 않는다 —
Job의 `GSI2`도 같은 이유로 비워 둔다(`docs/DATABASE.md`).

표현 값의 전이는 다음과 같다.

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
- 고객 repository CI 실패는 Deployment Job의 실패가 아니다. fail-closed 지점은 4번의 도달
  가능성 검사이며, 그 시점에는 실패시킬 Deployment Job이 아직 존재하지 않는다 — Deployment
  record·Job·Outbox는 같은 transaction에서 함께 생성되므로 Deployment를 만들지 않으면 Job도 없다.
- audit event의 **종류** 필드 정본은 `event_type`이고 enum 이름은 `AuditEventType`이다.
  `action`으로 통일하지 않는다 — `apps/backend/repositories/dynamodb.py`가 한 item에서 `event_type`
  (audit 종류)과 `action`(`RemediationAction` 값)을 다른 뜻으로 동시에 쓰고 있어, `action`으로
  통일하면 그 item에서 두 값이 같은 키를 다툰다.
- apply 실패나 부분 적용은 자동 재시도하지 않는다. `MANUAL_RECONCILIATION_REQUIRED` audit와
  Deployment `MANUAL_REVIEW`로 남기고, 재시도는 새 plan·새 Deployment·새 승인으로만 한다
  (`docs/DESIGN.md`의 Apply 무재시도 규칙을 상태 값으로 구체화한 것이다).

## Implementation note (2026-09-03, PR write)

§3의 "Patch → branch/commit/PR" 구간이 배선됐다. `agent/runtime/live_github_write_tool.py`의
`LiveGitHubWriteTool`이 §6이 허용한 세 write 표면(branch ref 생성, contents 갱신, pull request 생성)만
호출하고, workflow 파일·merge·apply는 건드리지 않는다. branch 이름은 patch에서 결정적으로 유도되므로
`LiveDeploymentCommitResolver`가 같은 이름으로 merge commit을 찾는 §3·§4의 경로와 맞물린다. patch
바이트는 `packages/contracts`가 아니라 `apps/backend/remediation/patch_content.py`의 canonical 형식이며
DynamoDB에 content-addressed로 저장한다 — Artifact bucket은 ADR-0014의 tenant identity 검토 뒤에 연다.

## 고정돼야 하는 불변식

이 ADR이 참이라면 아래 아홉 개가 테스트로 고정될 수 있어야 한다.

1. apply는 saved plan으로만 실행되고, state `lineage`·`serial`이 다르면 실행되지 않는다.
2. `plan_hash`는 같은 plan에서 두 번 계산해도 같고, A·C·D가 같은 투영 함수를 쓴다.
3. 승인되지 않은 commit·plan으로는 apply run이 dispatch되지 않는다.
4. 같은 approval로 두 번째 apply run이 만들어지지 않는다.
5. `MANUAL_REVIEW`·`REJECTED`·apply 실패 이후 같은 `plan_hash`로 재승인할 수 없다.
6. EventBridge payload만으로는 어떤 상태도 확정되지 않는다.
7. GitHub App 권한에 `workflows: write`가 없다.
8. 파괴적 변경(`delete` 또는 비어 있지 않은 `replace_paths`)이 있는 plan은 승인 화면에 도달하기
   전에 `DESTRUCTIVE_CHANGE_REQUIRES_MANUAL_REVIEW`로 걸린다.
9. 배포 생애주기 상태는 저장된 사실에서 파생되며, 어긋날 수 있는 두 번째 사본이 없다.

## Consequences

- `plan_hash`가 사람이 읽을 수 있는 JSON plan의 digest이므로 승인 화면에 보여준 것과 검증
  대상이 같다. binary plan은 적용 수단으로만 쓰인다.
- `plan_hash` + state `lineage`·`serial` + saved plan의 세 겹으로 plan-apply 사이의 변화가
  fail-closed된다. saved plan을 강제하면 state가 이동한 경우 Terraform 자체가 apply를 거부하므로,
  우리 hash가 약해도 안전성이 확보된다. `lineage`·`serial` 대조를 추가하는 이유는 그 거부를 우리
  상태 기계에서도 값으로 관측해 감사 기록을 남기기 위해서다.
- `DeploymentStatus`는 enum만 늘고 저장 스키마는 바뀌지 않는다. 전이 규칙이 순수 함수와 테스트로
  끝나므로 조건부 write 조정과 "어느 사본이 이기나" 규칙이 없어진다. 대가는 상태별 목록 조회를
  지금 할 수 없다는 것이고, 그 access pattern은 M3·M4 범위에 없다.
- `plan_hash` 투영 함수와 destructive 판정 함수가 역할 경계를 넘는 공용 코드가 되므로
  Producer/Consumer Owner 검토가 필요하다 (CONTRIBUTING).
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
- **`plan_hash` 대상을 제외 목록(blacklist)으로 정의:** 제외 목록은 열린 집합이라 Terraform이나
  Provider가 출력 필드를 늘리면 조용히 hash에 들어와 재현성이 깨지고, 깨진 사실이 승인 재검증
  실패로만 드러나므로 거부한다. 허용 목록은 닫혀 있고 Contract test로 고정된다.
- **state `serial`만 대조:** state가 재생성되면 `lineage`가 새로 발급되고 `serial`이 낮은 값으로
  초기화되므로, 다른 state가 우연히 같은 `serial`로 통과할 수 있어 거부한다.
- **`DeploymentStatus`를 DynamoDB에 저장:** `JobStatus`·`JobCurrentStep`이 이미 같은 사실을
  담고 있어 두 번째 사본이 생기고, 사본이 어긋날 때의 우선순위 규칙과 마이그레이션이 따라오므로
  거부한다. Readiness Score에서 이미 같은 판단을 했다.
- **고객 repository CI 성공을 Deployment 생성 전제조건으로 검사:** 고객 CI가 실패하면 PR이
  merge되지 않아 default branch 도달 가능성 검사에서 이미 걸린다. 검사를 두 개 두면 고객의 merge
  판단이 우리 상태 기계로 들어오고, "Deployment를 만들지 않으면서 Job을 `FAILED`로 만든다"는
  존재하지 않는 Job에 대한 규칙이 생기므로 거부한다.
- **자동 일괄 배포 트리거:** ADR-0018 Open decision 4가 M2 범위에서 제외했고 M3도 단건·사용자
  트리거만 다룬다. 일괄 트리거는 승인 단위와 apply 단위를 어긋나게 하므로 거부한다.
- **audit 종류 필드를 `action`으로 통일:** `dynamodb.py`가 한 item에서 `action`을
  `RemediationAction` 값으로 이미 쓰고 있어 두 값이 같은 키를 다투므로 거부한다. 정본은
  `event_type`이다.

## Open decision

- **Owner:** D(plan/apply 실행, workflow template) + A(Deployment 상태·API) + Security(state
  backend·OIDC·Environment 승인)
- **Needed by:** M2의 audit 정본화·live plan 구현 전 및 M3 착수 전. 이 결정 없이 D가 live 경로를
  구현하면 A/C가 사후에 그 구현을 따라가야 한다.
- **Blocks:** M2 A(`AuditEventType` 기본 enum·기존 audit `event_type` 정규화), M2 D(live plan),
  M3 A(Deployment/Approval 상태 전이, 결과 조회 API), M3 C(Deployment Readiness의 plan 입력),
  M3 D(plan/apply/Event 처리), M3 Shared(승인 없는 Write 방지 E2E), `DeploymentStatus` Contract와
  M3 Deployment audit event 값 추가.
- **Proposed options:** 위 Decision 8개 항목. 각 항목의 대안과 거부 이유는 Rejected alternatives에
  있다.
- **Final record:** `Accepted` (2026-09-02). A·D·Security가 서명 PR 리뷰 approve로 서명했다.
  Decision 1~8을 수정 없이 채택한다. 같은 PR에서 이 상태 전환과 함께 `docs/API.md`,
  `docs/CONTRACTS.md`, `docs/DATABASE.md`, `docs/DESIGN.md`, `docs/architecture/C4-CONTAINER.md`의
  계획 표기를 구현 표기로 옮긴다. Blocks에 묶여 있던 구현 커밋(M2 A audit 정본화, M2/M3 D live
  plan/apply, M3 A Deployment 생성·상태 API)은 이 서명 이후 A(PR #40)와 D 브랜치에서 병렬로
  시작한다.
