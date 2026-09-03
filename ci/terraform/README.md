# Terraform plan/apply workflow templates (ADR-0019 §6)

이 디렉터리는 고객이 **자신의 IaC repository에 1회 수동 설치**하는 GitHub Actions workflow
template이다. Platform(GitHub App)은 이 파일을 만들거나 수정하지 않는다 — App에
`workflows: write`를 주지 않는 것은 편의가 아니라 승인 경계 문제다(ADR-0019 §6). 고객 관리자가
아래 두 파일을 자신의 repository `.github/workflows/`로 복사해 설치한다.

| template (이 저장소) | 고객 repo 설치 경로 | 역할 |
| --- | --- | --- |
| `terraform-plan.yml` | `.github/workflows/terraform-plan.yml` | refreshed plan 생성, plan_hash 산출 |
| `terraform-apply.yml` | `.github/workflows/terraform-apply.yml` | 승인된 saved plan apply |

## 전제 (고객 관리자 1회 설정)

- **State backend (ADR-0019 §2):** bootstrap stack이 versioned·encrypted·TLS-only·
  bucket-owner-enforced S3 bucket과 DynamoDB lock table을 만든다. state key는
  `(repository_id, workspace)`로 분리하고 workspace 이름은 `{customer_id}-{repository_id}`다.
- **OIDC Role (ADR-0007, §6):** plan job은 `TerraformPlanRole`, apply job은
  `TerraformDeploymentRole`을 assume한다. OIDC trust는 exact repository와 exact environment
  subject로 제한한다. Organization이 custom OIDC subject template을 사용하면 repository 이름
  대신 immutable organization/repository ID가 포함될 수 있으므로, GitHub가 실제 발급한 `sub` claim을
  trust policy에 그대로 사용한다.
- **Protected Environment (§6):** apply job은 required reviewers가 붙은 protected Environment를
  2차 게이트로 둔다. plan job도 별도 `customer-terraform-plan` Environment를 사용해 OIDC
  subject와 backend 변수를 고정한다.
- **Version pin (§1):** workflow는 Terraform version을 고정하고, repository는
  `.terraform.lock.hcl`을 커밋한다. Provider 버전이 흔들리면 같은 commit에서 다른 plan이 나온다.
- **Environment variables:** `customer-terraform-plan`에는 `AWS_REGION`, `TF_PLAN_ROLE_ARN`,
  `TF_STATE_BUCKET`, `TF_STATE_KEY`, `TF_LOCK_TABLE`을, `customer-terraform-apply`에는
  `AWS_REGION`, `TF_APPLY_ROLE_ARN`, `TF_STATE_BUCKET`, `TF_STATE_KEY`, `TF_LOCK_TABLE`을
  GitHub Environment variables로 설정한다. 두 Environment에는 Terraform input인
  `SANDBOX_BUCKET_NAME`도 같은 값으로 설정한다. state 설정과 bucket 이름은 workflow input이나
  repository file에 넣지 않으며, 두 Environment의 backend 값은 완전히 같아야 한다.

## 경계 규약

- **apply는 saved plan만 적용한다(§1·§2):** `terraform apply -input=false <saved plan>`. apply 시점
  재계산(`terraform apply` 단독)은 금지한다. 승인 대상과 적용 대상이 달라진다.
- **plan_hash(§1):** `terraform show -json`을 허용 목록으로 투영한 canonical 바이트의 SHA-256.
  Platform의 `packages/contracts/terraform_plan.py`와 **같은 규칙**이어야 재검증이 통과한다.
- **완료 Event(§7):** Platform은 Event를 신뢰하지 않고 `run_id`로 run을 재조회한다. run name에
  `plan_hash=<hash>`를 담아 Platform이 승인 사실과 대조할 수 있게 한다. Event/게시 payload는
  `deployment_id`, `commit_sha`, `plan_hash`, `run_id`, `conclusion`만 담는다. plan 본문·정책
  원문·IaC 본문은 담지 않는다.
- **DynamoDB write 권한 없음(§7):** GitHub Actions에 상태 정본 write 권한을 주지 않는다.
- **plan artifact는 별도 run에서 온다(§1):** apply는 자기 run이 아니라 `terraform-plan` run이 만든
  saved plan을 적용한다. 그래서 apply workflow는 `plan_run_id` 입력과 `actions: read` 권한,
  `github-token`으로 그 run의 artifact를 내려받는다. 같은 run 안에서 만든 artifact가 아니다.
- **state 이동 실제 차단(§2):** plan은 plan 시점 `lineage`/`serial`을 `plan.state.json`으로 남기고,
  apply는 그 값을 실행 시점 state와 **실제로 비교**해 하나라도 다르면 apply 전에 실패시킨다.
  출력만 하지 않는다. `serial` 단독이 아니라 `lineage`와 쌍으로 대조한다.

## plan run id는 어떻게 전달되는가

apply는 자기 run이 아니라 `terraform-plan` run이 만든 saved plan을 적용한다(§1). 그래서 apply
workflow는 `plan_run_id`를 필수 입력으로 받는다.

이 값은 D가 dispatch 시점에 만들어내지 않는다. plan과 apply는 서로 다른 실행이고 그 사이에 사람
승인이 들어가므로, run 좌표는 durable해야 한다. `PlanRequestPort.request_plan()`이 돌려주는
`PlanExecutionResult.plan_run`(`WorkflowRunReference`)에 담겨 저장되고, apply 단계에서 A가
`DeploymentWork.plan_run`으로 다시 읽어 `ApplyDispatchPort.dispatch_apply(..., plan_run=...)`에
넘긴다. `LiveApplyDispatchPort`가 그것을 `plan_run_id` input으로 전달한다.

세 경계 모두 run 좌표가 승인된 배포·저장소 안인지 확인한다 — `PlanExecutionResult`는 plan과 같은
`deployment_id`인지, Worker는 work scope 안인지, live 어댑터는 approval과 tool scope에 맞는지.
확인 없이 통과시키면 다른 배포의 plan artifact를 적용하면서도 나머지 승인 값은 전부 일치하는
상태가 만들어진다.
