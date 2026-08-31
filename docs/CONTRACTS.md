# Domain Contracts

실행 가능한 Contract 정본은 `packages/contracts/`다. 이 문서는 Schema의 의미와 변경 규칙을 설명하며, 두 위치는 같은 PR에서 갱신한다.

## Core entities

- `Job`: 비동기 workflow 실행 상태와 상관 ID
- `Assessment`: 대상 Repository, Policy Profile, Resource Scope, Coverage
- `AssessmentResult`: Resource × Rule 판정, Score, Rationale, Evidence
- `Finding`: 실패·검토 필요 결과와 Severity
- `Remediation`: 선택된 Finding의 Terraform Patch/Diff와 PR 정보
- `Deployment`: plan, approval, apply, post-deploy 상태
- `Approval`: 승인자, 승인 시점, `commit_sha`, `plan_hash`
- `WorkflowTask`: 내부 SQS Worker에 전달하는 최소 `job_id`, 예상 revision, command

## EvaluationResult minimum shape

```json
{
  "resource_id": "string",
  "rule_id": "string",
  "perspective": "IAC | AWS_ACTUAL | DRIFT",
  "status": "PASS | FAIL | MANUAL_REVIEW | INSUFFICIENT_EVIDENCE | OUT_OF_SCOPE | EXECUTION_ERROR",
  "severity": "LOW | MEDIUM | HIGH | CRITICAL",
  "score": 85,
  "rationale": "string",
  "evidence_references": ["aws:s3:bucket/example#read-resource", "isms-p-2023#control/5.2.1"],
  "rule_version": "string",
  "rubric_version": "string",
  "model_profile_id": "string"
}
```

`score`는 기본적으로 0–100 범위의 연속 값이다. Golden Dataset 반복 평가에서 편차가 ±10점을 지속적으로 넘겨 Anchor 방식으로 전환한 경우에만 `{0, 15, 30, 50, 70, 85, 100}` 중 하나여야 한다. 코드가 현재 Scoring 정책, Schema와 Evidence Reference를 검증한다.

`packages/contracts/assessments.py`는 V3 평가 단계(`INITIAL`, `DEPLOYMENT_READINESS`, `POST_DEPLOY_VERIFICATION`), `EvaluationPerspective`(`IAC`, `AWS_ACTUAL`, `DRIFT`)와 `EvaluationResult`의 기본 검증을 제공한다. 각 결과는 평가에 실제 사용된 `model_profile_id`를 반드시 보존한다. Initial Assessment는 동일한 Terraform 관리 대상의 IaC Compliance, Actual Compliance, Drift를 이 관점으로 분리한다. Score 산출 자체는 AI Evaluator가 담당하며 Contract는 범위와 구조만 검증한다.

`scoring_mode`의 기본값은 `CONTINUOUS`다. 신뢰성 Gate가 Anchor 전환을 승인하면
`ANCHORED`를 명시하고 score는 `{0, 15, 30, 50, 70, 85, 100}`만 허용한다.

## Natural-language and Model Profile boundary

명시적 UI/API 요청은 대응 Workflow로 직접 진입한다. 자연어 요청은 Parent Orchestrator가
Policy Q&A를 직접 처리하거나 `ASSESSMENT`, `REMEDIATION`, `DEPLOYMENT` 중 후보 Workflow와
필요한 selector를 제안할 수 있지만, Job 생성·scope 권한·승인 여부는 Backend Contract가
결정한다. Parent의 자연어 출력은 실행 명령이 아니다.

Parent(Policy Q&A 포함)와 각 Workflow에는 역할별 Golden Dataset 평가를 통과한 Model Profile을 사전
선택한다. 실행과 Golden Dataset 결과는 Model ID/Version, Prompt/Rubric Version, 사용한
Model Profile을 함께 보존한다. 이 필드의 runtime shape는 각 Workflow 구현 PR에서
`packages/contracts/`에 추가하며, 그 전에는 Model Profile 변경을 배포할 수 없다.

`packages/contracts/model_profiles.py`의 `ModelProfile`은 workflow role, Region, Bedrock Model
ID, Prompt/Rubric Version, Golden Dataset Version을 하나의 immutable 승인 단위로 고정한다.
M0 Assessment 기본 Profile은 `fixtures/m0/assessment_model_profile.json`의
`assessment-nova-lite-m0-v1`이며, `us-east-1`의 `amazon.nova-lite-v1:0`을 사용한다.

M1 C의 Bedrock adapter는 injected Converse client로만 호출하며, 모델에는 선택된 Resource
Snapshot과 해당 Rule·Profile 정보만 전달한다. 모델 응답은 `status`, `score`, `rationale`,
`evidence_references` 네 필드의 JSON으로 한정된다. Resource/Rule/Perspective/Severity/Version과
Model Profile은 Runtime이 authoritative input에서 재구성하고, evidence는 Snapshot과 Rule이
허용한 locator의 부분집합만 허용한다. 정책 근거의 정규형은
`{source_id}#{locator}`이며 `SourceReference.evidence_reference`만 사용한다. AWS 실제 상태 근거는
`aws:` namespace를 사용하므로 정책 원문 근거와 구분된다.

S3 MVP의 `AWS_ACTUAL` Evidence는 C가 D의 `AwsResourceTool.READ_RESOURCE`로
`AWS::S3::Bucket` 한 건을 조회해 구성한다. C는 query의 Customer/Account/Resource ID와 응답의
동일성을 다시 검증하고, Resource Tool이 제공하지 않는 Write 경로는 사용하지 않는다.

M1 Coverage는 Assessment 시작 시 확정한 적용 가능 `Resource × Rule × Perspective` 수를 분모로
사용한다. `PASS`, `FAIL`, `MANUAL_REVIEW`, `INSUFFICIENT_EVIDENCE`, `OUT_OF_SCOPE` 결과는 완료된
평가로 집계하고 `EXECUTION_ERROR`는 분모에 남겨 재시도·실패 범위를 드러낸다. 동일한
Resource × Rule × Perspective의 재전송 결과는 한 번만 집계한다.

## Async Worker boundary

`WorkflowTask`는 Queue에 Artifact 본문이나 고객 scope를 복사하지 않고 `job_id`,
`expected_revision`, `command`만 전달한다. Worker는 JWT-derived Job 소유 데이터와 저장된
checkpoint를 DynamoDB에서 다시 읽고, 큰 데이터는 S3 Artifact reference로 복원한다. 역할별
Queue는 `ASSESS_RESOURCE`, `GENERATE_REMEDIATION`, `RUN_DEPLOYMENT`와 Plan/Apply 완료
command를 대응 Worker로 전달한다.

Assessment Worker는 한 리소스의 허용 Rule 묶음을 하나의 resumable work item으로 처리한다.
Lambda의 남은 시간이 3분이면 조건부 checkpoint 저장과 다음 Task 전송 뒤 종료한다. 일시 오류는
총 세 번 시도하고 DLQ로 이동하며, validation/scope/permission 오류는 즉시 실패한다. Apply는
자동 재시도하지 않는다. Admin 재시도는 새 revision을 생성해야 하며 기존 Task를 그대로 재생하지
않는다.

## M0 policy boundary

`packages/contracts/policy.py`는 B와 C 사이의 최소 handoff다.

- `PolicySource`: 승인된 정책 원문의 ID, 종류(`INTERNAL_POLICY`/`ISMS_P`), 버전과
  S3 Artifact ID/content hash
- `SourceReference`: 정책 원문 안의 locator와 content hash. `evidence_reference`는
  `{source_id}#{locator}` 정규형으로 Rule과 평가 Evidence를 추적한다.
- `SourceReference`: 정책 원문 안의 locator와 content hash, 그리고 그 locator가 유효한
  `source_version`. 원문이 개정되면 같은 locator라도 다른 내용을 가리키므로 Rule과 Control은
  항상 Source version까지 고정한다. 평가 Evidence는 `evidence_reference`
  (`{source_id}@{source_version}#{locator}`) 형식을 사용해 어떤 판본을 인용했는지 복원한다.
- `PolicyRule`: versioned rule, severity, 적용 평가 단계, Resource 유형과 하나 이상의
  Source Reference
- `PolicyRuleReference`: Rule ID와 version을 함께 고정하는 Profile 참조
- `PolicyProfile`: `rule_references`로 구성된 versioned allow-list. Repository/AWS Account 권한은
  Profile이 아니라 Backend의 JWT scope에서 강제한다.
- `PolicyControl`: 정책 통제 항목과 그것을 구현하는 Rule version 목록. Control은 Rule보다
  상위 단위이며, Coverage는 이 매핑으로 "어떤 통제가 어떤 Rule로 평가됐는지" 설명한다.

Policy Context Tool은 선택된 Profile의 Rule과 Source Reference만 전달한다. AI가
Profile 밖의 Rule 또는 임의 Policy Source를 선택할 수 없다.

Rule version이 고정돼도 Profile이 Rule 선택 경계이므로, 비동기 Job은 승인 시점의 Profile
version도 함께 고정한다. `PolicyContextResolver.resolve(..., expected_profile_version=...)`은
그 사이에 Profile이 교체되면 다른 allow-list로 평가하지 않고 `PolicyNotFoundError`로 실패한다.
Assessment/Work 레코드에 이 version을 영속화하는 것은 Backend(A)의 저장 경계다.

### Evidence reference boundary

평가 결과의 Evidence는 두 종류만 허용한다.

| 종류 | 형식 | 검증 |
| --- | --- | --- |
| 정책 근거 | `{source_id}@{source_version}#{locator}` | 해당 Policy Context가 실제로 포함한 `SourceReference`여야 한다 |
| Resource 상태 근거 | `aws:`, `terraform:`, `s3://` 접두사 | namespace allow-list |

`PolicyContext.allows_evidence()`가 이 규칙을 판정하고 `AssessmentRunner`가 평가기 결과마다
강제한다. version 없는 구 형식(`{source_id}#{locator}`)과 Context 밖 정책 근거는 거부한다.

### Customer Policy Source ingestion boundary

`PolicySource`는 승인 완료된 Source의 최소 평가 Contract이며 업로드/파싱 상태 Contract가
아니다. 수집 경계는 `packages/contracts/policy_ingestion.py`가 따로 정의한다. Source
종류(`INTERNAL_POLICY`, `ISMS_P`)와 파일 형식(`PolicySourceFormat`)은 서로 다른 개념이다.

| 값 | 역할 |
| --- | --- |
| `PolicySourceFormat` / `FORMAT_MEDIA_TYPES` | 지원 형식 allow-list의 정본. Backend와 Frontend가 같은 목록을 쓴다 |
| `PolicySourceUploadRequest` | Client가 선언할 수 있는 전부 (파일명, media type, byte size, 제목) |
| `IngestionStatus` | `UPLOADED`→`VALIDATING`→`PARSING`→`REVIEW_REQUIRED`/`READY`/`FAILED`/`SUPERSEDED` |
| `IngestionFailureCode` / `ExtractionWarningCode` | 실패·경고를 자유 문장이 아닌 열거값으로 표현 |
| `NormalizedDocumentUnit` | unit별 stable `locator`, 정규화 text hash, 원본 위치 |
| `NormalizedPolicyDocument` | 원본 identity, 파일 metadata, parser ID/version, 정규화 Artifact/hash, 처리 상태 |

**이 Contract는 원문도 추출 텍스트도 담지 않는다.** unit은 hash만 갖고 텍스트는 정규화
Artifact 바이트로만 존재한다. Queue payload와 DynamoDB item이 이 Contract를 그대로 나르므로,
"원문이 로그·payload에 남지 않는다"를 규율이 아니라 구조로 강제한다.

구현은 `apps/backend/policy/ingestion/`이다. `normalize_upload()`가 (선언 media type, 파일
signature, Parser 지원 형식) 3자를 대조하고, Markdown/Plain text/CSV/XLSX/DOCX Parser가 같은
`NormalizedPolicyDocument`를 만든다. Parser는 표준 라이브러리만 쓴다 —
`apps/backend/requirements.txt`가 비어 있는 ZIP Lambda 배포 구조가 형식 목록의 제약이다.
실패는 예외가 아니라 `FAILED` 상태와 failure code로 돌아온다.

`READY` 상태이면서 사람이 승인한 정확한 Source version에서 나온 Rule만 Profile이 참조할 수 있다
(`APPROVABLE_STATUSES`). `source_reference_for()`가 정규화 unit에서 `SourceReference`를 직접
만들어 locator와 `content_sha256`이 같은 판본에서 나오도록 보장한다. 승인·Profile publication
경계와 업로드 세션/저장은 아직 구현되지 않았다 (각각 B, A). 결정 근거는 ADR-0015다.

## M1 rule registry

MVP Rule Registry는 `fixtures/rules/`에 커밋된다. `apps/backend/policy/registry.py`의
`load_rule_registry()`가 이를 읽어 `PolicyRegistry`(sources, rules, profiles, catalog,
ControlMapping)를 만든다.

| File | Content |
| --- | --- |
| `sources.json` | `PolicySource` 목록. 원문 자체가 아니라 식별자·버전·content hash |
| `rules.<resource>.json` | Resource 유형별 `PolicyRule` 목록. 파일 추가만으로 확장한다 |
| `controls.json` | `PolicyControl` 매핑 (Control → Rule version) |
| `profiles.json` | `PolicyProfile` allow-list |

로드 시 (1) 모든 `SourceReference`가 선언된 Policy Source의 **정확한 version**을 가리키는지,
(2) Profile과 Control이 Registry에 실제로 존재하는 Rule version만 참조하는지, (3) Source가
`(source_id, version)`으로 유일한지 교차 검증한다. Registry에 정의됐지만
Profile allow-list에 없는 Rule은 어떤 Resource 유형으로도 Policy Context에 들어가지 않는다.

M1 평가 대상은 S3 단독이다. EC2 Rule은 Mapping/Context 계층의 multi-type 동작을 고정하기 위해
Registry에만 존재하며 Profile에는 포함하지 않는다 (M2 확장 대상).

정책 원문은 저장소에 없으므로 (ADR-0004) `SourceReference.content_sha256`은
`scripts/policy_source_digest.py --verify`로 원문 보유자만 검증한다. 원문 파일 매핑과 digest는
`(source_id, source_version)`으로 관리하므로 같은 Source의 여러 판본이 공존할 수 있다. 원문이
없는 Source version만 건너뛰고, 보유한 나머지는 계속 검증한다.

Coverage는 두 층으로 설명한다. `covered_controls()`는 Context가 근거로 **인용한** 통제이고,
`control_rule_coverage()`는 통제별 (평가 Rule 수 / 전체 Rule 수)다. 한 통제가 여러 Rule로 구현되고
그중 일부만 이번 Context에 들어올 수 있으므로, 인용됐다는 사실만으로 완전히 평가됐다고 보지 않는다.

Registry의 read-only DynamoDB 어댑터는 `apps/backend/policy/dynamodb_catalog.py`이며
`docs/DATABASE.md`의 `POLICY_PROFILE#`, `POLICY_SOURCE#`, `RULE#` key layout을 사용한다.
Catalog 인스턴스는 하나의 `customer_id`에 묶여 다른 Customer partition을 표현할 수 없다.
## M0 Golden Dataset boundary

`GoldenDatasetCase`는 case ID, Assessment Phase, Evaluation Perspective, Resource Snapshot Artifact ID,
Rubric/Scoring Mode Version, 기대 Status, score 허용 범위와 필수 Evidence Reference를
고정한다. Golden fixture는 `fixtures/m0/golden_dataset_case.json`이고 원문 Snapshot은
S3 Artifact로 관리한다. Model/Prompt/Rule/Rubric/Policy/Tool 변경 때 C는 이 Case와
후속 Case를 반복 실행해 DESIGN의 품질 Gate를 검증한다.

## M0 remediation and deployment boundary

`packages/contracts/deployments.py`는 D와 A/C 사이의 transport shape다.

- `ArtifactReference`: 고객 및 선택적 Repository 범위가 붙은 Artifact identity와 hash
- `IaCSnapshot`: Customer/Repository/Commit에 묶인 `TERRAFORM_SNAPSHOT`
- `RemediationPatch`: Finding의 base commit, `REMEDIATION_PATCH`, repository-relative
  changed path 목록
- `AwsResourceQuery`: `READ_RESOURCE`/`LIST_RESOURCES`만 허용하는 Read-Only Tool 요청
- `TerraformPlan`: Deployment/commit/plan hash에 묶인 `TERRAFORM_PLAN`
- `DeploymentApproval`: Apply 직전에 `TerraformPlan`의 deployment ID, commit SHA,
  plan hash가 모두 일치하는지 확인한다.

IaC 변경이 필요한 Drift Finding은 IaC를 원하는 안전한 상태로 변경하는 Patch를 만든다.
IaC가 이미 안전하고 Actual만 drift된 경우에는 Patch 없이 해당 IaC Snapshot/commit으로
동기화 Plan을 만든다. Readiness는 refresh된 Plan으로 Patch 또는 동기화 대상의 현재 Actual
적용 가능성을 검증하며, Write는 Human Approval 뒤 GitHub Actions OIDC Apply에서만
일어난다. Terraform 관리 밖의 리소스 또는 안전한 IaC 매핑이 없는 Drift는 `MANUAL_REVIEW`다.

Artifact는 공개 S3 URL을 포함하지 않는다. GitHub App은 승인 Repository에만 branch/commit/PR을
생성하고, Terraform Apply는 이 일치 검증 뒤 GitHub Actions OIDC 경로에서만 가능하다.

## Contract change review

Contract 변경 PR은 변경 작성자와 해당 Contract의 Producer 및 Consumer Owner가 검토한다. Contract가 확정됐지만 구현체가 없는 경우 `Mockable` 상태로 Fixture/Mock을 사용해 병렬 개발할 수 있다.
