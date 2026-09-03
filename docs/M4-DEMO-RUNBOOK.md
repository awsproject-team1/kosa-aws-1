# M4 데모 폐루프 runbook (ADR-0019 · ADR-0020 · ADR-0021)

이 runbook은 WordPress/LAMP 데모에서 폐루프 E2E를 한 번 재현하는 절차와, 그 실행에서 남겨야
하는 관측·비용 증적을 정의한다. 데모 IaC의 위치와 데모 토글 매핑은
`docs/M4-DEMO-IAC-REFERENCE.md`가 정본이다.

이 문서는 로컬 AWS 명령이나 직접 프로덕션 접근을 승인하지 않는다. apply는 데모 저장소에 설치된
protected GitHub Actions workflow를 통해서만 일어나고, Platform은 그 workflow를
`workflow_dispatch`로만 트리거한다(ADR-0019 §6).

## 0. 전제 확인

데모 실행 전에 아래가 모두 준비돼 있어야 한다. 하나라도 비면 데모를 시작하지 않는다.

- 데모 저장소가 `docs/M4-DEMO-IAC-REFERENCE.md` §2의 전제조건(version pin, workflow 설치,
  protected Environment, OIDC Role 분리, state backend/lock)을 갖춘다.
- Platform의 Deployment 생성·상태·검증 조회 endpoint가 배선돼 있다(A 소유, ADR-0019 §4,
  ADR-0020 §7). 없으면 승인 화면이 `commit_sha`/`plan_hash`를 얻을 수 없다.
- Platform composition root에 live 실행 어댑터
  (`LiveApplyDispatchPort`/`LiveWorkflowRunReader`/`LiveActualRereadPort`,
  `agent/runtime/live_deployment_ports.py`)와 `DeploymentWorker`
  (`apps/backend/deployment/worker.py`)가 주입돼 있다(D 소유 runtime 배선, A endpoint 병합 뒤).
- 승인된 sandbox 계정·Region이 `EXPECTED_AWS_ACCOUNT_ID` 검증을 통과한다.

## 1. 폐루프 단계

Deployment 1건은 하나의 Job revision 사슬
`PLAN → WAITING_APPROVAL → APPLY → POST_DEPLOY_VERIFICATION → COMPLETED`로 진행한다. 외부 완료
Event마다 revision이 오르고, Platform은 Event를 신뢰하지 않고 `run_id`로 run을 재조회한다
(ADR-0019 §7).

### 1-1. Initial Assessment (위반 상태)

- 데모 저장소의 여섯 토글을 위반 상태(`false`)로 둔 채(`docs/M4-DEMO-IAC-REFERENCE.md` §3에서
  데모용으로 지정한 조합) default branch에 commit한다.
- 그 commit에 대해 Initial Assessment를 시작한다. 여섯 S3 Rule × 세 관점(IAC/AWS_ACTUAL/DRIFT)의
  Finding·Evidence·Coverage·Readiness Score를 조회한다.
- 이 Assessment의 `assessment_id`가 이후 Post-Deploy Verification의 `source_assessment_id`가 된다.

### 1-2. Remediation → PR

- 자동 조치가 열리는 Rule(`AUTOMATIC`: S3-PUBLIC-001, S3-ACL-001, S3-TLS-001)은 Remediation Worker가
  Bedrock으로 Terraform patch를 생성해 저장하고, 데모 저장소에 branch(`remediation/{finding_id}/…`)·
  commit·PR을 **자동으로 연다**(ADR-0018, ADR-0019 §3·§6). Worker Lambda의 `DEPLOYMENT_RUNTIME_JSON`이
  비어 있으면 이 단계는 patch 생성 전에 fail-closed한다. Manual Review Rule
  (`MANUAL_ONLY`: S3-POLICY-001, S3-ENCRYPT-001, S3-LOGGING-001)은 사람이 토글을 준수 상태(`true`)로 바꾼다.
- 열린 PR은 사람이 검토·머지해 default branch의 merge commit이 된다. apply 대상은 이 merge commit이다
  (ADR-0019). Platform은 merge하지 않는다.

### 1-3. Plan

- Platform이 Deployment를 생성하고(`RUN_DEPLOYMENT`) `DeploymentWorker`가 plan을 요청한다.
- 데모 저장소의 `terraform-plan.yml`이 refreshed saved plan(binary)을 만들고,
  `terraform show -json`을 허용 목록으로 투영한 canonical 바이트의 SHA-256을 `plan_hash`로 낸다.
  plan 시점의 state `lineage`/`serial`을 함께 기록한다(ADR-0019 §1·§2).
- `plan_hash`는 Platform의 `packages/contracts/terraform_plan.py`와
  `ci/terraform/canonical_plan_hash.py`가 **같은 바이트**를 내야 재검증이 통과한다.

### 1-4. Human Approval

- `WAITING_APPROVAL`에서 승인 화면이 `commit_sha`/`plan_hash`를 표시한다(A endpoint,
  ADR-0020 §7).
- 승인(User)·거부(Admin, `REJECT_DEPLOYMENT`)는 감사 event로 남는다. 승인 없이는 apply가
  일어나지 않는다.

### 1-5. Apply (승인된 saved plan만)

- 승인 시 `DeploymentWorker`가 `PLAN_COMPLETED`를 소비해 idempotent하게 apply를 dispatch한다
  (`LiveApplyDispatchPort` → `terraform-apply.yml`의 `workflow_dispatch`, ADR-0019 §5).
  같은 approval로 재호출돼도 새 run을 만들지 않는다.
- `terraform-apply.yml`은 saved plan만 적용한다. apply 직전 `plan_hash`와 state `lineage`/`serial`을
  재검증하고, 하나라도 다르면 apply하지 않는다(§1·§2). protected Environment가 2차 게이트다(§6).

### 1-6. Post-Deploy Verification (준수 상태 재평가)

- `APPLY_COMPLETED`에서 `DeploymentWorker`가 `run_id`로 run을 재조회해 승인 사실
  (repository/workflow_path/ref/plan_hash/conclusion)을 대조한 뒤에만 Actual을 재조회한다
  (ADR-0019 §7, ADR-0020 §8). 승인 사실과 하나라도 다르면 재시도 없이 차단한다.
- **검증 Assessment 생성:** run 대조가 끝나면 Deployment Worker가 `PostDeployVerificationService`를
  호출해 검증 Assessment를 만들고 `ASSESS_RESOURCE` task를 Assessment Queue로 보낸다. 사람이 따로
  시작하지 않는다. `GET /deployments/{id}`의 `verification_assessment_id`가 채워지면 검증이 시작된
  것이다.
- **재조회 시점(ADR-0020 §8):** 1회차는 지연 없이 읽는다. 15초·45초 재조회는 아직 구현되지 않았으므로
  현재 데모에서는 1회 읽기만 일어난다(ADR-0020 Implementation note). 전파 지연으로 `UNRESOLVED`가
  보이면 새 Deployment가 아니라 같은 Deployment의 재검증이 아닌, 사람 확인 대상으로 다룬다.
- 검증은 **새 `assessment_id`**의 Assessment이며, 원 Assessment의 Profile version·rubric·planned
  집합을 그대로 재사용한다(ADR-0020 §1·§2·§3). 3회 후에도 불일치면 자동 실패가 아니라
  `VERIFICATION_INDETERMINATE`로 두고 사람에게 보낸다.
- 비교 결과는 `GET /deployments/{deploymentId}/verification`으로 조회한다(ADR-0020 §7). Finding
  Resolution(`RESOLVED`/`UNRESOLVED`/`NEW`/`INDETERMINATE`/`REGRESSED`)과 Score/Coverage delta는
  planned 집합·Profile·rubric이 모두 일치할 때만 계산된다(§4·§5). "drift가 사라졌다"는 점수가
  아니라 Resolution 값으로 말한다.

## 2. 관측·비용 기록 (ADR-0021 §3 통과 기준)

데모 폐루프 1회 실행에 대해 아래 값을 기록한다. **값이 비어 있으면 미충족으로 본다.** 비용은
절대 상한을 두지 않고 최초 실행값을 기준선으로 남긴다.

| 항목 | 기준 | 이번 실행값 |
| --- | --- | --- |
| Assessment 성공률 | 계획된 평가 중 `EXECUTION_ERROR` 0건 | _(기록)_ |
| Bedrock 호출 | 역할별 호출 수·토큰·p95 지연 | _(기록)_ |
| Queue 건전성 | DLQ depth 0, Queue age 최대값 | _(기록)_ |
| Job 재개 | checkpoint 재개 횟수, 3분 재큐잉 동작 | _(기록)_ |
| plan/apply | 실패 0건, 승인 없는 apply 0건 | _(기록)_ |
| 감사 | Remediation·Approval·Apply·Verification audit event 모두 존재 | _(기록)_ |
| 비용 | 데모 1회의 Bedrock·Lambda·저장소 비용 합계 | _(기록)_ |

같은 실행에서 민감한 Prompt·정책 원문·IaC 전체가 로그에 없음을 확인한다. 완료 Event/게시
payload는 `deployment_id`, `commit_sha`, `plan_hash`, `run_id`, `conclusion`만 담는다
(ADR-0019 §7).

## 3. 실패·중단 시 처리

- **plan_hash mismatch:** apply workflow가 재검증에서 실패로 종료한다. 재승인 없이 apply를
  강행하지 않는다.
- **state lineage/serial 이동:** saved plan을 강제하므로 Terraform이 거부한다. 값으로도 관측해
  기록한다.
- **run 재조회 실패/미완료:** 예외가 아니라 `not_found` 값으로 다루고, Platform은 EventBridge
  payload만으로 상태를 확정하지 않는다(ADR-0019 §7).
- **검증 3회 후 불일치:** `VERIFICATION_INDETERMINATE`로 사람 판단에 넘긴다(ADR-0020 §8).

## 4. 발표 시연 순서 (S3 6 Rule 고정)

§0의 전제가 갖춰진 뒤, 아래 순서로 한 번 리허설하고 그대로 발표한다. 시연 범위는 S3 6 Rule × 3 관점이다.
정책 업로드 → Profile 게시 경로는 화면이 없으므로 기본 시연에서 제외하고 fixture Profile
`profile-mvp-baseline`으로 시작한다. 보여주고 싶으면 §4-0을 curl로 먼저 돌린다.

### 4-0. 준비 (발표 1시간 전)

- GitHub App installation token 재발급 → Secrets Manager 갱신(handoff §2). 1시간 만료다.
- 고객 저장소 `awsproject-team1/test` main이 여섯 토글 위반 조합(`b283b6b` 계열)인지, sandbox 버킷의
  public access block이 위반 상태인지 확인한다. 이것이 시연의 **시작 상태**다(정리하지 않는다).
- SPA 로컬 실행(호스팅 리소스 없음, Cognito callback은 `http://localhost:5173` 기본값):
  ```bash
  export VITE_API_BASE_URL='https://8cimz0a9n9.execute-api.us-east-1.amazonaws.com'
  export VITE_COGNITO_DOMAIN='kosa-governance-sandbox-369676914736.auth.us-east-1.amazoncognito.com'
  export VITE_COGNITO_CLIENT_ID='66ektgjk0aan5nb8ah7789f160'
  export VITE_COGNITO_REDIRECT_URI='http://localhost:5173'
  npm --prefix apps/frontend run dev
  ```
- (선택) 정책 업로드 경로: `POST /policy-sources/uploads` → `/process` → authoring worker가 후보 생성 →
  `/approve` → `POST /policy-profiles`. 화면이 없으므로 curl. 재배포 후 리허설에서 먼저 1회 확인한다.

### 4-1. 순서와 화면

| 순서 | 무엇을 보여주나 | 어디서 | 확인 포인트 |
| --- | --- | --- | --- |
| 1 | 관리자 로그인 | SPA (Cognito Hosted UI) | access token에 customer claim |
| 2 | 자연어로 묻기 | `POST /orchestrate {"message": ...}` (curl) | Parent가 PolicyQA/ASSESSMENT 등 decision을 **제안만** 한다 |
| 3 | Initial Assessment 시작 | SPA "평가 시작" | coverage 18/18, findings에 위반 다수 |
| 4 | Finding 설명·evidence | SPA 리포트 | 정책 locator와 리소스 evidence가 나뉘어 보인다; 사람 검토 항목은 조치 버튼 없음 |
| 5 | 조치 요청 | Finding 카드의 조치 버튼 (S3-PUBLIC-001) | decision `TERRAFORM_PATCH`; Worker가 Bedrock patch 생성 |
| 6 | PR이 열렸다 | GitHub `awsproject-team1/test` PR 목록 | branch `remediation/{finding_id}/…`, 사람이 검토·머지 |
| 7 | Deployment 생성·plan | `POST /remediations/{id}/deployments` (curl) → SPA 승인 화면 | `commit_sha`(merge commit)·`plan_hash` 표시 |
| 8 | 사람 승인 | SPA 승인 버튼 | 승인 없이는 apply가 없다는 점을 말로 짚는다 |
| 9 | apply | GitHub Actions `terraform-apply` (protected Environment 2차 게이트) | plan_hash·state lineage 재검증 뒤 saved plan만 적용 |
| 10 | 검증 Assessment 자동 생성 | `GET /deployments/{id}` | `verification_assessment_id` 채워짐 |
| 11 | Before/After | `GET /deployments/{id}/verification` | Resolution `RESOLVED`; 점수가 아니라 Resolution 값으로 말한다 |
| 12 | 감사 이력 | `GET /audit-events` | Remediation·Approval·Apply·Verification event |

### 4-2. 시간을 잡아둘 곳

- **apply 후 재조회는 현재 1회다**(ADR-0020 §8의 15초·45초 재조회 미구현). 전파가 늦으면 실제로는 고쳐졌는데
  `UNRESOLVED`가 보일 수 있다. 리허설에서 이게 나오면 발표 대본에서 10번 전에 대기 시간을 넣고, 그래도 남으면
  `INDETERMINATE`를 "사람 확인 대상"으로 설명한다. 자동 실패로 말하지 않는다.
- Bedrock 호출은 결정적이지 않다. 5번의 patch 내용이 리허설과 다를 수 있다. PR diff를 그 자리에서 읽는다.
- Deployment Worker의 GitHub run 재조회는 Event를 신뢰하지 않고 `run_id`로 다시 읽는다. apply 완료 후 상태가
  바뀌기까지 수십 초가 걸릴 수 있다.

### 4-3. 실패했을 때 말할 것

- PR이 안 열리면: `DEPLOYMENT_RUNTIME_JSON` 미설정 또는 token 만료. Worker는 patch를 만들기 전에 fail-closed한다.
- plan_hash mismatch: apply workflow가 스스로 멈춘다. 그것이 설계다(§3).
- 검증이 `INDETERMINATE`: 사람 판단으로 넘기는 상태이지 오류가 아니다.

## 관련 문서

- 데모 IaC 참조·데모 토글: `docs/M4-DEMO-IAC-REFERENCE.md`
- workflow template과 경계: `ci/terraform/README.md`
- 릴리스 gate 첨부물: `CONTRIBUTING.md`의 "Release gate (`dev → main`)"
- 결정: `docs/decisions/ADR-0019-approved-deployment-execution-boundary.md`,
  `docs/decisions/ADR-0020-post-deploy-verification-and-comparison.md`,
  `docs/decisions/ADR-0021-demo-and-release-readiness-gate.md`
