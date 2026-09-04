# Session Handoff — Finding→Remediation 폐루프 + AI rule 선택 + LangGraph (2026-09-03)

이 문서는 다음 개발자/Agent가 이 작업을 이어받기 위한 저장소-공유 인수인계다. **Secret 값은
담지 않는다.** 토큰·키는 이름/ARN과 재발급 방법만 기록한다(AGENTS.md, secret 미포함 규칙).

브랜치: `docs/customer-sandbox-onboarding` (base `dev`). E2E 성공 후 dev로 병합 예정.
개인 로컬 상세 진행은 `.ai/HANDOFF.md`(Git 제외)에도 있다.

---

## 1. 무엇을 달성했나 (원래 비전 복원)

정본 설계(ADR-0002, ADR-0012, ADR-0018)의 "AI가 적용 rule을 선택하고, patch를 생성하고,
LangGraph Parent가 자연어를 라우팅한다"를 복원하고 **실제 sandbox에서 finding→remediation
patch 생성까지 라이브 폐루프를 완주**했다.

커밋 순서(이 브랜치):

| 커밋 | 내용 |
| --- | --- |
| `104f199` | RemediationContext DynamoDB reader + `POST /findings/{id}/remediations` API 배선 + auth/평가 수동 드리프트를 Foundation 템플릿에 반영 |
| `e1d0e5c` | remediation outbox task를 remediation 큐로 라우팅(CommandRoutingWorkflowDispatcher). 이전엔 assessment 큐 dispatcher가 GENERATE_REMEDIATION을 거부해 outbox가 PENDING으로 영구히 남던 버그 |
| `5254d87` | **AI rule 선택 복원**: `bedrock.py` `_SYSTEM_PROMPT`가 모델에게 rule 적용성 판정 권한 부여(미적용은 `OUT_OF_SCOPE`). plan 분모는 코드가 넓게 고정 유지(ADR-0002 §Rule applicability mechanism) |
| `d254ecc` | **LangGraph Lambda Layer** 패키징(`scripts/build-langgraph-layer.sh`, 템플릿 `LangGraphLayer`, deploy workflow가 Layer 빌드·업로드) |
| `feee518` + `af57307` | **Parent Orchestrator + Policy Q&A**: `contracts/orchestration.py`, `agent/agents/parent_orchestrator.py`(Bedrock 라우팅), `agent/graphs/parent_graph.py`(LangGraph StateGraph), `api/orchestration.py` + `POST /orchestrate` + `ORCHESTRATE` action |
| `d6ff2da` | **Bedrock Remediation patch Agent**: `remediation/bedrock.py` `BedrockPatchGenerator`가 `UnavailablePatchAction`(fail-closed 차단)을 교체. finding+snapshot→최소 Terraform patch |

검증: 오프라인 Unit 867 / Contract 174 / Security 79 / Integration 11 OK, ruff clean.
라이브: 재배포 성공(스택 UPDATE_COMPLETE), 폐루프 완주(§4).

### 아키텍처 경계 (지키면서 구현함)
- **Rule 적용 선택 = AI**(OUT_OF_SCOPE), **action 선택(PATCH/SYNC/MANUAL/SUPPRESSED) = B 정책**(결정적,
  `fixtures/rules/remediation.json`), **patch 내용 = AI(Bedrock)**. Coverage 분모는 코드가 고정.
- **Parent는 판단·제안만**: PolicyQA 직접 답변 또는 워크플로 intent+selector 제안. Job 생성·scope
  검증·승인·AWS 변경 권한 없음. Backend가 JWT로 selector 검증 + 사용자 확인 후 워크플로 시작(ADR-0012).
- LangGraph는 문서상 프레임워크지만 실제 LLM 호출은 boto3 `bedrock-runtime.converse` 직접 호출이다
  (Assessment 선례). LangGraph는 오케스트레이션 그래프 껍데기.

---

## 2. AWS / GitHub 연결 (계정·역할·토큰)

### AWS sandbox
- Account **369676914736**, region **us-east-1**.
- 세션마다 **MFA 세션 재발급** 필요(`kosa03` 사용자는 `kosa-edu-mfa-pol` explicit deny로 MFA 없이는
  CloudFormation/Bedrock/IAM 대부분 거부). 결과를 `mfa` 프로필로 저장(약 12h 유효):
  ```bash
  aws sts get-session-token \
    --serial-number arn:aws:iam::369676914736:mfa/<your-mfa-device> \
    --token-code <MFA 6자리> --duration-seconds 43200
  aws configure set aws_access_key_id <AK> --profile mfa
  aws configure set aws_secret_access_key <SK> --profile mfa
  aws configure set aws_session_token <ST> --profile mfa
  aws sts get-caller-identity --profile mfa   # 확인
  ```
  이후 모든 aws 명령에 `--profile mfa --region us-east-1`.

### Foundation 스택 `kosa-governance-sandbox` (UPDATE_COMPLETE)
- API: `https://8cimz0a9n9.execute-api.us-east-1.amazonaws.com` (id `8cimz0a9n9`)
- Cognito UserPool `us-east-1_yHRFQCFIH`, Client `66ektgjk0aan5nb8ah7789f160`,
  HostedUI `kosa-governance-sandbox-369676914736.auth.us-east-1.amazoncognito.com`
- DynamoDB `kosa-governance-sandbox-metadata`
- SQS: `-assessment` / `-remediation` / `-deployment` (+ 각 `-dlq`)
- S3: `-artifacts-369676914736`, `-audit-369676914736`, `-lambda-code-369676914736`
- Lambda: `-api`, `-outbox-sweeper`, `-apply-completion`, `-assessment-worker`,
  `-remediation-worker`, `-deployment-worker`, `-pretoken`
- **LangGraph Layer**: `arn:aws:lambda:us-east-1:369676914736:layer:kosa-governance-sandbox-langgraph:1`
  (api + 3 worker에 연결). 새 배포마다 content-addressed로 갱신될 수 있음.

### GitHub
- 플랫폼 repo: `awsproject-team1/kosa-aws-1`
- 고객 IaC repo: `awsproject-team1/test`
- 배포 role: `arn:aws:iam::369676914736:role/kosa-governance-sandbox-github-deploy`,
  CFN 실행 role: `arn:aws:iam::369676914736:role/kosa-governance-sandbox-foundation-cfn`
  (스택 `kosa-governance-sandbox-bootstrap-roles`, 템플릿 `infrastructure/cloudformation/m1-customer-bootstrap-roles.yaml`).
  OIDC immutable subject: `repo:awsproject-team1@320848962/kosa-aws-1@1350256257`.
- GitHub Environment(플랫폼 repo): `customer-sandbox-artifact`(EXPECTED_AWS_ACCOUNT_ID),
  `customer-sandbox-deploy`(EXPECTED_AWS_ACCOUNT_ID, M1_ASSESSMENT_MODE=live,
  M1_ASSESSMENT_RUNTIME_JSON/SECRET_ARNS/READ_ROLE_ARNS). 둘 다 필수 리뷰어 승인 게이트.
- 고객 repo `test`: `customer-terraform-plan`/`customer-terraform-apply` Environment,
  TerraformPlanRoleKosaTest/TerraformDeploymentRoleKosaTest.

### GitHub App (평가 대상 IaC read + remediation PR용)
- slug `awsproject-team1-kosa-reader`, **App ID 4813609**, **installation 158679675**
- `awsproject-team1/test`에만 설치. perms: contents:write, pull_requests:write, metadata:read.
- private key: 로컬 `./awsproject-team1-kosa-reader.2026-09-02.private-key.pem` (**gitignore, 저장소에 없음** — 보관자에게 요청).
- **(2026-09-04부터) worker가 token을 스스로 발급한다.** secret
  `kosa-governance-sandbox/m1/github-token`에 token 대신 App 자격 JSON을 넣는다:
  ```json
  {"app_id":"4813609","installation_id":"158679675","private_key":"-----BEGIN RSA PRIVATE KEY-----\n..."}
  ```
  `GitHubAppTokenProvider`가 이 자격을 읽어 App JWT를 만들고 installation token을 받아
  만료 5분 전까지 재사용한다. 서명은 표준 라이브러리만으로 한다(함수 ZIP은 third-party
  의존성을 갖지 않고 Layer에도 `cryptography`가 없다). secret에 예전처럼 token 문자열이
  들어 있으면 그대로 쓰므로, secret 교체와 배포 순서는 자유롭다 — 단 **배포가 먼저**여야
  한다. 예전 코드는 JSON을 token으로 착각해 401을 낸다.
- 아래는 사람이 직접 발급하던 예전 절차다. token은 **1시간 만료**:
  ```bash
  APP_ID=4813609; INSTALL_ID=158679675; PEM=./awsproject-team1-kosa-reader.2026-09-02.private-key.pem
  b64url(){ openssl base64 -A | tr '+/' '-_' | tr -d '='; }
  now=$(date +%s); h=$(printf '{"alg":"RS256","typ":"JWT"}'|b64url)
  p=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' $((now-60)) $((now+540)) "$APP_ID"|b64url)
  sig=$(printf '%s.%s' "$h" "$p"|openssl dgst -sha256 -sign "$PEM"|b64url); jwt="$h.$p.$sig"
  curl -s -X POST -H "Authorization: Bearer $jwt" -H "Accept: application/vnd.github+json" \
    "https://api.github.com/app/installations/$INSTALL_ID/access_tokens"
  # 받은 token을 Secrets Manager에 갱신:
  aws secretsmanager put-secret-value --profile mfa --region us-east-1 \
    --secret-id kosa-governance-sandbox/m1/github-token --secret-string <token>
  ```

### Secrets Manager (값 아님, 이름만)
- `kosa-governance-sandbox/m1/github-token` — installation token 보관(**1h 만료, 위 방법으로 재발급**)
- `kosa-governance-sandbox/m1/aws-external-id` — read role trust의 ExternalId
- AWS read role: `arn:aws:iam::369676914736:role/kosa-governance-sandbox-m1-read`
  (workflow-runtime role + ExternalId 조건, `tfsbx-...` 버킷 S3 read-only)

### 테스트 사용자 (Cognito)
- `e2e-admin@example.com`, Admin 그룹, `custom:customer_id=kosa-sandbox`,
  sub `a4f804b8-f081-705d-5423-260a20ab8058`. **비밀번호는 저장소에 두지 않음**(보관자에게 요청).
- CLI 토큰 발급(USER_PASSWORD_AUTH 활성):
  ```bash
  aws cognito-idp initiate-auth --profile mfa --region us-east-1 \
    --auth-flow USER_PASSWORD_AUTH --client-id 66ektgjk0aan5nb8ah7789f160 \
    --auth-parameters USERNAME=e2e-admin@example.com,PASSWORD='<pw>' \
    --query 'AuthenticationResult.AccessToken' --output text
  ```

---

## 3. 평가 대상 (M1 RUNTIME)
- customer_id=`kosa-sandbox`, repository_id=`test-s3-sandbox`, policy_profile_id=`profile-mvp-baseline`
- github_repository=`awsproject-team1/test`
- **commit=`b283b6b5a41945349f64c41036870a5507c264f7`** (intentional insecure S3, PR #1로 머지)
- s3_bucket_id=`tfsbx-20260903-7f3a-a91c`, aws_account_id=369676914736
- M1 RUNTIME_JSON은 `customer-sandbox-deploy` Environment secret `M1_ASSESSMENT_RUNTIME_JSON`에 있고
  로컬 사본은 `.ai/e2e-secrets/m1_runtime.json`(Git 제외). commit 바꾸려면 secret 갱신 후 재배포.

---

## 4. 라이브에서 검증된 폐루프 (증거)
1. `POST /assessments`(repository_id=test-s3-sandbox, policy_profile_id=profile-mvp-baseline)
   → assessment `asm-51b74ee0-...` coverage 18/18, **findings 12** (S3-PUBLIC IAC+ACTUAL FAIL 포함,
   provenance commit=b283b6b/eval 채워짐).
2. `POST /findings/finding-04ec79d05d823c7b06c12654/remediations` → **decision TERRAFORM_PATCH**
   (S3-PUBLIC=AUTOMATIC), remediation `rem-a573b16a-...`, job QUEUED.
3. remediation-worker가 **BedrockPatchGenerator로 실제 patch 생성**:
   result.patch = {finding_id, base_commit=b283b6b, changed_paths=[main.tf], REMEDIATION_PATCH,
   content_sha256=afdf2da75960e473...}. (이전 세션엔 `PatchGenerationUnavailableError`로 막혔음)

배포는 `Deploy M0 Foundation` workflow(`gh workflow run`)로 2게이트(artifact→deploy) 승인.
Layer 빌드+업로드는 prepare-artifact 잡에서 자동.

---

## 5. 관리자 end-to-end 여정: 지금 어디까지 직접 테스트되나

관리자가 "온보딩 → Profile → 정책 질문 → 평가 → finding/리포트 → 개선 → 승인 → 실제 변경"을
직접 실행할 수 있는지 라이브(API `8cimz0a9n9`, 토큰 USER_PASSWORD_AUTH)로 점검한 결과다.
**결론: 전 구간을 끝까지 직접 테스트할 수는 아직 없다. 세 개의 공백이 있다(아래 §6 B·C·G).**

| 단계 | 라우트 | 라이브 상태 |
| --- | --- | --- |
| 1. 관리자 로그인/온보딩 | Cognito (USER_PASSWORD_AUTH / HostedUI) | ✅ 동작. access token에 `custom:customer_id=kosa-sandbox`, Admin 그룹 주입 확인 |
| 2. 정책 문서 **업로드** | `POST /policy-sources/uploads` → `/process` → status | ⚠️ 라우트·업로드·정규화 동작. AI 후보 추출은 ADR-0023 authoring worker(`PolicyAuthoringWorkerFunction` + 전용 큐, 커밋 `58b902f`)로 **코드는 배선됨**. 마지막 배포(`d6ff2da`)에는 없으므로 **재배포 전까지 라이브 미검증**(§6 H) |
| 3. Profile **생성/게시** | `/approve` → `POST /policy-profiles` | ⚠️ 라우트 배선됨. authoring worker가 만든 후보를 사람이 승인해 게시하는 경로. 재배포 후 라이브 1회 확인 필요(§6 H) |
| 3'. Profile **선택** | `POST /assessments`의 `policy_profile_id` | ✅ 기존 fixture Profile `profile-mvp-baseline`은 선택·평가 가능(업로드 없이) |
| 4. **정책 질문 / 자연어**(PolicyQA·라우팅) | `POST /orchestrate` | ❌ **현재 404**. 코드·라우트 커밋 완료(`af57307`)이나 마지막 배포가 그 이전(`d6ff2da`) 기준이라 스택에 라우트 없음. **재배포하면 열림**(§6 D) |
| 5. **평가** | `POST /assessments` | ✅ 동작. 라이브 검증(§4): coverage 18/18, findings 12 |
| 6. **finding / 리포트** | `GET /assessments/{assessmentId}` | ✅ 동작. coverage·readiness·findings·evidence 조회 |
| 7. **개선(remediation) 선택·생성** | `POST /findings/{findingId}/remediations` | ✅ decision(TERRAFORM_PATCH/ACTUAL_SYNC/MANUAL_REVIEW/SUPPRESSED) + worker가 실제 Bedrock patch **생성**까지 라이브 동작(§4) |
| 7'. patch → **PR / 실제 파일 변경 제안** | `LiveGitHubWriteTool` (branch/commit/PR) | ⚠️ #66으로 **배선됨**. patch 바이트는 `REMEDIATION_PATCH#{digest}`에 저장, Worker가 PR을 연다. 재배포 + `DEPLOYMENT_RUNTIME_JSON` 설정 후 라이브 1회 확인 필요(§6 H) |
| 8. **승인** | `POST /deployments/{deploymentId}/approve` | ⚠️ 라우트 있으나 7'·8' 공백으로 실제 도달 불가 |
| 8'. **실제 변경(apply)** | Deployment Worker(OIDC Terraform apply) | ⚠️ `DEPLOYMENT_RUNTIME_JSON`은 #67 전까지 **설정할 통로가 없었다**. 이제 deploy Environment 값으로 넘어간다. 값은 준비됐고(§6 H) 재배포 후 미검증 |
| 감사 이력 | `GET /audit-events` | ✅ 동작(Admin) |

즉 **지금 직접 끝까지 되는 경로는**: fixture Profile 선택 → 평가 → finding/리포트 → remediation
patch 생성. **안 되는 것**: (2/3) 업로드로 정책/Profile 만들기, (4) 자연어 질문(재배포 필요),
(7'/8') patch를 실제 PR·apply로 반영. UI(React SPA)는 이 저장소 범위 밖이며 별도 배포다.

## 6. 다음에 이어서 할 작업 (선행 조건 포함)

### A. 정리 (권장 — 실제 리소스가 insecure 상태로 남음)
- 실제 버킷 `tfsbx-20260903-7f3a-a91c`의 public access block이 **현재 4개 다 false(insecure)**.
  E2E용으로 의도적으로 만든 것. 복원:
  ```bash
  aws s3api put-public-access-block --profile mfa --region us-east-1 --bucket tfsbx-20260903-7f3a-a91c \
    --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
  ```
- 고객 repo `test` main도 insecure IaC(commit b283b6b). remediation patch(§4)가 secure 복원
  내용을 담고 있으므로, 아래 B(PR write)로 정식 복원하거나 수동 revert.

### B. patch → S3 저장 + GitHub PR write (폐루프의 "PR 준비물")
- **2026-09-03 반영됨:** patch 바이트는 DynamoDB `REMEDIATION_PATCH#{digest}`에 content-addressed로
  저장하고(`DynamoDbPatchContentStore`, S3는 ADR-0014 tenant identity 검토 뒤), `LiveGitHubWriteTool`이
  branch/commit/PR을 연다. Worker는 `DEPLOYMENT_RUNTIME_JSON`이 있을 때만 TERRAFORM_PATCH를 진행한다.
  남은 것은 재배포와 라이브 1회 확인(아래 C 선행 조건과 같다).
- 현재는 patch **생성·바인딩**까지. `BedrockPatchGenerator`가 만든 변경 내용을 실제로 S3 artifact로
  저장하고, `agent/runtime/github_write_tool.py`(존재)로 branch/commit/PR을 만드는 **D write port**
  배선이 남음. `RemediationPatch`는 changed_paths와 content digest만 담고 patch 바이트는 별도 저장 필요.
- 선행: github-token 재발급(§2), GitHub App PR write perm 확인(이미 pull_requests:write 있음).
- BEDROCK_MODEL_SELECTION.md "PR 준비물" 참고. worker.py `_require_patch_result` 이후 단계.

### C. Deployment E2E (Task#8, 미착수)
- remediation PR 머지 → `POST /remediations/{id}/deployments` → plan → `POST /deployments/{id}/approve`
  → OIDC apply → post-deploy 재평가.
- 선행: DEPLOYMENT_RUNTIME_JSON이 현재 api Lambda env에서 **빈값**이라 TERRAFORM_PATCH commit 해석이
  fail-closed. **2026-09-03 확인: deploy workflow가 이 파라미터를 CloudFormation에 넘기지 않아 값을
  설정할 통로 자체가 없었다.** workflow에 다섯 파라미터(`DeploymentRuntimeJson`,
  `DeploymentGitHubSecretArns`, `PolicyAuthoringModelProfileJson`, `FrontendCallbackUrl`,
  `FrontendLogoutUrl`)를 추가했으므로, 이제 deploy Environment에 아래를 정의하면 재배포로 반영된다.
  - Secret: `DEPLOYMENT_RUNTIME_JSON`, `DEPLOYMENT_GITHUB_SECRET_ARNS` (둘 다 있거나 둘 다 없어야 함 —
    workflow와 템플릿 Rule `DeploymentCommitResolutionAllOrNone`이 각각 강제)
  - Variable: `POLICY_AUTHORING_MODEL_PROFILE_JSON`, `FRONTEND_CALLBACK_URL`, `FRONTEND_LOGOUT_URL`
  ACTUAL_SYNC는 GitHub 없이 동작.
- 고객 repo `test`에 terraform-plan/apply workflow + OIDC role 준비됨. deploy Environment 승인자 필요.

### D. Parent Orchestrator 라우트 배포 + 라이브 검증 (선행: 재배포)
- **현재 `POST /orchestrate`는 라이브 404다.** 라우트 리소스 `PostOrchestrateRoute`는 커밋
  `af57307`에 있으나 마지막 배포(run 33734365438)가 그 이전 커밋 `d6ff2da` 기준이라 스택에
  아직 없다. **`af57307` 이후 커밋으로 `Deploy M0 Foundation` workflow를 재실행하면 열린다**(§4 배포법).
- 열린 뒤 `POST /orchestrate {"message":"..."}`로 자연어→PolicyQA/ASSESSMENT/REMEDIATION/DEPLOYMENT
  decision 반환을 검증. Bedrock PARENT 프로파일(`fixtures/m1/parent_model_profile.json`,
  amazon.nova-lite-v1:0). Parent는 워크플로를 시작하지 않는다(제안·답변만).
- 이 재배포는 §5의 "정책 질문(PolicyQA)" 단계를 직접 테스트 가능하게 만드는 최소 작업이다.

### G. 정책 문서 업로드 완결 경로 — AI 후보 추출 미배선 (관리자 UX의 큰 공백)
- **증상:** `POST /policy-sources/uploads`(body: `filename`, `declared_media_type`, `byte_size`,
  optional `title`) → `/process` → status 조회까지는 배선·동작한다. 그러나 `/approve` 또는
  `POST /policy-profiles`로 가면 후보가 없어 `EMPTY_PROFILE`로 거부된다.
- **원인:** 승인 read(`load_review`/`load_publication`)는 `#CANDIDATES` item에서 후보를 읽는데,
  그 후보를 저장하는 `record_candidate_extraction` 호출자(= C의 **AI 후보 추출 실행자**)가
  아직 배선되지 않았다(API.md, `docs/POLICY_INGESTION.md`). 업로드한 정책 원문에서 Control/Rule
  후보를 뽑아 저장하는 실행 경로가 없다.
- **해야 할 일:** 정규화된 Policy Document → Bedrock으로 Control/Rule 후보 추출 →
  `record_candidate_extraction`으로 `#CANDIDATES`에 저장하는 실행자를 구현·배선. 그 뒤 `/approve`가
  후보 부분집합을 승인하고 `POST /policy-profiles`가 승인된 Rule로 versioned Profile을 게시한다.
- **선행/제약:** `docs/POLICY_INGESTION.md`의 지원 문서 형식 allow-list만 처리(목록 밖 형식 코드 추가
  금지). 후보는 사람 승인 없이는 Profile에 못 들어간다(ADR-0015). 형식 추가 시 문서+Contract 동시 갱신.
- **현재 우회:** 업로드 없이 fixture Profile `profile-mvp-baseline`을 assessment에 선택하면 평가/finding/
  remediation은 그대로 테스트된다. "관리자가 자기 정책 문서를 올려 Profile을 만드는" 경로만 미완이다.

### H. 재배포 실행 — 값 준비 완료, 실행만 남음 (2026-09-03)
- 마지막 배포는 `d6ff2da`. dev HEAD(`4df922e`)와 23 커밋 차이. 재배포 없이는 `/orchestrate`, authoring worker,
  검증 Assessment 자동 생성, PR write, 다중 리소스가 라이브에 없다.
- deploy Environment `customer-sandbox-deploy`에 추가할 값 세 개(프론트 URL 둘은 로컬 SPA 시연이라 기본값 유지):

  | 종류 | 이름 | 값 |
  | --- | --- | --- |
  | Secret | `DEPLOYMENT_RUNTIME_JSON` | `[{"customer_id":"kosa-sandbox","repository_id":"test-s3-sandbox","repository_full_name":"awsproject-team1/test","github_token_secret_id":"kosa-governance-sandbox/m1/github-token","aws_account_id":"369676914736","aws_read_role_arn":"arn:aws:iam::369676914736:role/kosa-governance-sandbox-m1-read","aws_external_id_secret_id":"kosa-governance-sandbox/m1/aws-external-id","resource_types":["AWS::S3::Bucket"]}]` |
  | Secret | `DEPLOYMENT_GITHUB_SECRET_ARNS` | `arn:aws:secretsmanager:us-east-1:369676914736:secret:kosa-governance-sandbox/m1/github-token-*` |
  | Variable | `POLICY_AUTHORING_MODEL_PROFILE_JSON` | `{"model_profile_id":"policy-authoring-nova-lite-m4-v1","role":"POLICY_AUTHORING","region":"us-east-1","model_id":"amazon.nova-lite-v1:0","prompt_version":"policy-authoring/2026-09-04","rubric_version":"policy-authoring-rubric/1","golden_dataset_version":"policy-authoring-golden/0-ungated"}` |

  세 값은 운영 파서(`DeploymentRuntimeConfiguration.from_json`, authoring `_model_profile()`, 템플릿
  `AllowedPattern`)로 로컬 검증했다. 식별자만 있고 credential은 없다. 주의 두 가지:
  - `DEPLOYMENT_GITHUB_SECRET_ARNS`는 Secrets Manager가 붙이는 6자 접미사를 모르므로 `-*` 접미사 wildcard다.
    M1 값처럼 exact ARN을 쓰려면 `aws secretsmanager describe-secret --secret-id kosa-governance-sandbox/m1/github-token`의
    ARN으로 바꾼다. IAM 동작은 같다.
  - authoring Profile은 아직 Golden으로 게이트되지 않았다(`golden_dataset_version`에 `ungated` 명시, ADR-0012).
    prompt_version은 `bedrock_extractor.PROMPT_VERSION`과 같다.
- 실행(설정 + dispatch를 한 번에):
  ```bash
  gh secret set DEPLOYMENT_RUNTIME_JSON --env customer-sandbox-deploy < DEPLOYMENT_RUNTIME_JSON
  gh secret set DEPLOYMENT_GITHUB_SECRET_ARNS --env customer-sandbox-deploy < DEPLOYMENT_GITHUB_SECRET_ARNS
  gh variable set POLICY_AUTHORING_MODEL_PROFILE_JSON --env customer-sandbox-deploy --body "$(cat POLICY_AUTHORING_MODEL_PROFILE_JSON)"
  gh workflow run deploy-m0-foundation.yml --ref dev \
    -f stack_name=kosa-governance-sandbox -f project_name=kosa-governance \
    -f environment=customer-sandbox-artifact -f stack_environment=sandbox \
    -f artifact_approval_environment=customer-sandbox-deploy -f aws_region=us-east-1 \
    -f role_to_assume=arn:aws:iam::369676914736:role/kosa-governance-sandbox-github-deploy \
    -f cloudformation_execution_role_arn=arn:aws:iam::369676914736:role/kosa-governance-sandbox-foundation-cfn \
    -f lambda_code_s3_bucket=kosa-governance-sandbox-lambda-code-369676914736 \
    -f assessment_scope_json='{"kosa-sandbox":[{"repository_id":"test-s3-sandbox","github_repository":"awsproject-team1/test","aws_account_id":"369676914736"}]}'
  ```
  두 Environment 게이트를 순서대로 승인한다.
- **첫 dispatch(run 33763473589)는 `Validate assessment deployment configuration`에서 멈췄다:**
  `assessment scope selector fields are invalid`. 원인은 ADR-0023 커밋 `58b902f`가 scope selector와 M1 runtime
  target에서 `policy_profile_id`를 제거한 것이다(Profile은 고객 Catalog가 정한다). 마지막 성공 배포(`d6ff2da`)의
  입력값과 그때 넣은 `M1_ASSESSMENT_RUNTIME_JSON`은 모두 옛 형식이라 **둘 다** 새 검증기에 걸린다(로컬에서 세 조합을
  돌려 확인: 새 target+새 scope만 통과). 고칠 것:
  - dispatch 입력 `assessment_scope_json`에서 `policy_profile_id` 제거(위 명령은 수정본).
  - **2026-09-04 재배포 결과(run 33828388203, `dev` `68d4f98`, UPDATE_COMPLETE).** 앞선 세 번은 각각
    다른 층에서 막혔다: scope selector 검증(#74) → API Gateway CORS 중복(#75) → CLI 템플릿 51,200B
    상한(#76). 네 번째는 **손으로 만든 route 6개**(`/scope`, `/policy-sources` 목록·삭제, `/admin/users`
    3종)가 스택 밖에 있어 `CREATE_FAILED AlreadyExists`로 죽었다. 복구: `list-stack-resources`의
    Route PhysicalResourceId와 `apigatewayv2 get-routes`를 대조해 고아를 산출 → 전체 route JSON 백업 →
    고아만 `delete-route` → 재디스패치. **CloudFormation이 관리하는 API에 route를 손으로 만들지 말 것.**
    급하면 템플릿을 고쳐 배포한다. 배포 후 CORS가 `http://localhost:5173`만 허용해 CloudFront SPA가
    전부 차단됐다 — deploy Environment 변수 `FRONTEND_CALLBACK_URL`/`FRONTEND_LOGOUT_URL`이 비어
    기본값이 들어간 것. 둘 다 `https://dfur2d0d1329n.cloudfront.net`로 설정했고(이제 필수값으로
    취급), 라이브 API는 `update-api`로 즉시 패치했다(다음 배포가 같은 값으로 덮어쓴다).
  - **사용자 생성 뒤 로그인 실패(2026-09-04 11:40 KST).** CloudTrail: `Login_Error_POST` ×5 — Cognito가
    비밀번호를 거부했다. 계정은 완전했고(CONFIRMED·User 그룹·customer_id) 입력값 불일치였다. 같은 사고가
    드러낸 코드 공백 셋을 고쳤다: (1) 백엔드가 길이 8만 보고 pool 정책(대·소·숫자·기호)은 안 봐서, 미달
    비밀번호는 create→group 성공 후 `admin_set_user_password`에서 500이 나며 반쪽 계정을 남겼다 → 요청
    단계 검증 + 실패 시 생성 사용자 삭제(`AdminDeleteUser` 권한 추가). (2) SPA에 로그아웃이 없어 Hosted UI
    세션 쿠키가 직전 계정을 재사용했다 → `/logout` 경로("로그아웃", 로그인 화면의 세션 종료 링크). (3) pool
    username이 대소문자 구분이라 email을 소문자로 정규화. 운영 시 재설정은
    `admin-set-user-password --permanent`, 새 사용자 로그인 테스트는 시크릿 창에서.
  - `github_repository`·`aws_account_id`는 콘솔의 "연결된 고객사 리소스"(`GET /scope`)가 읽는
    표시값이다. 배포 gate가 이 둘을 받도록 넓혔으므로 dispatch로 넣을 수 있다 — 빼고 배포하면
    라이브 Lambda에 손으로 넣어둔 값을 덮어써 화면에서 사라진다. 두 값은 같은 selector의
    `M1_ASSESSMENT_RUNTIME_JSON` target과 정확히 일치해야 하며, 어긋나면 gate가 거부한다.
  - Secret `M1_ASSESSMENT_RUNTIME_JSON`을 `policy_profile_id` 없는 형식으로 다시 넣는다. 필드는
    `customer_id, repository_id, commit_sha, github_repository, github_token_secret_id, aws_account_id,
    aws_read_role_arn, aws_external_id_secret_id, s3_bucket_id`. secret 참조는 exact ARN이어야 하고
    `M1_ASSESSMENT_SECRET_ARNS`와 집합이 정확히 같아야 한다(`aws secretsmanager describe-secret --query ARN`으로 조회).
  - 같은 exact ARN을 `DEPLOYMENT_RUNTIME_JSON`/`DEPLOYMENT_GITHUB_SECRET_ARNS`에도 쓰면 wildcard가 필요 없다.
  워크플로 입력이 코드 변경으로 조용히 무효화됐지만 CloudFormation 호출 전에 fail-closed로 잡혔다. 그것이 이 검증
  단계의 목적이다.
- **두 번째 dispatch(run 33766208595)는 검증을 통과하고 CloudFormation에서 멈췄다:** `PolicyAuthoringDlqAlarm`
  CREATE_FAILED, 실행 role `kosa-governance-sandbox-foundation-cfn`이 `cloudwatch:PutMetricAlarm` 권한 없음
  (`no identity-based policy allows`). 이 알람은 foundation 템플릿의 **첫** `AWS::CloudWatch::Alarm`이고, bootstrap
  실행 role 정책에는 `cloudwatch` 서비스가 없었다. 스택은 `d6ff2da` 상태로 롤백됐다(`/orchestrate` 여전히 404, API 정상).
  고침: 두 bootstrap 템플릿의 `FoundationExecutionRole`에 `ProvisionFoundationAlarms`
  (`cloudwatch:PutMetricAlarm/DeleteAlarms/DescribeAlarms`) 추가, 회귀는
  `tests/security/test_bootstrap_execution_role_covers_foundation.py`(foundation 리소스 유형 ↔ 실행 role 서비스 대조).
  **재배포 전에 bootstrap 스택을 먼저 갱신해야 한다** — 실행 role은 GitHub deploy role이 바꿀 수 없고 고객 관리자가
  Console(CloudFormation → `kosa-governance-sandbox-bootstrap-roles` → 업데이트 → 현재 템플릿 교체 →
  `infrastructure/cloudformation/m1-customer-bootstrap-roles.yaml` 업로드, 파라미터 그대로) 또는 MFA 세션의
  `aws cloudformation deploy --stack-name kosa-governance-sandbox-bootstrap-roles --template-file
  infrastructure/cloudformation/m1-customer-bootstrap-roles.yaml --capabilities CAPABILITY_NAMED_IAM
  --profile mfa --region us-east-1`로 적용한다. 그 뒤 위 `gh workflow run`을 다시 dispatch한다(Secret은 이미 새 형식).
- 재배포 뒤 확인 순서: `POST /orchestrate` 200 → `GET /deployments/{id}`에 `verification_assessment_id` →
  remediation 후 `awsproject-team1/test`에 PR. 시연 순서는 `docs/M4-DEMO-RUNBOOK.md` §4.
- **시연 직전 1시간 안에** GitHub App installation token을 재발급해 `kosa-governance-sandbox/m1/github-token`에
  넣는다(§2). 만료 token이면 PR write와 commit 해석이 provider error로 실패한다.

### E. 단계4 — Subgraph 래핑 (선택적, 낮은 가치)
- 기존 결정적 Assessment/Remediation worker를 LangGraph StateGraph node로 감싸는 형식 작업.
  평가 의미 불변, 회귀 위험만 있어 우선순위 낮음. Parent 그래프(`agent/graphs/parent_graph.py`)가 선례.

### F. 정합성·문서
- ADR-0012/DESIGN.md는 이미 Parent를 기술. AI rule 선택은 ADR-0002 §Rule applicability mechanism에 반영됨.
- Remediation/Parent Model Profile을 Golden Dataset으로 재검증 후 승인(ADR-0012 model profile 규칙).
- `StoredDataError`(finding 없음) → 현재 handler 광범위 except가 503으로 변환. 404 매핑 개선 여지.
- Deploy workflow는 이제 LangGraph Layer를 빌드/업로드(단계2). 다른 스택에도 동일 workflow 재사용 가능.

---

## 7. 검증 명령
```bash
# 오프라인 전체
python3 -m unittest discover -s tests/unit -p 'test_*.py'
python3 -m unittest discover -s tests/contract -p 'test_*.py'
python3 -m unittest discover -s tests/security -p 'test_*.py'
python3 -m unittest discover -s tests/integration -p 'test_*.py'
python3 -m ruff check . && python3 -m ruff format --check .
# LangGraph Layer 로컬 빌드
bash scripts/build-langgraph-layer.sh /tmp/langgraph-layer.zip
# Lambda 코드 ZIP
bash scripts/package-m0-lambda.sh /tmp/m0-lambda.zip
```
langgraph 테스트는 `requirements-dev.txt`의 `langgraph==1.2.11` 설치 필요.

## 8. 로컬 보존 파일 (Git 제외, 보관자 A/taemin에게 요청)
- `./awsproject-team1-kosa-reader.2026-09-02.private-key.pem` — GitHub App private key
- `.ai/e2e-secrets/` — M1 설정 사본(extid, m1_runtime.json 등), 이미 AWS/GitHub에 반영됨
- `.ai/HANDOFF.md` — 개인 로컬 상세 진행
