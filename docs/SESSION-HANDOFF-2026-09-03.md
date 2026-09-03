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
- installation token은 **1시간 만료**. 재발급(private key 필요):
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

## 5. 다음에 이어서 할 작업 (선행 조건 포함)

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
- 현재는 patch **생성·바인딩**까지. `BedrockPatchGenerator`가 만든 변경 내용을 실제로 S3 artifact로
  저장하고, `agent/runtime/github_write_tool.py`(존재)로 branch/commit/PR을 만드는 **D write port**
  배선이 남음. `RemediationPatch`는 changed_paths와 content digest만 담고 patch 바이트는 별도 저장 필요.
- 선행: github-token 재발급(§2), GitHub App PR write perm 확인(이미 pull_requests:write 있음).
- BEDROCK_MODEL_SELECTION.md "PR 준비물" 참고. worker.py `_require_patch_result` 이후 단계.

### C. Deployment E2E (Task#8, 미착수)
- remediation PR 머지 → `POST /remediations/{id}/deployments` → plan → `POST /deployments/{id}/approve`
  → OIDC apply → post-deploy 재평가.
- 선행: DEPLOYMENT_RUNTIME_JSON이 현재 api Lambda env에서 **빈값**이라 TERRAFORM_PATCH commit 해석이
  fail-closed. deploy Environment에 DeploymentRuntimeJson + DeploymentGitHubSecretArns 설정 필요
  (템플릿 Rule DeploymentCommitResolutionAllOrNone: 둘 다 있거나 둘 다 없어야 함). ACTUAL_SYNC는 GitHub 없이 동작.
- 고객 repo `test`에 terraform-plan/apply workflow + OIDC role 준비됨. deploy Environment 승인자 필요.

### D. Parent Orchestrator 라이브 검증 (미실행)
- `POST /orchestrate` `{"message": "..."}`는 배포됐으나 라이브 호출 미검증. Bedrock PARENT 모델
  프로파일(`fixtures/m1/parent_model_profile.json`, model amazon.nova-lite-v1:0)로 실제 라우팅 확인 필요.
- 자연어→PolicyQA/ASSESSMENT/REMEDIATION/DEPLOYMENT decision 반환. Parent는 워크플로 시작 안 함.

### E. 단계4 — Subgraph 래핑 (선택적, 낮은 가치)
- 기존 결정적 Assessment/Remediation worker를 LangGraph StateGraph node로 감싸는 형식 작업.
  평가 의미 불변, 회귀 위험만 있어 우선순위 낮음. Parent 그래프(`agent/graphs/parent_graph.py`)가 선례.

### F. 정합성·문서
- ADR-0012/DESIGN.md는 이미 Parent를 기술. AI rule 선택은 ADR-0002 §Rule applicability mechanism에 반영됨.
- Remediation/Parent Model Profile을 Golden Dataset으로 재검증 후 승인(ADR-0012 model profile 규칙).
- `StoredDataError`(finding 없음) → 현재 handler 광범위 except가 503으로 변환. 404 매핑 개선 여지.
- Deploy workflow는 이제 LangGraph Layer를 빌드/업로드(단계2). 다른 스택에도 동일 workflow 재사용 가능.

---

## 6. 검증 명령
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

## 7. 로컬 보존 파일 (Git 제외, 보관자 A/taemin에게 요청)
- `./awsproject-team1-kosa-reader.2026-09-02.private-key.pem` — GitHub App private key
- `.ai/e2e-secrets/` — M1 설정 사본(extid, m1_runtime.json 등), 이미 AWS/GitHub에 반영됨
- `.ai/HANDOFF.md` — 개인 로컬 상세 진행
