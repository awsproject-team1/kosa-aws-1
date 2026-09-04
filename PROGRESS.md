# Progress

## Current

- **정책 후보 추출이 부분 성공을 완전한 결과로 표시하지 않는다.** 로컬 보관 사내 체크리스트의
  선언 항목 128개와 Markdown parser 결과 128개가 정확히 일치하고 누락·중복·경고가 없음을 다시
  검증했다(전체 정규화 unit 193개). Bedrock 응답은 이제 모든 청크 locator를 Requirement 또는
  `non_requirement_locators`로 빠짐없이 분류해야 하며, 중복·청크 밖 locator·잘못된 후보·청크 실패
  하나라도 있으면 실행 전체가 실패한다. 코드 prompt와 배포 Model Profile의 `prompt_version`도
  `policy-authoring/2026-09-04`로 고정해 서로 다른 schema가 실행되지 않게 했다.
  고객 disposable IaC 저장소는 PR #35로 기존 S3와 함께 EC2·RDS·ALB fixture를 복원했다. restricted
  plan role에 없던 AZ/AMI discovery 권한을 요구하지 않도록 explicit AZ와 AWS-owned AL2023 public
  parameter를 사용했다. 실제 artifact 대조에서 customer helper가 없는 optional field를 생략해 platform의
  null-filled canonical projection과 hash가 달라지는 결함도 발견해 PR #36으로 수정했다. 최종 원격 OIDC
  plan run `33842560536`이 성공했고 platform 재검증 바이트도 정확히 일치했다(22개 plan 좌표: create 17,
  기존 S3 no-op 5). 실제 apply는 실행하지 않았다. 검증: unit 1285 / contract 239 / integration 21 /
  security 118, Ruff check/format, Terraform 1.9.5 init/validate/fmt, policy source digest 21개 대조 통과.

- **관리자 정책 후보 조회가 Backend의 전체 검토 형식을 표시한다.** 기존 SPA는
  `CandidateReviewEntry`에서 Rule ID/version·요약·매핑 사유만 보여 상세 요구사항, resource/evidence,
  평가 rubric·semantics, 서버 생성 locator/hash를 숨겼고 `unsupported`/`rejected` 결과도 렌더링하지
  않았다. 이제 승인 가능·미지원·검증 거절을 별도 섹션으로 표시하고, 집계와 rejection code를 함께
  보여준다. READY 결과는 opaque cursor가 끝날 때까지 읽어 50건 초과 후보를 누락하지 않으며,
  `QUEUED`뿐 아니라 `PROCESSING`도 진행 중으로 처리한다. Frontend 응답 타입이 Backend 검토 Contract의
  전체 필드와 일치하는 회귀 테스트를 추가했다. 검증: frontend production build, 관련
  contract/unit/integration 31건, Ruff, diff check 통과.

- **PR #72 리뷰 후속(2026-09-04, `f574c0c`·`34b5f05`, #72에 포함돼 `dev` 병합):** `assign_profile`이 호출자가 준
  email을 검증 없이 Cognito에 넘겨 다른 customer 사용자의 `profile`을 덮어쓸 수 있던 테넌트 구멍을 닫았다
  (`admin_get_user`로 대상의 `custom:customer_id` 확인, 없는 사용자와 남의 사용자는 같은 403).
  `DELETE /policy-sources/…`는 record → S3 순서로 바꾸고 없는 문서는 500이 아니라 404, 승인 문서 409 매핑은
  문자열 타입명 비교 대신 `packages/common/errors.py`의 실제 예외로. CORS `localhost:5173`은 sandbox에서만.
  `cognito-idp:AdminGetUser`를 ApiRuntimeRole에 추가하고, 서비스가 호출하는 Cognito API ↔ role 허용 목록을
  소스에서 읽어 대조하는 security test를 넣었다. `ruff format` 7개 파일. 검증: unit 1231 / contract 236 /
  integration 21 / security 108, cfn-lint, frontend build. **주의: 마지막 Foundation 배포는 `d687a00`(#71)이라
  #72의 route 8개·IAM·`USER_POOL_ID`·`AdminGetUser`는 workflow 기준으로 아직 라이브에 없다.**

- **SPA 콘솔을 관측성 중심으로 재설계하고, 라이브 sandbox에서 문서 관리·사용자 관리·평가/게시
  폐루프를 시연 가능한 상태로 굳혔다** (PR #72, branch `feature/console-redesign-and-authoring-hardening`,
  base `dev`). 정책 authoring 견고화와 신규 콘솔 API를 함께 포함한다. 라이브 배포·검증까지 완료.
  - Authoring 견고화: Bedrock extractor가 ```json code fence를 제거하고, chunk 크기를 낮춰
    (`UNITS_PER_CHUNK=6`, overlap 1, maxTokens 8192) 잘림을 줄이며, 필드 스키마·분류 규칙을 명시한
    system prompt로 재작성. fail-soft — 잘못된 requirement/chunk는 건너뛰고 실행을 이어가되, 금지된
    evaluation-outcome 필드(`PoisonedResponseError`)는 그 chunk를 중단, 모든 chunk 실패 시에만 전체 실패.
    `finalize_upload`/`document_from_item`의 Decimal-vs-int 버그도 수정.
  - 신규 콘솔 API: `GET /policy-sources`(문서 목록), `DELETE /policy-sources/{sid}/versions/{ver}`
    (미승인만 삭제, 승인 문서는 409로 거부 — Profile evidence 보호), `GET /scope`,
    `POST/GET /admin/users`, `POST /admin/users/profile`(사용자별 Profile을 Cognito 표준 `profile`
    속성에 저장). 신규 `Action.MANAGE_USERS`(Admin 전용). `docs/API.md`·`docs/CONTRACTS.md` 동기화.
  - 콘솔 UX: 왼쪽 관측 패널에 LangGraph/Queue-Jobs에 더해 삭제 진행상황, "연결된 고객사 리소스"
    (repository_id + GitHub repo + AWS 계정), "사용자·지정 Profile"을 상시 표시. 다중 문서 후보를
    한 장바구니로 담아 단일 Profile 게시.
  - 발표용 "실제 연결" 노출: `/scope`가 `ASSESSMENT_SCOPE_JSON`의 비밀 아닌 연결 정보
    (`github_repository`, `aws_account_id`)를 함께 반환하도록 확장하고, 콘솔 좌측에 표시. secret 참조
    (role ARN·secret id)는 계속 미노출. `EnvironmentAssessmentScope`는 이 두 필드는 허용하되
    `policy_profile_id`·미지 필드는 fail-closed 유지.
  - 버그 수정 2건: (1) 삭제 시 "Failed to fetch"는 HttpApi CORS `AllowMethods`에 DELETE가 빠져
    preflight가 막힌 것 — IaC와 라이브 API에 DELETE 추가. (2) 승인 문서 삭제가 "요청 실패 (409)"로
    떴던 건 프론트가 중첩 오류 봉투 `{"error":{"code":...}}`를 최상위에서만 읽던 버그 — `error.code`를
    읽도록 수정해 "승인된 문서는 삭제할 수 없습니다" 안내로 표시.
  - 라이브 인프라: api role에 `dynamodb:ConditionCheckItem`(cart 게시 approve 트랜잭션의 500 원인),
    `DeleteItem`, `s3:DeleteObject`(policy-sources), Cognito Admin 5종, `bedrock:InvokeModel` 추가.
    `USER_POOL_ID` env 추가. API Gateway에 신규 8개 route 등록. `ASSESSMENT_SCOPE_JSON`에
    `github_repository=awsproject-team1/test`, `aws_account_id=369676914736` 반영.
  - 검증: unit 1200 / contract 236 / security 105 통과, ruff·frontend build 통과. 라이브 E2E —
    문서 목록/삭제(미승인 200·승인 409)·`/scope`(연결정보 포함)·사용자 생성/목록/Profile 지정·
    approve+publish(cart) 성공, profile `profile-internal-baseline@v1` 게시. 프론트는 CloudFront
    `E1BENIQUOV74AI`(`https://dfur2d0d1329n.cloudfront.net`)에 배포·무효화. 정책 원문·secret은
    저장소·문서에 넣지 않는다.

- **Golden 릴리스 게이트의 입력을 만드는 producer가 없었다.** ADR-0022는 C consumer(`release_quality.py`,
  `evaluate_m4_golden_release_gate.py`)와 D 결합(`release_binding.py`)을 구현했지만, §4의 "A producer" —
  customer runtime에서 Post-Deploy 18 Case를 5회 돌려 `m4-golden-observations-v1` bundle을 내보내는 쪽 —
  는 코드에 없었다. 게이트는 완성돼 있으나 먹일 것이 없는 상태였다.
  - `apps/backend/assessment/golden_observations.py`: `GoldenObservationExporter`가 운영
    `BedrockStructuredEvaluator`로 IAC/Actual을 평가하고 DRIFT는 `derive_drift_results()`로 파생한다.
    `UsageRecordingConverseClient`가 호출마다 latency/token을 기록하고, usage 없는 응답은 fail-closed.
    provider 실패는 `stable_error_code()`(예: `PROVIDER_THROTTLED`)로만 남기고 message는 버린다.
    bundle에는 resource ID·snapshot 본문·rationale이 들어가지 않는다(회귀 테스트로 고정).
  - snapshot reader 두 종: `DirectoryGoldenSnapshotReader`(dry run)와 `ArtifactStoreGoldenSnapshotReader`
    (S3 content-addressed store + private `artifact_id → sha256:` index). 첫 Bedrock 호출 전에 12개를 모두
    읽어 형식·IAC/Actual resource 일치를 검증한다.
  - `scripts/export_golden_observations.py`: `--customer-sandbox`(실제 Bedrock/S3, D 결합 digest 계산, 쓴
    파일을 C parser로 자체 검증) / 기본 dry run(`runtime_mode=DRY_RUN`, gate가 거부). 로컬에서
    export → gate 연결을 실제로 돌려 확인했다: dry-run bundle 90 observation / 60 call, gate exit 2
    "release evidence must come from CUSTOMER_SANDBOX".
  - 남은 것은 코드가 아니라 실행이다: sandbox에서 12개 Golden snapshot artifact를 만들어 index로 넘기고,
    `--customer-sandbox`로 한 번 돌린 뒤 gate report를 release packet에 첨부한다.

- **배포 workflow가 CloudFormation 파라미터 다섯 개를 넘기지 않고 있었다.** 템플릿에는 선언돼 있지만
  `--parameter-overrides`에 없어 **값을 설정할 통로 자체가 없었고**, 그래서 세 기능이 "배선됨"으로
  적혀 있으면서 프로덕션에서는 영구히 fail-closed였다.
  - `DeploymentRuntimeJson`·`DeploymentGitHubSecretArns` → TERRAFORM_PATCH의 PR write(#66)와 apply 대상
    commit 해석(#65)이 동작할 수 없었다.
  - `PolicyAuthoringModelProfileJson` → ADR-0023 정책 후보 추출 worker의 Bedrock 권한이 만들어지지 않는다.
  - `FrontendCallbackUrl`/`FrontendLogoutUrl` → Cognito Hosted UI가 `localhost:5173`에 고정된다.
  - 다섯 개를 workflow env와 `--parameter-overrides`에 추가하고, 배포 pair(runtime JSON + token ARNs)를
    CloudFormation 호출 **전에** all-or-none으로 검증한다. 회귀는
    `tests/unit/test_deploy_workflow_parameters.py`가 템플릿 파라미터 집합과 workflow override 집합을
    대조해 고정한다 — 파라미터를 추가하고 통로를 만들지 않으면 테스트가 먼저 깨진다.
  - 같은 테스트가 배포 인증이 OIDC임을 고정한다(`id-token: write` + `role-to-assume`, 정적 access key
    문자열 부재). **재배포에 AWS access key는 필요 없다.**
  - 검증: `ruff check .`/`format` 통과, unit 1178 / contract 237 / integration 21 / security 102 통과,
    workflow YAML 파싱 확인.

- **폐루프의 마지막 코드 조각 — patch → GitHub PR write — 를 이었다** (#65 handoff §6 B, ADR-0019 §3·§6).
  - `BedrockPatchGenerator`는 digest만 남기고 변경 내용을 버리고 있었다. 이제 canonical patch 바이트
    (`apps/backend/remediation/patch_content.py`)를 `content_sha256`으로 저장한다
    (`DynamoDbPatchContentStore`, `REMEDIATION_PATCH#{digest}`, 300KB 상한). S3가 아닌 이유는 ADR-0014의
    tenant-scoped identity 게이트를 건드리지 않기 위해서다.
  - `LiveGitHubWriteTool.open_pull_request()`가 branch ref·contents·pull request 세 write만 호출한다.
    branch 이름이 patch에서 결정적이라 재전달은 있는 ref·같은 blob·열린 PR을 재사용한다. 결과는
    `REMEDIATION#{id}.pull_request`에 한 번 기록되고, Deployment 생성은 여전히 branch 이름으로 merge
    commit을 찾는다.
  - `RemediationWorker`는 `generate → put_result → open PR → put_pull_request` 순서이며, PR port가
    없으면(`DEPLOYMENT_RUNTIME_JSON` 미설정) TERRAFORM_PATCH를 **생성 전에** 거부한다. Remediation Worker
    Lambda에 `DEPLOYMENT_RUNTIME_JSON`을 추가했다(GitHub token 권한은 `WorkflowRuntimeRole`의 기존
    조건부 policy가 이미 준다).
  - **조립 smoke test**(`tests/unit/test_composition_smoke.py`): boto3를 stub으로 바꿔 API Lambda·
    Remediation Worker·outbox sweeper의 실제 composition root를 만들어 본다. 2026-09-03 검토에서 잡은
    "route는 있는데 service가 조립되지 않은" 종류의 결함을 앞으로 테스트가 잡는다.
  - **Golden 실행기**(`scripts/run_assessment_golden.py`): bench가 아니라 Runtime의
    `BedrockStructuredEvaluator`와 승인 Profile로 Golden case를 반복 실행한다. `--dry-run`으로 배관을
    검증했고, 실제 실행은 case별 snapshot 파일(`{artifact_id}.json`)과 sandbox Bedrock이 필요하다 —
    R2(v3 Profile 재검증)의 실행 수단이다.
  - 검증: `ruff check .`/`format` 통과, unit 1174 / contract 237 / integration 21 / security 102 통과.

- **구현 검토(2026-09-03) 후속: Finding 이후 경로 중 A/C/D 조각 세 곳을 이었다.** Remediation 시작
  API 조립과 Terraform patch/PR 생성은 별도 진행 중이라 손대지 않았다.
  - **Post-Deploy Verification Assessment가 실제로 만들어진다** (ADR-0020 §1·§7). 전에는
    `plan_verification_assessment()`를 부르는 곳이 없어 apply 뒤 Actual을 다시 읽고 끝났고,
    `GET /deployments/{id}/verification`은 입력이 생길 수 없었다. 이제 `DeploymentWorker._verify_apply()`가
    run facts 확정 뒤 `VerificationStarter`를 호출하고, `apps/backend/deployment/verification.py`의
    `PostDeployVerificationService`가 원 Assessment의 판본·plan·Model Profile을 pin한 새 Assessment와
    Deployment Job 다음 revision·`ASSESS_RESOURCE` outbox·record link를 한 transaction으로 쓴다
    (`apps/backend/repositories/deployment_verification.py`). Deployment Worker Lambda에
    `ASSESSMENT_QUEUE_URL`이 추가됐다(권한은 기존 `WorkflowRuntimeRole`이 이미 가짐). §8의 15초·45초
    재조회는 미구현이며 1회 읽기만 한다 — ADR-0020 Implementation note.
  - **승인된 Rule의 실행 의미가 평가기에 도달한다.** `BedrockStructuredEvaluator`가 authored Rule의
    `evaluation_rubric`·`applicability_semantics`·evidence capability를 prompt에 싣고, 모델의
    `EXECUTION_ERROR` 반환을 거부한다. **Prompt가 바뀌어 승인 Profile을 `assessment-nova-lite-m1-v3`
    (`assessment-three-perspective-rubric-v3`)로 올렸다. 릴리스 전 Golden 36 case 반복 평가를 이 Profile로
    다시 실행해야 한다** (ADR-0021 gate).
  - **AWS Actual pre-flight evidence gate** (ADR-0023 §2): `ActualBedrockEvaluator`가 `required_evidence`의
    `document_paths`를 read 결과에서 먼저 확인하고, 비어 있으면 모델을 부르지 않고 Code가
    `INSUFFICIENT_EVIDENCE`를 만든다. legacy Rule과 IaC hint는 대상이 아니다.
  - **live M1 plan이 planner를 따른다** (ADR-0023 §7·§8): `_with_complete_evaluation_plan()`이 모든 Rule에
    세 관점을 하드코딩하던 것을 `EvaluationExecutionPlanner`로 바꿨다(IaC 전용 Rule은 IAC 좌표만).
    승인된 MANUAL Rule이 있으면 `governance:{repository_id}` work를 추가해 `ManualReviewEvaluator`로
    MANUAL 좌표를 남긴다. 검증 경로도 계획의 governance 좌표를 허용한다.
    `NoApplicablePolicyRulesError`(PolicyNotFoundError의 하위)로 "Rule 없음"과 "Profile 없음"을 구분한다.
  - **화면이 rationale·evidence를 보여준다.** Finding을 위반(FAIL)과 사람 검토 필요
    (MANUAL_REVIEW/INSUFFICIENT_EVIDENCE)로 나누고, 판단 이유·정책 근거·리소스 상태 근거·평가 commit·
    예외 억제 표시를 펼쳐 본다. 검토 필요 항목에는 조치 버튼을 두지 않는다.
    `test_frontend_response_contracts`에 `Suppression` 매핑을 더했다.
  - 문서: DESIGN/PRD/ADR-0002의 "AI가 Severity를 선택" 문구를 코드(Rule severity 고정)에 맞췄고,
    CONTRACTS/API/DATABASE/ADR-0020/runbook을 동기화했다. 기존 unit 실패 3건(RDS `ec2_client_factory`,
    deployment runtime 조립)도 고쳤다.
  - 검증: `ruff check .` 통과, unit 1094 / contract 237 / integration 19 / security 102 통과,
    `npm --prefix apps/frontend run build` 통과.

- **고객이 업로드한 정책이 실제로 평가를 결정하게 됐다.** 그전까지 Runtime은 `fixtures/rules/`에
  커밋된 Rule을 읽었고, 고객이 무엇을 업로드하든 평가 결과는 같았다 — 업로드·정규화·승인 단계
  전체가 결과에 아무 영향을 주지 않는 장식이었다. 이제 경로가 이어진다: 업로드 → 정규화 →
  후보 추출(비동기) → 사람의 부분 승인 → Approved Rule Registry → Profile 게시 → Assessment 생성
  시 판본 고정 → 고객 partition의 승인된 Rule로 평가 (ADR-0023).
  - **자동화 경계는 code-owned Governance Control Catalog가 정의한다**
    (`apps/backend/policy/control_catalog.py`, 17개 Control). 이 경계가 없으면 "AI가 그렇게
    판단했다"가 곧 "제품이 평가할 수 있다"가 되고, 실행 경로 없는 Rule이 승인 가능해진다.
    `AVAILABLE`/`KNOWN_UNSUPPORTED`/`MANUAL` 세 상태를 구분한다 — `EC2_SNAPSHOT_NOT_PUBLIC`은
    M1 planner가 Snapshot work를 못 만들므로 `KNOWN_UNSUPPORTED`다. Catalog에 있다는 것과 지금
    자동 평가할 수 있다는 것은 다른 말이다.
  - **AWS와 IaC의 evidence capability를 비대칭으로 뒀다.** AWS는 adapter의 projected document
    경로를 갖고 Runtime이 모델 호출 전에 근거 유무를 판정한다. 그 경로가 실제 adapter 출력에
    존재하는지는 손으로 적은 기대값이 아니라 **실제 adapter를 가짜 AWS 응답으로 돌려** 확인한다
    (`tests/unit/test_governance_control_catalog.py`). IaC는 raw HCL을 받고 Evidence가 파일
    단위이므로 Terraform hint는 prompt 경계 설명일 뿐이며, `document_paths`를 가질 수 없다.
    IaC attribute-level pre-flight는 HCL parser 계층이 필요해 이번 범위 밖이다.
  - **LLM은 제안만 하고 판정하지 않는다.** `ExtractedRequirement`에 `judgment`/`severity`/
    `score`/`source_score`/`anchor` 자리를 만들지 않았다 — prompt로 금지하는 것과 schema에 자리가
    없는 것은 다르고, 자리가 있으면 언젠가 채워진다. severity는 Catalog가 정하고 리뷰 API는
    read-only `proposed_severity`로 노출한다. `SourceReference`도 모델이 주는 locator만 받아
    서버가 digest를 조회해 만든다. Catalog 밖 evidence 요청은 **빼고 만들지 않고 후보를 거절한다** —
    빼고 만들면 승인된 Rule과 AI가 제안한 Rule이 달라지고 그 차이가 아무 데도 남지 않는다.
  - **AUTOMATABLE 후보가 검증에 실패해도 MANUAL로 바꾸지 않는다.** 그것은 검증 실패로부터 사람이
    승인 가능한 Rule을 만들어내는 일이다. 실패한 후보는 rejection code와 함께 보존하되 Rule로
    변환하지 않는다. `UNSUPPORTED`(authoring: 만들 수 있는 Rule이 없다)와 `OUT_OF_SCOPE`
    (Runtime: 이 대상에 적용되지 않는다)는 다른 질문의 답이므로 alias를 만들지 않았다.
  - **정책 원문은 `ExtractionUnit` 안에만 존재하고 그 타입에는 직렬화가 없다.** 규율이 아니라
    구조로 막는다 — `to_dict()`가 생기는 순간 DynamoDB item이나 API 응답에 실수로 담을 수 있다.
    `repr()`도 텍스트를 가린다. Artifact Reader는 READY 상태·크기·payload digest·exact JSON
    schema·unit 수와 **순서**·locator/kind/origin·정규화 digest를 전부 확인한 뒤에만 텍스트를
    내놓고, 하나라도 실패하면 후보 하나가 아니라 추출 전체를 중단한다.
  - **후보를 한 item에 담지 않는다.** 문서가 만드는 후보 수는 문서에 달려 있고, 단일 item은
    DynamoDB 크기 상한에 걸리는 순간 그 문서의 추출 **전부**가 실패한다. manifest가 완결 경계를
    담당하고(`PROCESSING → child write → count/digest 검증 → READY`), Review와 Approval은 READY
    manifest만 읽는다. 같은 source version을 다른 extractor·prompt·Catalog로 재추출하면
    identity가 달라지므로 재시도가 아니라 **다른 추출**로 보고 fail-closed한다.
  - **승인이 Rule Registry를 같은 transaction에 쓴다.** 승인 record만 쓰고 Rule item을 나중에
    쓰면, 그 사이에 게시된 Profile이 참조하는 Rule을 Catalog가 찾지 못한다. Catalog는
    `entity_type == POLICY_RULE`과 `lifecycle == APPROVED`를 함께 확인하며, 미승인 Rule은
    "없음"이 아니라 **오류**다 — None을 돌려주면 Profile이 참조하는 Rule이 사라진 경우와 구별되지
    않는다.
  - **자체 리뷰에서 잡은 것:** 승인 재시도의 멱등 판정이 서버가 만드는 write 시각까지 비교하고
    있었다. 그러면 두 write가 같은 마이크로초에 떨어질 때만 통과하므로 흡수 경로가 사실상
    존재하지 않는다 — 테스트가 순서에 따라 붙었다 떨어졌다 했다. 비교에서 write 시각을 제외하고,
    Rule item이 승인 시각과 같은 이름(`approved_at`)으로 서버 시각을 쓰던 것을 `recorded_at`으로
    바꿨다. 5분 뒤 같은 승인이 재전송되는 경우를 테스트로 고정했다.
  - **Profile을 판본 이력과 current pointer로 나눴다.** 판본을 고정한 Assessment가 나중에 그
    판본을 직접 읽을 수 있어야 하고(pointer만 두면 불가능), 새 Assessment가 무엇을 고를지 정할
    곳도 있어야 한다. pointer 교체는 `expected_current_version` 낙관적 동시성으로 보호한다 —
    없으면 동시에 게시된 두 Profile 중 나중 것이 앞의 것을 조용히 덮어쓰고 두 게시자 모두 자기
    Profile이 현재 판본이라고 믿는다.
  - **`policy_profile_version`을 모든 phase의 필수 값으로 올렸다** (ADR-0020 amendment). 전에는
    verification 전용 pin이었다. 실행 도중 Profile이 교체되면 앞뒤 결과가 다른 allow-list에서
    나오는데, 그 위험은 검증에만 있는 것이 아니라 실행 시간이 긴 모든 Assessment에 있다 —
    Profile 게시가 이제 고객 승인으로 수시로 일어나므로 더욱 그렇다. 판본이 없는 저장된 record는
    최신 pointer로 조용히 대체하지 않고 실패한다.
  - **Runtime configuration과 Policy Catalog의 책임을 분리했다.** 전자는 "어떤 Repository와 AWS
    Resource를 읽을 수 있는가", 후자는 "어떤 게시된 Profile을 쓸 수 있는가"에 답한다. Profile을
    배포 JSON key에 두면 고객이 정책을 승인·게시할 때마다 인프라 배포가 필요해진다 — 승인 직후
    평가에 쓸 수 있어야 한다는 목표와 정면으로 충돌한다. `ASSESSMENT_SCOPE_JSON`에 아직
    `policy_profile_id`가 남아 있으면 조용히 무시하지 않고 **거부한다** — 무시하면 운영자는
    Profile 경계가 여전히 환경변수로 강제된다고 믿은 채 배포한다.
  - **실행 유형이 Perspective 집합을 정한다.** IaC 전용 Rule은 `IAC`만, AWS 전용은 `AWS_ACTUAL`만
    만들고 **Drift로 보내지 않는다** — 한쪽만 평가하는 것이 그 Rule의 정의이므로,
    `derive_drift_results()`가 없는 쪽을 "누락된 Perspective"로 읽어 `MANUAL_REVIEW`를 만들면 실제
    불일치와 구별되지 않는다. legacy Rule(`evaluation_type is None`)은 지금까지처럼 세 관점을
    유지한다. 계획·실행·Drift 대상 선택이 모두 `EvaluationExecutionPlanner` 하나를 통과한다.
  - **`EvaluationPerspective.MANUAL`을 더했다.** 조직 통제를 기존 세 관점 중 하나로 표현하면 그
    결과가 "IaC를 읽고 내린 판단"처럼 보인다. `ManualReviewEvaluator`는 Bedrock도 Tool도 부르지
    않고 `MANUAL_REVIEW` 좌표만 남긴다 — 빼면 Coverage가 그 통제를 모르고 Initial/Verification의
    planned set이 달라져 비교가 깨진다. 좌표는 `governance:{repository_id}`로 Repository 단위 안정
    값이다(Assessment ID를 쓰면 비교하려고 만든 결과가 비교를 불가능하게 만든다). readiness는
    **숫자 평균에서만** 제외하며, 제외 기준은 Perspective이지 status가 아니다 — 기존
    IAC/AWS_ACTUAL의 `MANUAL_REVIEW` 점수 의미는 그대로다.
  - **Authoring Worker는 전용 큐와 전용 IAM Role을 갖는다.** 정책 원문을 읽는 권한과 고객 AWS
    계정을 읽는 권한이 같은 Role에 있으면 한쪽의 사고가 다른 쪽 자료까지 닿는다. Role은 정규화
    artifact를 **읽기만** 하고(write는 정규화 단계의 책임), 자기 큐에 다시 넣지 못하며(실패한
    추출이 스스로를 무한 재요청하는 것을 막는다), 승인된 authoring 모델이 설정되지 않으면 Bedrock
    권한 자체가 만들어지지 않는다. DLQ에는 알람을 걸었다 — 추출되지 않은 정책은 "위반 없음"이
    아니라 "아직 검토할 것이 없음"으로 보인다.
  - 검증: `ruff check .`/`ruff format` 통과. unit 1056, contract 237, integration 19, security
    102 통과. **unit 3건은 이 작업 이전부터 실패하던 것으로 그대로 남아 있다** —
    `test_multiresource_actual_adapters`의 RDS 페이지네이션 2건(`AssumeRoleRdsResourceTool`이
    `ec2_client_factory`를 요구하는데 테스트가 넘기지 않는다)과
    `test_deployment_runtime`의 worker 조립 1건이다. 이 변경과 무관하며 손대지 않았다.

- **PR #64 리뷰 후 실행 차단 3건을 보완했다.** 공개 API가 만든 selector 없는 Assessment는 보호된
  runtime target의 전체 리소스로 확장되고, 모든 resource work가 하나의 immutable evaluation plan을
  공유한다. verification은 source plan이 지목한 승인 리소스만 재평가하며 plan 저장은 동일 재시도만
  허용한다. `RDS-ACCESS-001`에는 연결된 VPC security group의 실제 `IpPermissions`를 제공하고 부분
  응답은 거부한다. Cognito 로그인 왕복은 원래 `assessment_id`/`deployment_id` 경로를 복원하며,
  Assessment 생성 뒤에는 전체 reload 없이 메모리의 access token을 유지한다.
  - 검증: frontend production build와 `git diff --check` 통과. 로컬 Python runtime/Ruff가 없어
    Python suite와 Ruff는 실행하지 못했다.

- **구현돼 있던 endpoint 셋이 API Gateway route 없이 방치돼 있었다.** `POST /findings/{id}/remediations`
  (ADR-0018 조치 흐름의 **진입점**), `POST /deployments/{id}/approve`(M3 사람 승인 게이트),
  `GET /deployments/{id}/observability`. handler branch는 세 개 다 있는데 CloudFormation에 route가
  없었다. API Gateway는 명시적 allow-list이므로 **프로덕션에서는 조치를 시작할 수도, 배포를 승인할
  수도 없는 상태였다.** 이 문서의 이전 판과 `docs/API.md`가 셋을 "배선됨/열었다"로 적고 있었으므로
  문서도 사실과 달랐다.
  - route 세 개를 선언하고, **회귀를 사람이 유지하는 목록에서 handler 파생으로 바꿨다.**
    기존 회귀는 route 이름과 key를 손으로 적은 목록이었고, handler에 branch가 늘어날 때 같이
    갱신되지 않아 조용히 낡았다 — 그게 이 구멍이 생긴 방식이다. 새 회귀는
    `apps/backend/api/handler.py`를 AST로 읽어 분기 조건에서 (method, path) 요구를 뽑아내고
    CloudFormation route와 대조한다. **suffix만 비교하면 안 된다** — `/approve`는
    policy-source 승인 route와 deployment 승인 route가 함께 쓰는 접미사라, method를 짝지어야
    누락이 다른 route에 가려지지 않는다(이 함정을 실제로 밟아 확인했다: approve route를 지운
    상태에서 suffix 버전 회귀는 통과했다).
  - route가 선언됐지만 JWT authorizer가 빠지는 경우도 별도 회귀로 막았다. 선언만 되고 authorizer가
    없으면 인증 없는 호출이 handler까지 닿는다.
  - **반대 방향도 막았다** — 선언됐지만 handler가 처리하지 않는 route는 런타임 404다. handler→route
    검사만으로는 RouteKey 오타를 잡지 못한다. `GET /audit-events`를 `/audit-eventz`로 바꿔 두 방향
    검사가 모두 실패하는 것을 확인했다.
  - 검증: approve route를 비활성화한 상태에서 새 회귀가
    `handler serves POST /deployments/*/approve but no API Gateway route declares it`로 실패하고,
    복원 후 통과하는 것을 확인했다.

- **M3 폐루프를 운영할 UI가 없었다.** `apps/frontend`는 Cognito 로그인과 Assessment 결과 조회
  101줄이 전부였다 — 사람 승인 게이트가 M3의 핵심인데 승인할 화면이 없으면 폐루프는 문서에만
  존재한다. Assessment 화면에서 Finding별 **조치 요청**(`POST /findings/{id}/remediations`)을 걸 수
  있게 하고, **Deployment 화면**(`?deployment_id=`)에 파생 상태·plan/commit 식별자·승인·거절·
  Post-Deploy 비교를 붙였다.
  - **승인은 상태 조회가 돌려준 `commit_sha`/`plan_hash`를 그대로 되돌려 보낸다.** API는 그 쌍이
    저장된 plan과 다르면 승인을 거절하므로(ADR-0019 §4), 화면을 열어둔 사이 plan이 교체됐다면
    조용히 새 plan을 승인하는 대신 거절된다. UI가 값을 새로 만들어 보내면 그 방어가 무력해진다.
  - **거절 사유는 열거값 select다.** API가 자유 문장을 거부하므로(ADR-0019 §8) 텍스트 입력을 두지
    않았다.
  - **조치 요청 응답은 "고쳐진다"는 뜻이 아니다.** 비조치 판정은 Job 없는 정상 200이므로 화면은
    `action`과 `manual_review_code`를 그대로 보여주고 진행 중인 것처럼 표시하지 않는다.
  - 검증 비교는 `verification_assessment_id`가 있을 때만 읽는다 — 검증 Assessment는 apply 완료
    뒤에 생기므로, 없는 것을 조회해 오류로 보여주지 않는다.
  - access token은 `App`이 소유한다. 화면마다 따로 들고 있으면 승인 화면과 Assessment 화면이 다른
    인증 상태로 갈릴 수 있다.
  - 검증: `npm --prefix apps/frontend run build`(`tsc -b && vite build`) 통과. 이 명령이
    `.github/workflows/frontend-checks.yml`의 필수 check와 같다.
  - **자체 리뷰에서 잡은 것:** 비교 표가 `FindingResolutionResult`가 발행하지 않는 `finding_id`로
    행 key를 만들고 있었다. `to_dict()`는 `resource_id`/`rule_id`/`rule_version`/`perspective`/
    `resolution`만 담는다. 런타임에 `undefined`가 되어 같은 perspective의 모든 행이 한 key로
    충돌한다. **`tsc`는 이것을 잡지 못한다** — 손으로 쓴 응답 `type`은 검사가 아니라 주장이다.
    그래서 field 이름을 고치는 것으로 끝내지 않고 `tests/contract/test_frontend_response_contracts.py`
    를 추가했다: frontend의 응답 type이 선언한 field가 대응 Contract `to_dict()`의 부분집합인지
    대조한다(적게 읽는 것은 정상, 없는 field를 읽는 것은 항상 버그). 되돌려 재현했을 때 이 검사만
    실패하고 `tsc`는 통과하는 것을 확인했다. 요청 body는 handler가 정확한 field 집합으로 400을
    내므로 조용히 실패하지 않아 제외했다.

- **평가 대상 Resource 범위를 S3 단독에서 EC2/RDS/ALB까지 4종으로 넓혔다.** Registry에 Rule을
  더하는 것만으로는 아무 것도 평가되지 않는다 — 파이프라인 네 곳이 S3로 하드코딩돼 있었고, 그 넷을
  모두 resource type 분배로 바꿨다. 확장 지점은 전부 allow-list이며 등록되지 않은 type은 빈 결과가
  아니라 실패다. **미배선 type이 조용히 통과하면 "위반 없음"과 구별되지 않는다** — 그것이 이 작업의
  단일 설계 원칙이다.
  - **B Registry:** Rule 8건을 새로 추가했다 — EC2 2건(`EC2-PUBLIC-IP-001`, `EC2-SG-INGRESS-001`;
    `EC2-EBS-ENCRYPT-001`은 M0부터 Registry에 있었다), RDS 4건(`PUBLIC`/`ACCESS`/`ENCRYPT`/
    `LOGGING`), ALB 2건(`HTTPS`/`LOGGING`). Control 매핑과 remediation 허용 범위를 함께 넣고, 4종
    범위 Profile `profile-multiresource-baseline`을 새로 게시했다 — 구성은 **S3 6 + EC2 3 + RDS 4 +
    ALB 2 = 15 Rule**이며 EC2 3건은 기존 `EC2-EBS-ENCRYPT-001`을 포함한다.
    **승인된 `profile-mvp-baseline`(S3 6 Rule, `v2`)은 건드리지 않았다** — 승인 경계를 거치지 않은
    Rule을 기존 Profile에 끼워 넣는 것은 `docs/POLICY_INGESTION.md`의 업로드→검증→승인 경계를
    우회하는 것이다. 어느 Profile로 평가할지는 고객 승인 시점에 정한다.
    `scripts/policy_source_digest.py --verify`가 원문 보유 환경에서 2 sources / 21 references 일치.
    신규 8건 중 `RDS-PUBLIC-001`만 `AUTOMATIC`이다 — ADR-0017의 두 기준(Rule 하나가 준수를 유일하게
    결정, 리소스 교체·데이터 손실 없음)을 모두 만족하는 것이 그것뿐이다. SG 규칙은 "필요한 IP/Port"를
    Rule이 결정할 수 없고, EBS/RDS 암호화는 리소스 교체를 요구하며, ALB HTTPS는 인증서라는 외부
    입력이, 로깅 3건은 대상 버킷 결정이 필요하다.
  - **`AWS::EC2::Volume`/`Snapshot`은 독립 평가 대상으로 열지 않았다.** EC2 read adapter가 인스턴스에
    연결된 볼륨과 보안 그룹 상태를 인스턴스 view에 함께 담으므로 인스턴스 하나를 읽으면 두 Rule의
    근거가 모두 생긴다. 별도 target type을 열면 같은 위반이 두 좌표에서 두 번 세어진다.
  - **Actual read adapter 3종 추가**(`AssumeRoleEc2/Rds/AlbResourceTool`)와 `resource_type` 분배
    composite(`ResourceTypeRoutingAwsResourceTool`). AssumeRole 자격증명 획득은
    `AssumeRoleReadSession` 한 곳으로 모았다 — 유형을 늘리는 것이 두 번째, 더 약한 자격증명 경로를
    만들 수 없어야 한다. 각 adapter는 응답을 **필드 allow-list로 투영**하므로 `UserData`, key 이름,
    tag 값, `MasterUsername`, `Endpoint`처럼 근거가 아닌 값은 모델 입력·저장 evidence에 들어가지
    않는다. 읽기 전용 두 operation과 customer/account scope guard는 공용 함수를 그대로 상속한다.
  - **Actual evidence loader를 유형별 locator allow-list로 일반화**했다(`assessment/actual.py`,
    구 `s3.py`). `PolicyContext.allows_evidence()`가 `aws:` namespace 전체를 허용하므로 locator를
    type 문자열에서 파생하면 검토되지 않은 리소스 형태도 항상 유효해 보인다. loader는 생성 시 한
    유형에 고정된다 — 평가기는 `resource_id`만 받으므로 유형이 고정되지 않으면 근거 문서와 Rule
    집합이 서로 다른 종류의 리소스를 설명할 수 있다.
  - **runtime 설정이 승인된 `(resource_type, resource_id)` 목록을 받는다.** Assessment record가
    그중 하나를 지목하면 해당 Initial resource로 좁히고, 목록 밖은 거부된다 — Assessment record는
    서버가 쓰지만 **승인 경계는 배포 설정**이며 두 좌표를 교차 확인하는 곳은 거기 하나다. 공개 API가
    만든 selector 없는 Assessment는 승인 목록 전체로 확장한다. 레거시 단일 `s3_bucket_id` 설정은
    그대로 유효하고, 두 설정 방식을 동시에 선언하는 target은 거부한다("무엇을 평가할 수 있는가"에
    답이 둘이 되므로).
  - **Terraform plan resource-id 투영에 4종을 추가**했다(ADR-0019 §1-a 보완). type별 identity 속성은
    그 type의 `AwsResourceQuery.resource_id`와 같은 값을 담는 속성이다: `aws_instance`→`id`,
    `aws_db_instance`→`identifier`(computed인 `arn`이 아니다), `aws_lb`/`aws_lb_listener`→ARN.
    ALB만 ARN인 이유는 리스너가 부모를 ARN으로만 지목할 수 있어 load balancer와 리스너가 한 어휘로
    모여야 하기 때문이다. **`aws_ebs_volume`/`aws_ebs_snapshot`/독립 `aws_security_group*`은 넣지
    않았다** — 그 id는 Finding 어휘가 아니어서 투영하면 readiness가 묻는 것과 다른 질문에 답한다.
    그 리소스만 바꾸는 plan은 `BLOCKED`로 남으며, 조용한 불일치가 아니라 문서화된 경계다.
  - 검증: ruff check/format, unit 889 / contract 182 / integration 9 / security 89 통과.
    security 회귀는 네 read adapter 전부에 대해 (1) 공개 method가 두 read operation뿐, (2) 선언한
    client Protocol과 소스에 변경 API 호출이 없음, (3) 다른 고객·계정·resource type 질의 거부를
    고정한다 — 어댑터 수가 셋 늘었으므로 경계를 어댑터별 관례가 아니라 회귀로 잡는다.

- **자체 리뷰에서 잡은 것 (같은 브랜치).** 네 건 모두 실행으로 재현했다. 공통점은 하나다 —
  **읽지 못한 것이 준수 상태와 구별되지 않는 경로.** 확장의 설계 원칙으로 그것을 적어놓고도, 정작
  세 곳에서 그 원칙을 어기고 있었다.
  1. **배포 후 Actual 재조회가 S3 어댑터에 못 박혀 있었다.** `DeploymentTarget.resource_types`는
     검증 없는 자유 문자열이고 그 값이 그대로 `AwsResourceQuery.resource_type`이 되는데, 기존
     테스트는 거기에 Terraform type 이름(`aws_s3_bucket`)을 넣고 있었다. 즉 프로덕션에서 그 조회는
     첫 질의부터 실패하거나(어댑터가 type을 거부) S3만 조용히 다시 읽는다. ADR-0020의 검증은 이
     재조회로 "위반이 사라졌는가"를 판단한다. **Assessment/Deployment 두 Worker가 같은 factory
     (`build_actual_resource_tool`)를 쓰게 하고 `resource_types`를 같은 어휘로 검증한다.**
     한쪽만 읽을 수 있는 유형이 있으면 검증이 그 유형을 건너뛴다.
  2. **목록 조회가 첫 페이지만 읽었다.** RDS는 기본 100건, ELBv2는 400건씩 답한다. 잘린 목록은
     "위반 없음"과 구별되지 않는다. 네 어댑터 전부 continuation token을 따라가게 하고, 페이지 상한을
     넘으면 부분 목록을 돌려주지 않고 실패하게 했다. ALB 리스너도 같다 — 리스너가 잘리면 평문 HTTP
     리스너가 보이지 않아 `ALB-HTTPS-001`이 잘못 `PASS`한다.
  3. **한 리소스의 부분 응답을 받아들였다.** 인스턴스가 붙인 볼륨 중 하나가 응답에서 빠지면 남은
     볼륨이 모두 암호화돼 있어 `PASS`처럼 보인다. 요청한 볼륨·보안 그룹이 응답에 모두 있는지 대조해
     거부한다.
  4. **읽을 수 있는 유형 목록을 세 곳에 손으로 베껴놨다.** 배포 게이트 스크립트에는 "애플리케이션
     패키지 없이 import돼야 하므로"라고 근거까지 적었는데, 그 스크립트는 이미 `agent.runtime`을
     import하고 있었다 — 근거가 사실이 아니었다. 목록은 read adapter registry 하나가 정하고
     (`ACTUAL_READ_RESOURCE_TYPES`) 나머지는 모두 그것을 읽는다. evidence scope 표와 어긋나면
     import 시점에 실패한다.
  - 함께 정리한 것: evidence 필드 allow-list를 Rule이 실제 인용하는 필드와 **정확히** 일치시켰다
    (MultiAZ·백업 보존·idle timeout 등 어떤 Rule도 묻지 않는 상태를 평가기에 주지 않는다).
    `ActualEvidenceLoader`의 `resource_type` 기본값(S3)을 없앴다 — "기본값 없음"을 원칙으로 적어놓고
    footgun을 하나 남겨두고 있었다. ALB evidence locator는 ARN 전체가 아니라 ARN의 resource 부분만
    담는다(130자 문자열을 모델이 그대로 되돌려줘야 근거가 인정된다).
    **실제 AWS 자격증명으로 EC2/RDS/ALB를 읽는 live 검증은 여전히 고객 sandbox 승인 대기다**
    (`Blocked` 참조). Golden Dataset을 신규 Rule로 넓히는 것은 C의 rubric 반복 평가가 필요하므로
    M4 live gate 입력으로 남긴다.
- 고객 sandbox의 Terraform 구성요소 검증을 완료했다. 고객 관리자 경계에서 state/lock backend, GitHub
  OIDC Plan/Apply Role 분리, Environment 변수·reviewer 승인, refreshed saved plan과 state
  lineage/serial 재검증 뒤 Apply까지 성공했다. 초기 부분 state/누락 S3 read 권한은 stale plan 재사용이
  아니라 새 plan으로 회복했으며, Organization custom OIDC subject는 repository 이름이 아닌 immutable
  ID를 포함할 수 있어 실제 claim과 같은 trust를 사용했다. 이 결과는 Foundation bootstrap, 플랫폼 API
  승인, Post-Deploy Verification 또는 release evidence가 아니다.

- PR #58–#62를 최신 `dev`에서 하나의 M4 통합 브랜치로 합쳤다. runtime/IaC API Gateway·SQS Worker
  배선, Admin audit read, Deployment plan/approval/read/completion 경계가 함께 존재한다. Deployment
  live plan runner는 승인 target의 `terraform-plan.yml` dispatch → exact GitHub run 재조회/폴링 →
  GitHub API artifact ZIP 검증으로 plan/state/binary를 회수한다. protected customer sandbox의
  secret, OIDC, demo repository 승인과 실제 실행 증적은 외부 승인 단계이며 fixture 성공을 release
  evidence로 표기하지 않는다.

- **세 Worker 큐 중 둘에 소비자가 없었다.** `docs/DESIGN.md`는 Assessment/Remediation/Deployment
  세 Worker Lambda를 아키텍처로 기술하지만 CloudFormation에는 Assessment 하나만 있었다. Deployment
  Worker는 composition root(`apps/backend/deployment/runtime.py`)까지 다 만들어 두고도 그것을
  실행할 Lambda와 event source가 없었고, Remediation Worker는 composition root조차 없었다. 큐에
  task가 쌓여도 소비자가 없으면 재시도만 반복하다 DLQ로 간다.
  - `apps/backend/remediation/runtime.py`를 Deployment Worker runtime과 같은 구조로 추가했다
    (`parse_tasks`/`run_tasks`/`lambda_handler`). 이 큐는 두 remediation command만 받는다 — 다른
    큐의 command가 흘러들면 Worker가 "지원하지 않는 command"로 실패하기 전에 파싱에서 막는다.
    큐를 잘못 지목한 것은 재시도로 나아지지 않는다.
  - `SnapshotSyncAction`으로 **`ACTUAL_SYNC` 경로를 완결 배선**했다. 대상은 평가된 snapshot
    commit이고 GitHub를 읽지 않는다 — 지금의 default branch head를 읽으면 평가 이후 merge된 다른
    변경까지 apply 대상에 들어오는데, 그건 아무도 이 Finding의 조치로 승인한 적 없는 코드다.
    `RemediationWorker._require_sync_result`가 요구하는 불변식도 정확히 이것이다.
  - **`TERRAFORM_PATCH`는 막았다.** 그 port는 승인된 snapshot에 바인딩된 Terraform 변경을 실제로
    생성해야 하는데 저장소에 있는 것은 변경 계획을 주입받는 fixture generator뿐이다. 고객 실행
    경로에서 그것을 쓰면 사람이 검토한 적 없는 patch가 고객 repository에 제안된다.
  - CloudFormation에 `RemediationWorkerFunction`/`DeploymentWorkerFunction`과 두 event source
    mapping을 추가하고, **모든 workflow 큐에 소비자가 있음**을 security 회귀로 고정했다. Deployment
    Worker의 live GitHub token 읽기 정책도 설정됐을 때만 만들어지도록 조건부로 붙였다.

- **`POST /deployments/{id}/approve`를 열었다** — M3 폐루프의 사람 승인 게이트다. 막고 있던 건
  결정 하나였고, ADR-0019에 §1-a 보완으로 확정했다: `plan_hash`는 "이 plan이 무엇인가"를 고정하지만
  C readiness가 묻는 세 가지(refresh 여부, 파괴적 변경, **그 plan이 건드리는 AWS 리소스**)를 담지
  않는다. `PlanReadinessInput`은 D를 producer로 지목했지만 산출 규칙도 저장 경로도 없었다.
  - `PlanSummary`(`refreshed`/`has_destructive_changes`/`mapped_resource_ids`)를
    `PlanExecutionResult`에 싣고 plan facts와 함께 저장한다. 승인은 plan보다 나중 invocation이라
    durable해야 한다.
  - **`mapped_resource_ids`는 resource type별 identity 속성의 허용 목록으로 투영한다.** Terraform
    address와 Finding의 `resource_id`는 다른 어휘라 잇는 규칙이 필요했다. 허용 목록인 이유는 plan
    투영과 같다 — provider가 identity처럼 보이는 필드를 늘렸다고 "이 plan이 어느 리소스를 건드리는가"의
    답이 조용히 바뀌면 안 된다. S3 범위에서는 모두 `bucket`이고, 그 값이 곧 `Finding.resource_id`다.
    허용 목록 밖 type은 아무것도 기여하지 않아 readiness가 `BLOCKED`가 된다(fail-closed — 관련성을
    확인하지 못한 plan을 승인 가능으로 표시하지 않는다). `after` → `before` 순으로 읽어 삭제되는
    리소스도 자기 id를 밝히고, 계산값은 추측하지 않고 건너뛴다.
  - **어댑터는 회수한 canonical 바이트로 `plan_hash`를 다시 계산해 대조한 뒤에만 요약을 만든다.**
    승인 게이트는 hash를 재검증하지 요약을 재검증하지 않으므로, 요약이 다른 plan을 설명하면 그 뒤로는
    아무도 잡지 못한다.
  - `DynamoDbDeploymentPlanReader`가 저장된 plan + 요약 + Worker context로 readiness를 read 시
    파생한다. **판정은 저장하지 않는다** — 낡은 `READY_FOR_APPROVAL`은 C가 막았을 plan을 승인
    가능으로 보여준다. 상태 조회의 readiness도 같은 reader의 같은 판정을 써서 "승인 대기" 표시와
    실제 승인 가능 여부가 어긋나지 않는다.
  - `refreshed`의 근거는 "어느 workflow가 돌았는가"다. terraform 플래그를 사후에 관측할 방법은
    없고, 승인 대상 plan은 refreshed saved plan을 만드는 승인 template이 만든 것이며 apply 직전
    재검증이 run의 workflow path를 대조한다.
  **이로써 Deployment API 다섯 route가 모두 durable 배선을 갖췄다**(생성·조회·검증조회·승인·거절).
- M4 A 관측·비용 조회를 HTTP 경계에 붙였다(`GET /deployments/{id}/observability`, Admin 전용).
  `DemoRunObservabilityService`는 정의만 있고 route가 없었다. live CloudWatch/CloudTrail/Cost
  Explorer adapter는 아직 없으므로 composition root는 source를 `None`으로 두고 route는 404로
  남는다 — 주입할 source가 없는 것을 항상 실패하는 endpoint(500)로 보여주지 않는다. 남은 조각은
  live metric source 하나이고, 그건 실제 AWS 자격 증명이 있어야 동작·검증된다.
- **A/D 공유 계약의 A 몫(apply 완료 Event 예약 write)을 구현했다**(ADR-0019 §7, DATABASE.md
  "완료 Event 경계"). D는 예약 item에서 `run_reference`를 읽어 검증·확정하는 경로를 이미 갖고
  있었지만 예약을 쓰는 쪽이 없어 live `APPLY_COMPLETED`가 영원히 fail-closed였다.
  `ApplyCompletionService` + `DynamoDbDeploymentCompletionStore` + `apply_completion_handler`
  (EventBridge 진입점)로 닫았다. Event에서 읽는 값은 `deployment_id`/`run_id` **두 좌표뿐**이고
  conclusion·commit·plan digest 같은 주장은 읽지도 저장하지도 않는다 — Event는 신호이지 정본이
  아니며, 저장하는 순간 검증되지 않은 주장이 `derive_deployment_status()`의 입력이 된다.
  EVENT 예약 + Job revision bump + `APPLY_COMPLETED` outbox는 하나의 조건부 transaction이다.
  **소유 고객은 Event가 아니라 저장에서 해석한다** — `DEPLOYMENT#` item에
  `GSI1PK = DEPLOYMENT#{deployment_id}`를 채우고 Job 해석과 같은 방식으로 id를 푼 뒤 그 customer
  scope로 record를 다시 읽는다. payload에서 소유자를 받으면 Event를 만들 수 있는 누구든 남의 Job을
  재개시킬 수 있다. 이미 terminal인 Job은 되살리지 않는다(사람이 끝낸 결정을 Event가 뒤집지 않는다).
  CloudFormation에 `ApplyCompletionFunction`과 EventBridge rule/permission을 추가하고, 이 Lambda가
  유일한 DynamoDB write 경로임을 security 회귀로 고정했다.
- M3 A 조회·검증 reader를 붙여 `GET /deployments/{id}`와 `GET /deployments/{id}/verification`이
  실제로 답하게 했다(같은 브랜치). `DynamoDbDeploymentFactsReader`는 `DEPLOYMENT#{id}` SK prefix
  query 한 번으로 승인·거절·dispatch·EVENT item을 모두 읽고 Job과 합쳐 `DeploymentFacts`를 만든다.
  apply 결론은 D가 재조회로 확정한 `VERIFIED` EVENT item에서만 온다 — dispatch 영수증과 예약된
  `PENDING_VERIFICATION` item은 "실행 중"일 뿐 결론이 아니다(ADR-0019 §5·§7).
  `DynamoDbComparisonInputReader`는 두 Assessment를 complete `ComparisonAssessment`로 만든다.
  **`model_profile_id`/`rubric_version`은 결과에서 파생한다** — Initial Assessment는 그 pin을 item에
  저장하지 않는 것이 규칙이고(pin은 검증 전용, ADR-0020 §3), 원본 쪽은 파생 말고 근거가 없으며,
  양쪽을 같은 방법으로 읽어야 비교 축이 한 종류가 된다. 결과의 값은 "이 값으로 평가했다"는 사실이고
  비교가 필요로 하는 건 그쪽이다. 한 Assessment가 Profile/rubric을 섞고 있으면 비교 전에
  fail-closed한다. 상태 화면의 검증 판정은 검증 조회와 **같은** reader를 써서 둘이 어긋나지 않게 했다.
- **`POST /deployments/{id}/approve`는 의도적으로 fail-closed로 남겼다.** 승인 plan reader는 plan과
  함께 C의 readiness 판정을 돌려줘야 하는데, 그 판정에 필요한 D의 plan 요약(`refreshed`,
  `mapped_resource_ids`, destructive 여부)을 `PlanExecutionResult`가 영속화하지 않는다.
  `PlanReadinessInput`은 D를 producer로 지목하지만 저장 경로가 없다. 같은 이유로 상태 파생의
  readiness도 `None`이다 — 근거 없이 `READY_FOR_APPROVAL`을 넣으면 C가 막았을 plan(예: destructive
  변경)이 "승인 대기"로 보인다. **다음 작업은 D가 `PlanExecutionResult`에 plan 요약을 실어 plan
  facts와 함께 저장하는 것**이고, 그게 되면 plan reader와 approve가 같이 열린다.
- M3 A Deployment 생성 경로를 실제로 살렸다(`feature/m3-a-deployment-readers`, base=dev). 문서는
  생성이 "durable 배선 완결"이라고 적었지만, composition root가 `DeploymentApiService(sources=...)`
  를 넘기지 않아 `POST /remediations/{id}/deployments`는 프로덕션에서 항상
  `deployment creation dependencies are not configured`로 죽고 있었다. 원인은 그 아래에 하나 더
  있었다 — **C Remediation Worker 결과를 저장하는 DynamoDB 구현이 아예 없었다**
  (`RemediationResultStore`의 실구현이 테스트 fake뿐). 결과가 저장되지 않으니 생성이 확인해야 할
  전제조건("worker 결과가 존재") 자체를 만들 수 없었다. 세 조각을 넣어 경로를 닫았다:
  (1) `DynamoDbRemediationResultStore` — `REMEDIATION#{id}` item에 `result`를 conditional
  update로 한 번만 채운다. plan facts와 같은 관례(멱등 흡수, 덮어쓰기 불가)이고, 별도 `#RESULT`
  item으로 나누지 않은 이유는 생성이 decision과 결과를 **함께** 봐야 하기 때문이다 — 한 item이면
  단일 strongly-consistent get이고, 두 item이면 decision만 보이는 중간 상태를 읽는다.
  (2) `DynamoDbDeploymentSourceReader` — decision·worker 결과·source Assessment를 한 번에 읽고
  대상 commit을 정한다. `ACTUAL_SYNC`는 저장된 sync target commit이 곧 대상이라 GitHub read가
  없고, `TERRAFORM_PATCH`는 ADR-0019 §3대로 **merge된 default branch commit**이 대상이다.
  merge 전이면 도달 불가로 표시하고 commit을 지어내지 않는다. `source_assessment_id`가 없으면
  검증을 정확한 before-state에 묶을 수 없으므로 fail-closed한다.
  (3) `DeploymentCommitResolver` port(D 소유)와 `LiveDeploymentCommitResolver` — default branch
  이름을 repository에서 읽고(설정을 믿지 않는다), patch에서 결정적으로 유도한 head branch로 PR을
  찾고, merge commit이 default branch에서 **여전히** 도달 가능한지 compare로 확인한다. `merged_at`만
  보면 merge 뒤 revert된 commit을 배포하게 된다. 미설정 배포에서는 `TERRAFORM_PATCH`만 fail-closed
  되고 `ACTUAL_SYNC` 배포는 GitHub 없이 그대로 동작한다.
  CloudFormation에 `DeploymentRuntimeJson`/`DeploymentGitHubSecretArns` 파라미터와 all-or-none
  Rule, API Lambda 환경 변수, 조건부 secret 정책(와일드카드 아님)을 추가하고 security 회귀로
  고정했다. 문서(CONTRACTS/DATABASE) 동기화. **남은 reader 3종**(`DeploymentPlanReader`/
  `DeploymentFactsReader`/`ComparisonInputReader`)은 approve/get/verification을 여는 후속이다.

- M2 A Admin 감사 이력 조회(`GET /audit-events`)를 구현했다(`feature/m2-a-audit-events`, base=dev).
  일곱 writer가 이미 `AUDIT#{occurred_at}#{event_id}` 한 SK 규약과 `event_type` 한 필드명을 쓰고
  있어, 조회는 writer별 분기 없이 고객 partition의 `AUDIT#` prefix를 SK 역순으로 읽는 단일 query다
  (scan 없음). `AuditEvent`/`AuditEventPage` read projection은 네 identity 필드와 writer별 payload
  `details`만 담고, `audit_event_details()`가 DynamoDB key·GSI·`entity_type`·`version` 같은 저장
  bookkeeping을 걷어낸다. 범위는 항상 호출자의 verified `custom:customer_id`이며 조회 대상 고객을
  query로 지정할 수 없다. cursor도 Client가 되돌려주는 값이므로 customer scope를 검증해 다른
  고객 이력으로 넘어가는 것을 막는다. 알 수 없는 `event_type`은 빈 페이지가 아니라 400이다.
  `AuditEventType`에 ADR-0019가 합의한 다섯 값(`APPLY_DISPATCHED`/`APPLY_COMPLETED`/`APPLY_FAILED`/
  `POST_DEPLOY_VERIFIED`/`MANUAL_RECONCILIATION_REQUIRED`)을 먼저 넣어 어휘를 닫았다 — 조회가 한
  vocabulary만 보게 하려면 종류 집합이 writer보다 먼저 고정돼야 한다. `GetAuditEventsRoute`를
  CloudFormation에 JWT authorizer와 함께 선언하고, route 누락을 security 회귀로 고정했다.
  문서(API/CONTRACTS/DATABASE) 동기화. **M2 A는 이로써 남은 항목이 없다.**
- D Deployment Worker runtime의 설정 검증 순서를 고쳤다(`fix/deployment-runtime-env-validation-order`).
  `lambda_handler`가 `_metadata_table()`을 `_required_env("METADATA_TABLE_NAME")`보다 먼저 평가해
  필수 환경 변수 누락이 fail-closed된 설정 오류 대신 boto3 `NoRegionError`로 새어 나갔고, 그 때문에
  `dev`에서 unit 테스트 1건이 실패하고 있었다. 검증을 AWS client 생성보다 먼저 끝내고,
  `_required_env()`가 누락된 이름을 밝히는 `DeploymentRuntimeError`를 올리도록 바꿨다. live mode
  테스트도 실제 동작에 맞게 다시 썼다 — 기존 테스트는 "설정이 유효해 plan I/O에서 멈춘다"고
  주장했지만 실제로는 그보다 먼저 멈추고 있었다.
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

- 2026-09-03 M1 A/C storage 항목 종료 확인(신규 구현 아님, `Next`에 낡은 채로 남아 있었다):
  `DynamoDbEvaluationResultStore`가 immutable 결과·Finding write와 **같은
  `transact_write_items`**에서 `ASSESSMENT#{id}#PLAN`의 `completed_evaluations`를
  `ADD … :one` + `completed_evaluations < planned_evaluations` 조건으로 갱신하고,
  Assessment 조회는 `cursor`/`findings_cursor`로 results와 findings를 각각 페이지네이션한다
  (`apps/backend/assessment/results.py`, `reporting.py`, `api/handler.py`).
- Finding→Remediation 폐루프를 실제 sandbox에서 완주하고 원래 AI-주도 설계를 복원했다. AI가 rule
  적용성을 `OUT_OF_SCOPE`로 판정(plan 분모는 코드가 고정, ADR-0002 §Rule applicability mechanism),
  Bedrock Remediation Agent가 finding+snapshot에서 최소 Terraform patch를 생성(`UnavailablePatchAction`
  차단 대체), LangGraph를 Lambda Layer로 도입하고 Parent Orchestrator(`POST /orchestrate`)가 자연어를
  PolicyQA/ASSESSMENT/REMEDIATION/DEPLOYMENT로 라우팅(판단·제안만, 실행 권한 없음, ADR-0012). remediation
  outbox를 remediation 큐로 라우팅하는 dispatch 버그도 수정. 라이브 검증: assessment 18/18·findings 12,
  S3-PUBLIC IAC finding → decision TERRAFORM_PATCH → worker가 실제 patch(changed_paths=[main.tf],
  base_commit 바인딩) 생성. 상세 인수인계와 계정·토큰·선행조건은 `docs/SESSION-HANDOFF-2026-09-03.md`.
  Secret 값은 저장소에 두지 않으며, 실제 sandbox 버킷은 E2E용 insecure 상태라 복원이 필요하다.

- 고객 sandbox Terraform component test: 별도 고객-owned 테스트 repository에 최소 secure S3
  baseline, Plan/Apply workflow, canonical plan hash helper와 provider lock을 설치하고, 고객 관리자
  승인 아래 OIDC plan → protected Environment 승인 → saved-plan apply를 성공시켰다. state artifact나
  자격 증명·고객 식별자는 이 저장소에 기록하지 않는다. 부분 생성/state write 실패는 IAM 최소권한의
  state object path와 provider read 범위를 보완한 뒤 새 plan으로 회복했다. 이 성공을 M1/M4 release
  증적으로 사용하지 않는다.

- 고객 Terraform/AWS 리소스가 아직 없는 sandbox 시작 단계를 위해 고객 관리자용 사전 준비 요청서와, 별도 customer-owned repository에만 복사하는 secure S3 baseline Terraform starter를 추가했다. 템플릿은 state backend·Plan/Apply OIDC Role·protected Apply Environment·workflow 수동 설치·`.terraform.lock.hcl` 생성/commit을 요구하며, 이 저장소에는 고객 account ID·role ARN·state backend·credential·Terraform state를 저장하지 않는다. 실제 customer resource 생성·workflow 실행은 승인된 고객 관리자만 수행한다.

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

- **고객 sandbox의 다음 단계:** Terraform component test는 Foundation과 분리된 사전 검증으로
  성공했으므로, 고객 관리자가 `m1-customer-bootstrap.yaml`을 실행하고 Foundation 배포용 두
  protected GitHub Environment·OIDC Role·artifact binding을 준비한다. 그 뒤 Foundation을 배포하고
  platform runtime에 승인된 customer repository/read role을 연결해, 플랫폼 Deployment 생성 → plan →
  플랫폼 Human Approval → apply → Post-Deploy Verification E2E를 실행한다. 이전 Terraform test의
  public repository·식별자·state를 Foundation/release evidence에 재사용하지 않는다.

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
- ~~**M3 A (ADR-0020 파생분의 남은 절반)**~~ **해소됨 (2026-09-04 확인):** `phase`/`source_assessment_id`/
  `deployment_id`는 `apps/backend/repositories/dynamodb.py`의 Assessment item write에 들어갔고,
  `assessment/runtime.py`의 `AssessmentPhase.INITIAL`은 하드코딩이 아니라 `phase` 없는 legacy 레코드의
  fallback이다. `GET /deployments/{deploymentId}/verification`은 `apps/backend/api/deployments.py`에서
  `compare_post_deploy_assessments()`에 배선돼 있다.
- ~~**M3 A endpoint 배선 (Contract 동결 이후)**~~ **해소됨:** `POST /remediations/{id}/deployments`,
  `GET /deployments/{id}`, `GET /deployments/{id}/verification`, `POST /deployments/{id}/reject` 전부
  handler branch + API Gateway route로 배선됨(`docs/API.md` M3 표). 라이브 확인만 재배포 뒤로 남는다.
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
- **Frontend 남은 화면:** Admin 감사 이력 조회(`GET /audit-events`)와 관측·비용 조회
  (`GET /deployments/{id}/observability`) 화면. 정책 원문 업로드·후보 추출·승인·Profile 게시·사용자 관리·
  `/scope`·`/orchestrate` 챗봇은 #72 콘솔 재설계로 배선됐고, Assessment 조회·조치 요청·배포 승인/거절·
  Post-Deploy 비교는 그 전에 배선됐다.
- **EC2/RDS/ALB Golden Case:** Golden Dataset은 여전히 S3 6 Rule × 3관점 = 36 case다. 확장한 9 Rule
  (EC2 3 / RDS 4 / ALB 2)은 품질 게이트 없이 평가되므로, C의 rubric 반복 평가와 함께 case를 추가해야
  한다. fixture만 늘리는 것은 근거가 아니다 (ADR-0021/0022).
- M4 C external evidence: A/D가 protected customer sandbox의 Post-Deploy artifact set과 실제 Demo
  실행을 제공하면 `docs/M4-GOLDEN-RELEASE-GATE.md` 절차로 18 Case × 5 run private observation
  bundle을 만들고 `scripts/evaluate_m4_golden_release_gate.py --observations ...`로 sanitized report를
  생성한다. Dry-run의 `EXTERNAL_EVIDENCE_REQUIRED`나 fixture 결과는 release 통과 근거가 아니다.
  같은 `execution_id`와 repository/deployment/artifact digest로 A 관측·비용, D plan/apply 증적을
  결합한다 (ADR-0022).
- ~~M1 A: 업로드 세션 / 승인·Profile 게시 API 배선 / 업로드→정규화→승인→Profile→Assessment 통합
  테스트~~ **해소됨 (2026-09-04 확인):** 업로드 세션·상태 조회·`/process`·`/approve`·
  `POST /policy-profiles`·목록·삭제가 전부 배선됐고(`docs/API.md` "Customer policy ingestion"),
  ADR-0023 authoring worker가 후보를 채운다. 통합 테스트는
  `tests/integration/test_policy_ingestion_lifecycle.py`·`test_policy_authoring_to_assessment.py`.
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

- ~~M1 actual sandbox validation: required reviewer·`M1_ASSESSMENT_MODE`·M1 Secret 3개 미설정~~
  **해소됨 (2026-09-03, `docs/SESSION-HANDOFF-2026-09-03.md` §2):** 두 Environment 모두 필수 리뷰어
  게이트가 있고 deploy Environment에 `M1_ASSESSMENT_MODE=live`와 Secret 3개가 설정됐다. Foundation 스택은
  `d687a00`(#71)까지 배포됐고 라이브 Initial Assessment·remediation patch 생성까지 완주했다(§4).
  남은 것은 `dev` HEAD(#72 포함) 재배포 — 그 전에 bootstrap 스택을 새 템플릿으로 갱신해야 한다(§6 H).
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
  **planned 집합 저장과 Assessment 상관관계 영속화는 모두 해소됐다** — `ASSESSMENT#{id}#PLAN` item의
  `planned_coordinates` 속성과 `DynamoDbAssessmentReportStore.get_planned_evaluations()` 조회가
  들어갔고, `phase`/`source_assessment_id`/`deployment_id`와 검증 pin 3종
  (`model_profile_id`/`rubric_version`/`policy_profile_version`)도
  `apps/backend/repositories/dynamodb.py`의 Assessment item write에 들어갔다(verification만 pin을
  싣는다 — ADR-0020 §3). 남은 차단 요인은 **D의 apply 후 Actual 재조회 입력뿐**이며, 그것은 고객
  sandbox 자격 증명·protected Environment 대기다.
  *Owner:* D (+ B exception read). *Blocks:* live M3 verification endpoint와 M4 customer runtime report,
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

- [x] **A — Platform/Backend:** Remediation/Deployment API, Job 재개, Approval 상태 전이와 Audit Log *(B policy gate, customer exception registration/read, canonical decision/context/Job/Outbox/audit transaction, 200/202 public response, authoritative revision work reader, Admin `GET /audit-events` 감사 이력 조회까지 구현 완료; customer runtime wiring 대기)*
- [x] **B — Policy/Governance Boundary:** Remediation 허용 범위·예외·Manual Review 정책 제공 *(Rule version 단위 허용 범위 Registry, 만료되는 고객 예외, 조치 유형·Manual Review 사유 판정 구현 완료. 예외 등록·저장 API는 A, Patch 생성 연결은 D)*
- [x] **C — AI Evaluation & Agent Orchestration:** Finding 근거 기반 Remediation Context, C-owned revision-bound Remediation Worker, Deployment Readiness 평가 *(duplicate strategy 제거, stored decision command matrix와 injected Patch/Sync ports, stale/mismatch fail-closed 검증 완료)*
- [ ] **D — Remediation/GitHub/Deployment:** Patch/Diff, GitHub PR, OIDC Terraform Plan, `commit_sha`/`plan_hash` 생성 *(`plan_hash`의 대상 바이트, destructive 판정, `mapped_resource_ids` 투영은 ADR-0019 §1·§1-a로 확정돼 `packages/contracts/terraform_plan.py`에 공용 함수로 구현됨. `PlanSummary` 영속화로 승인 경로도 열렸다. GitHub write 제안 경계(`ProposedPullRequest`)까지 완료. 남은 조각은 live GitHub branch/commit/PR adapter와 Remediation Worker customer runtime 배선)*
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
  네트워크가 있어야 검증된다 — 구현 자체(`workflow_dispatch` → run 재식별 → artifact 축소)는 들어가 있고
  `DEPLOYMENT_RUNTIME_JSON`이 없으면 조립 단계에서 fail-closed다. A의 `#EVENT` 예약 write와
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
