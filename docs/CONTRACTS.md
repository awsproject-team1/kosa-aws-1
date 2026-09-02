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
  "evidence_references": ["aws:s3:bucket/example#read-resource", "isms-p-2023@2023.1#control/5.2.1"],
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
`{source_id}@{source_version}#{locator}`이며 `SourceReference.evidence_reference`만 사용한다. AWS 실제 상태 근거는
`aws:` namespace를 사용하므로 정책 원문 근거와 구분된다.

S3 MVP의 `AWS_ACTUAL` Evidence는 C가 D의 `AwsResourceTool.READ_RESOURCE`로
`AWS::S3::Bucket` 한 건을 조회해 구성한다. C는 query의 Customer/Account/Resource ID와 응답의
동일성을 다시 검증하고, Resource Tool이 제공하지 않는 Write 경로는 사용하지 않는다.

M1 Coverage는 Assessment 시작 시 확정한 적용 가능 `Resource × Rule × Perspective` 수를 분모로
사용한다. `PASS`, `FAIL`, `MANUAL_REVIEW`, `INSUFFICIENT_EVIDENCE`, `OUT_OF_SCOPE` 결과는 완료된
평가로 집계하고 `EXECUTION_ERROR`는 분모에 남겨 재시도·실패 범위를 드러낸다. 동일한
Resource × Rule × Perspective의 재전송 결과는 한 번만 집계한다.

## M1 Finding and Readiness boundary

`EvaluationResult` 중 `FAIL`, `MANUAL_REVIEW`, `INSUFFICIENT_EVIDENCE`는 C가 각각 하나의
immutable `Finding`으로 투영한다. Finding ID는 `resource_id`, `rule_id`, `rule_version`,
`perspective`에서 결정적으로 만들며, 원 Evaluation Result의 status, severity, score, rationale,
evidence와 평가 provenance(`assessed_commit_sha`, offset-aware `evaluated_at`)를 보존한다.
legacy Result/Finding은 provenance 없이 읽을 수 있으나 remediation 자동화에는 사용할 수 없다
(fail-closed). `PASS`, `OUT_OF_SCOPE`, `EXECUTION_ERROR`는 Finding이 아니다.

`ReadinessScore`는 평가 계획이 완전히 Coverage 되었을 때만 반환한다. `OUT_OF_SCOPE`와
`DRIFT` 관점은 점수 계산에서 제외하고, 나머지 평가 score를 Rule Severity 가중치 `LOW=1`,
`MEDIUM=2`, `HIGH=4`, `CRITICAL=8`로 가중 평균하여 소수 둘째 자리로 반올림한다.
`EXECUTION_ERROR` 또는 미완료 평가가 있으면 Readiness Score는 `null`이며 Coverage가 그
이유를 표시한다. 이 산식은 AI가 아닌 C의 결정적 report projection이다.

## M1 DRIFT derivation boundary

`DRIFT`는 AI 판정이 아니라 같은 Resource × Rule에 대한 `IAC`와 `AWS_ACTUAL` 결과의 기계적
비교다(ADR-0011). 두 결과가 같은 severity, 같은 Model Profile, 같은 rubric version에서
나왔을 때만 비교하며, 그렇지 않으면 비교하지 않고 실패한다.

| IAC | AWS_ACTUAL | DRIFT |
| --- | --- | --- |
| `PASS` | `PASS` | `PASS` (정합) |
| `FAIL` | `FAIL` | `PASS` (정합; 준수 문제는 두 관점의 Finding이 담는다) |
| `PASS` | `FAIL` | `FAIL` (Actual이 안전한 IaC에서 이탈) |
| `FAIL` | `PASS` | `FAIL` (IaC가 안전한 Actual과 불일치) |
| `MANUAL_REVIEW` 또는 `INSUFFICIENT_EVIDENCE` 포함 | | `MANUAL_REVIEW` |
| 한쪽 결과 없음 | | `MANUAL_REVIEW` |
| `EXECUTION_ERROR` 포함 | | `EXECUTION_ERROR` (Coverage 분모에 남고 완료로 세지 않는다) |
| 양쪽 `OUT_OF_SCOPE` | | `OUT_OF_SCOPE` |

`DRIFT` 결과의 score는 정합 100, 이탈 0이며, evidence는 두 관점 근거의 합집합이므로 Drift
Finding이 IaC와 Actual 양쪽으로 추적된다. `DRIFT`는 AI나 Runtime에 어떤 write 권한도 주지
않으며 Remediation 입력 근거로만 쓰인다.

IAC 관점 평가에는 Artifact reference인 `IaCSnapshot`만으로는 부족하므로, D의 read-only
GitHub 경계가 승인 commit의 Terraform 본문(`IaCDocument`)을 함께 read한다. 본문 read는
`SnapshotReadRequest.include_iac_document`로 호출자가 명시할 때만 수행하고, tool이 본문
read를 지원하지 않으면 관점을 조용히 빼지 않고 실패한다.

## Async Worker boundary

`WorkflowTask`는 Queue에 Artifact 본문이나 고객 scope를 복사하지 않고 `job_id`,
`expected_revision`, `command`만 전달한다. Worker는 JWT-derived Job 소유 데이터와 저장된
checkpoint를 DynamoDB에서 다시 읽고, 큰 데이터는 S3 Artifact reference로 복원한다. Assessment
Queue는 `ASSESS_RESOURCE`, C Remediation Queue는 `GENERATE_REMEDIATION`과
`SYNC_ACTUAL_STATE`, D Deployment Queue는 `RUN_DEPLOYMENT`와 Plan/Apply 완료 command만
각 대응 Worker로 전달한다. Queue별 dispatcher는 다른 역할의 command를 거부한다.

Assessment Worker는 한 리소스의 허용 Rule 묶음을 하나의 resumable work item으로 처리한다.
Lambda의 남은 시간이 3분이면 조건부 checkpoint 저장과 다음 Task 전송 뒤 종료한다. 일시 오류는
총 세 번 시도하고 DLQ로 이동하며, validation/scope/permission 오류는 즉시 실패한다. Apply는
자동 재시도하지 않는다. Admin 재시도는 새 revision을 생성해야 하며 기존 Task를 그대로 재생하지
않는다.

## M0 policy boundary

`packages/contracts/policy.py`는 B와 C 사이의 최소 handoff다.

- `PolicySource`: 승인된 정책 원문의 ID, 종류(`INTERNAL_POLICY`/`ISMS_P`), 버전과
  S3 Artifact ID/content hash
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

Parser 경계는 신뢰할 수 없는 고객 문서를 읽으므로 fail-closed 한도를 함께 강제한다: 업로드
크기, zip entry 수·압축 해제 크기·압축비, 정규화 unit 수, 그리고 **DTD를 선언한 OOXML part
거부**(`XML_DTD_NOT_ALLOWED`). 마지막 항목은 `xml.etree`가 내부 엔티티를 확장하기 때문이며,
zip 한도로는 잡히지 않는다 — 증폭이 압축 해제 이후 Parser 안에서 일어난다.

`READY` 상태이면서 사람이 승인한 정확한 Source version에서 나온 Rule만 Profile이 참조할 수 있다
(`APPROVABLE_STATUSES`). `source_reference_for()`가 정규화 unit에서 `SourceReference`를 직접
만들어 locator와 `content_sha256`이 같은 판본에서 나오도록 보장한다. 승인·Profile publication
경계와 업로드 세션/저장은 아직 구현되지 않았다 (각각 B, A). 결정 근거는 ADR-0015다.

### Approval and Profile publication boundary

`packages/contracts/policy_approval.py`가 승인과 게시를 정의한다. 문서와 API가 못 박은 대로
**승인과 게시는 서로 다른 operation이다.** 승인은 Source/Control/Rule version을 확정하고, 게시는
그 Rule들을 평가 경계로 만든다.

| 값 | 역할 |
| --- | --- |
| `RuleLifecycle` | `CANDIDATE`/`APPROVED`/`REJECTED`/`SUPERSEDED`. `docs/DATABASE.md`의 Rule metadata가 담는 lifecycle |
| `RuleCandidate` | 승인 전 Rule. frozen이며 `approved()`가 새 값을 만든다 |
| `PolicyCandidateExtraction` | C가 exact `READY` 정규화 문서에서 만든 immutable 후보 묶음. 문서 metadata, 후보와 extractor ID/version만 담고 원문·정규화 text는 담지 않는다. A는 이 결과를 저장해 review/publication read port에서 같은 문서 판본과 함께 복원한다. |
| `PolicySourceApproval` | `(source_id, source_version, artifact_id, s3_version_id, content_sha256)`을 인용하는 immutable 승인 record |
| `ApprovalRejectionCode` | 승인·게시 거부 사유. 자유 문장이 아니다 |

판정은 `apps/backend/policy/ingestion/approval.py`의 두 함수다.

- `approve_source(document, candidates, ...)` — `READY` 문서에만 붙고, 후보가 인용한
  (source_id, source_version)·locator·`content_sha256`을 정규화 결과와 대조한다. 사람이 검토한
  문장과 Rule이 고정한 hash가 같음을 승인 시점에 못 박는다. 이미 `REJECTED`/`SUPERSEDED`인
  후보는 거부한다 (`RULE_NOT_APPROVABLE`) — 재검토는 새 후보로 올린다. 이미 `APPROVED`인 후보를
  다시 넘기는 것은 허용한다. 승인 API가 at-least-once로 재시도될 수 있기 때문이다 (ADR-0013).
- `publish_profile(...)` — `docs/POLICY_INGESTION.md`의 거부 조건 3건을 구현한다: 승인되지 않은
  Source/Rule 참조, 승인된 것과 다른 Source version 참조, 승인 record의
  `(artifact_id, s3_version_id, content_sha256)`과 어긋나는 Rule. 마지막 항목의 게시 시점 대조는
  `(artifact_id, content_sha256)`까지다 — `PolicySource`에 `s3_version_id` 필드가 없다. 두 값이
  같으면 같은 바이트이므로 판본이 뒤바뀌는 경우는 걸리며, S3 object version까지 고정하는 것은
  A가 조건부 write에서 `PolicySourceApproval.original_binding`을 쓰는 몫이다.

두 경로의 거부 사유는 **같은 `ApprovalRejectionCode` 열거값**이다. 같은 성격의 거부가 경로에
따라 코드 없는 예외로 새면 A가 응답 오류 코드로 옮길 수 없다.

두 함수는 아무것도 영속화하지 않는다. A가 DynamoDB 조건부 write 앞에서 호출하고, 거부되면
write를 시도하지 않는다. 게시 결과는 기존 `PolicyProfile`이므로 `InMemoryPolicyCatalog`와
`PolicyContextResolver` 경로가 그대로 동작한다. **Profile allow-list가 Rule의 유일한 진입
경로이므로, Catalog에 후보 Rule이 있어도 Profile이 참조하지 않으면 Policy Context에 들어오지
못한다.**

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

## M2 remediation scope boundary

`packages/contracts/remediation_policy.py`는 B가 D와 A에게 주는 조치 판정 경계다 (ADR-0017).
평가가 끝난 Finding 하나가 **자동 조치 대상인지, 예외로 덮였는지, 사람이 봐야 하는지**를
값으로 돌려준다. 이 경계는 아무것도 영속화하지 않고 GitHub·AWS·Terraform을 건드리지 않는다.

- `RemediationAction`: `TERRAFORM_PATCH`, `ACTUAL_SYNC`, `MANUAL_REVIEW`, `SUPPRESSED`
- `RemediationEligibility`: Rule version 단위 `AUTOMATIC` / `MANUAL_ONLY`. **Patch 합성**에
  대한 판단이다. 정본은 `fixtures/rules/remediation.json`이며 Registry가 함께 로드한다
- `ManualReviewCode`: 자동 조치를 거부한 사유. 자유 문장이 아니다
- `RemediationException`: `(customer_id, rule_id, rule_version)`과 선택적 `resource_id`에
  묶이는 고객 승인 면제. 사유는 열거값이고 `approved_at`/`expires_at`은 offset을 포함한
  ISO-8601이어야 한다. 억제 여부는 **두 시각**으로 갈린다 — 승인은 Finding이 평가된 시점보다
  앞서야 하고(`approved_at <= finding_evaluated_at`), 만료는 판정 시점 기준으로 확인한다
  (`at < expires_at`). `is_in_force_for()`가 이 판단이고, 한 시각만 보는 `is_active_at()`은
  조치 게이트가 아니다. 조치 요청은 평가보다 늦게 오는 것이 정상이므로 두 시각을 같은 값으로
  두면 나중에 승인된 예외가 옛 Finding을 소급 억제한다
- `RemediationTarget`: 대상 Resource의 Terraform 관리 여부와 그 Resource에 대한 **해당 Rule
  version의 IAC 관점** 판정. `rule_id`/`rule_version`을 Finding과 대조하고,
  `iac_status`/`iac_perspective=IAC`/`iac_commit_sha`를 한 묶음으로 강제한다. 같은 리소스의
  다른 Rule이나 Actual 관점에서 나온 `PASS`가 `ACTUAL_SYNC`를 열 수 없고, 평가 뒤 Repository가
  진행한 경우 옛 commit의 판정이 새 commit을 배포 대상으로 만들 수 없다
- `RemediationDecision`: Finding·Rule version·관점과 판정. `MANUAL_REVIEW`만
  `manual_review_code`를, `SUPPRESSED`만 `exception_id`를 가진다

`RemediationPolicy.decide(finding, *, customer_id, target, commit_sha, finding_evaluated_at, at,
exceptions)`의 판정 순서가 정책이다. 유효한 예외 → 평가되지 못한 Finding → 허용 범위 등록 여부
→ Terraform 관리 여부 → IaC 판정의 commit 대조 → 관점별 조치 유형 → Patch일 때만 `MANUAL_ONLY`
확인. `commit_sha`는 이번 조치가 대상으로 삼는 IaC commit이고, `finding_evaluated_at`은 Finding이
평가된 시각, `at`은 판정 시각이다. A는 Finding provenance가 context snapshot commit과 정확히
같고 `finding_evaluated_at <= at`일 때만 이 호출을 수행한다.

- 허용 범위에 **등록되지 않은** Rule은 어떤 자동 조치도 받지 못한다. 판단의 부재는
  `MANUAL_ONLY`라는 판단과 다르다
- `AWS_ACTUAL`/`DRIFT` Finding의 IaC 판정이 `commit_sha`가 아닌 commit에서 나왔으면
  `IAC_VERDICT_COMMIT_MISMATCH`로 사람에게 간다. `PASS`든 `FAIL`이든 같다 — 옛 `PASS`는 새
  commit을 평가한 적이 없고, 옛 `FAIL`은 이미 고쳐졌을 수 있어 그 위의 Patch가 사람이 쓴 수정을
  되돌린다. `IAC` Finding은 자기 관점의 판정을 다시 읽지 않으므로 이 대조의 대상이 아니다
- `MANUAL_ONLY`는 `TERRAFORM_PATCH`만 막는다. `ACTUAL_SYNC`는 새 변경을 합성하지 않고 사람이
  쓴 commit을 배포 대상으로 삼으므로, 적용의 파괴성은 Deployment Readiness의 refresh된 Plan과
  Human Approval이 판단한다 (ADR-0007)
- `AWS_ACTUAL`/`DRIFT` Finding은 같은 `Resource × Rule`의 IaC 판정이 `PASS`일 때만
  `ACTUAL_SYNC`가 된다. `OUT_OF_SCOPE`나 `EXECUTION_ERROR`는 안전으로 읽지 않는다

예외는 Registry에 커밋하지 않는다. 고객 데이터이므로 A가 고객 partition에 저장하고 판정 시점에
넘긴다. 허용 범위는 Rule과 함께 커밋되고, 예외는 고객이 등록하고 만료된다 — 수명이 다르다.

### 예외의 조회 시점 표시 (M3 B, ADR-0020 §6)

예외는 **평가 게이트가 아니다.** 위반이면 Finding은 그대로 생성·저장되고, 억제는 조회 시점에
예외를 join해 표시만 한다. `annotate_suppressed_findings(findings, *, customer_id, exceptions, at)`
가 그 경계이며 `FindingSuppression`(`finding_id`, `exception_id`, `reason`, `expires_at`,
`ticket_reference`) 목록을 **억제된 것만** 담아 입력 순서로 돌려준다. 목록의 부재가 곧 "억제 아님"
이다.

- `Finding`에도 결과 item에도 억제 필드를 추가하지 않는다. 예외는 만료되므로 저장된 억제는 만료
  이후 과거 사실을 왜곡한다. 반환값을 Finding 사본이 아니라 별도 값으로 둔 것도 같은 이유다 —
  저장 경로로 흘러도 Finding item에는 들어가지 않는다
- 억제 여부는 조치 판정과 **같은 술어**(`select_in_force_exception()`)로 정한다. 두 경계가 각자
  판정하면 화면의 "억제됨"과 `RemediationDecision.SUPPRESSED`가 갈리고, 어느 쪽이 거짓인지 알 수
  없다. 겹치는 예외의 우선순위(리소스 단위 > Rule 전체, 같으면 `exception_id` 순)도 공유한다
- 평가 시각은 `Finding.evaluated_at_utc`로 읽는다. 문자열 `evaluated_at`을 소비자가 각자 파싱하면
  같은 값에 서로 다른 파싱 규칙이 생기므로, `RemediationException.approved_at_utc`와 같은 방식으로
  Contract가 한 번만 정의한다. provenance가 없는 옛 record는 **억제하지 않는다** — 없는 평가 시각을
  조회 시각으로 대체하면 나중에 승인된 예외가 옛 위반을 덮는 경로가 그대로 열린다. 근거가 없으면
  위반이 보이는 쪽으로 닫는다
- `FindingSuppression`의 필드 검증은 Contract 공용 헬퍼(`require_non_empty_string`,
  `require_optional_non_empty_string`, `require_offset_aware_timestamp`)를 쓴다. 세 헬퍼는
  `packages.contracts`의 공개 export이며, 경계 밖에서 같은 규칙을 다시 쓰지 않게 하는 것이 목적이다.
  `expires_at`은 화면에 "언제까지"로 노출되므로 표시 경계에서도 offset-aware timestamp를 요구한다
- `at`은 조회 시각이다. 만료를 이 시각으로 판정하므로 같은 Assessment라도 조회 시점에 따라 억제
  표시가 사라질 수 있다. 그것이 의도다
- 억제 표시는 재평가를 막지 않는다. 검증 Assessment의 계획·Coverage·Readiness는 예외와 무관하게
  원 Assessment의 집합을 그대로 재실행한다 (ADR-0020 §2)

## M2 A/C remediation and readiness boundary

`packages/contracts/remediation.py`는 M1 Finding을 A의 정책·Job 경계와 C Remediation Worker,
D의 Patch/Plan port에 안전하게 연결한다. customer workload write나 Apply 요청은 표현하지 않는다
(ADR-0018).

- `RemediationDecision`은 API, 저장소, Worker가 사용하는 **유일한 action 정본**이다.
  `RemediationStrategy`는 존재하지 않는다.
- C의 `RemediationContext`는 authoritative `Finding`, exact `IaCSnapshot`, deduplicated evidence
  references만 보존한다. IAC/AWS_ACTUAL identity와 evidence를 검증하지만 action은 판정하지 않는다.
- A는 context/target/customer exception을 읽고 B의 `RemediationPolicy.decide()`를 호출한다.
  Actionable decision만 revision-zero Job과 최소 Outbox를 만들고 context/decision/audit와 원자 저장한다.
- `MANUAL_REVIEW`/`SUPPRESSED`는 정상 decision 응답이다. decision/audit만 기록하고 Job/Outbox는 없다.
- `RemediationStartResponse`는 항상 `decision`을 포함한다. Actionable은 `job`을 포함한 `202`,
  non-actionable은 `job: null`인 `200`이다.
- C의 revision-bound `RemediationWorker`는 `job_id + expected_revision`으로 authoritative work를
  다시 읽는다. `GENERATE_REMEDIATION ↔ TERRAFORM_PATCH`,
  `SYNC_ACTUAL_STATE ↔ ACTUAL_SYNC`만 허용하고 action에 맞는 injected D port 하나만 호출한다.
- D는 raw Plan을 immutable `TerraformPlan` artifact로 보관하고 C에 `PlanReadinessInput`만
  전달한다. C의 `DeploymentReadiness`는 exact `deployment_id`/`commit_sha`/`plan_hash`에
  바인딩되지만 Apply 권한은 아니다. A Admin 승인과 D OIDC 재검증이 계속 필요하다.

### Finding-to-remediation integration handoff

```text
A: POST /findings/{finding_id}/remediations
  -> JWT customer scope에서 C RemediationContext read
  -> A RemediationTarget + customer RemediationException read
  -> B RemediationPolicy.decide(finding, customer_id, target, at, exceptions)
  -> identity 검증
  -> MANUAL_REVIEW/SUPPRESSED: decision + audit만 저장, 200
  -> TERRAFORM_PATCH/ACTUAL_SYNC: context + decision + Job + Outbox + audit 원자 저장, 202
C RemediationWorker:
  -> job_id + expected_revision으로 authoritative work 재조회
  -> command/action/context binding 검증
  -> D PatchAction 또는 SyncAction 하나만 호출
  -> validated result를 idempotent store에 기록
```

Queue에는 `job_id`, `expected_revision`, `command`만 들어간다. `RUN_DEPLOYMENT`는 D Deployment
Worker 명령이며 C가 소비하지 않는다. D live GitHub/Terraform adapter와 customer runtime 배선은
이 mockable Contract의 구현 범위 밖이다.

### Rule item writer invariant

`RULE#{rule_id}#VERSION#{version}`의 customer-catalog writer는
`apps/backend/policy/bootstrap.py`의 Registry bootstrap 하나로 제한한다. bootstrap은 게시된
`POLICY_RULE` item에 `lifecycle=APPROVED`를 기록하고, `_matches_existing()`도 그 형태를
기준으로 비교한다. 기존에 lifecycle 없이 저장된 legacy item은 APPROVED로 정규화해 재실행을
허용하지만, 다른 writer가 다른 lifecycle이나 내용을 쓰면 계속 fail-closed한다. 따라서 같은
키에 lifecycle을 별도로 쓰는 두 번째 writer를 만들지 않는다.

## M3 contract additions

ADR-0020이 `Accepted`가 되면서 C-owned Contract는 `packages/contracts/`에 추가됐다. ADR-0019도
`Accepted`가 됐으므로 아래 A/D-owned Contract를 구현할 수 있다(서명 직후이며 구현 커밋은 이어서
추가한다). 역할마다 같은 의미의 값을 따로 만들지 않는다 — 같은 개념이 두 곳에 생기면 ADR-0018이
제거한 "판정 정본이 둘"인 구조가 재발한다.

| 추가 | 소유 | 의미 |
| --- | --- | --- |
| `DeploymentStatus` + `derive_deployment_status()` | A | Deployment 생애주기 위치의 **표현 타입과 파생 함수**. 저장하지 않는다 (ADR-0019 §8) |
| `AuditEventType` | A | 감사 event **종류** 어휘. 정본 필드명은 `event_type`이다 |
| `FindingResolution` | C | 구현됨. Finding 해소 여부의 5개 값 (ADR-0020 §4) |
| `AssessmentComparison` | C | 구현됨. before/after 비교 projection과 `comparable` 판정 (ADR-0020 §5) |
| `TERRAFORM_PLAN_BINARY` (ArtifactType) | D | apply가 사용하는 saved plan. hash 대상은 아니다 (ADR-0019 §1) |
| `RemediationSyncTarget` 이관 | C→Contract | 현재 `apps/backend/remediation/worker.py`에 있다 |

`DeploymentStatus`는 **DynamoDB에 저장하지 않는다.** API 응답 shape을 위한 표현 타입이며 값은
순수 함수 `derive_deployment_status()`가 `JobStatus`, `JobCurrentStep`, approval/rejection record,
apply run reference, verification 결과에서 read 시 계산한다. 저장하면 `JobStatus`·`JobCurrentStep`과
같은 사실의 두 번째 사본이 생긴다 (ADR-0019 §8). 표현 값의 전이는 다음과 같다.

```text
PLAN_REQUESTED → PLAN_COMPLETED → READINESS_EVALUATED → WAITING_APPROVAL
→ APPROVED → APPLYING → APPLIED → VERIFYING → VERIFIED
분기: BLOCKED, MANUAL_REVIEW, REJECTED, VERIFICATION_INDETERMINATE
```

`AuditEventType`(`packages/contracts/audit.py`)은 감사 event의 **종류**를 담는 StrEnum이고 정본
필드명은 `event_type`이다. `action`으로 통일하지 않는다 — `apps/backend/repositories/dynamodb.py`가
한 item에서 `event_type`(audit 종류)과 `action`(`RemediationAction` 값)을 다른 뜻으로 동시에 쓰고
있어, `action`으로 통일하면 두 값이 같은 키를 다툰다. `action`을 종류 필드로 쓰던 세 곳
(`repositories/deployment.py`의 `DEPLOYMENT_APPROVED`, `repositories/policy_approval.py`의
`POLICY_SOURCE_APPROVED`·`POLICY_PROFILE_PUBLISHED`)을 `event_type`으로 개명해, 다섯 writer가 모두
같은 필드명과 어휘를 쓴다. 현재 값은 위 셋에 `REMEDIATION_DECIDED`·
`REMEDIATION_EXCEPTION_APPROVED`를 더한 다섯이며, M3의 `DEPLOYMENT_REQUESTED`,
`DEPLOYMENT_REJECTED`, `APPLY_DISPATCHED`, `APPLY_COMPLETED`, `APPLY_FAILED`,
`POST_DEPLOY_VERIFIED`, `MANUAL_RECONCILIATION_REQUIRED`는 ADR-0019 서명(`Accepted`)에 따라 M3
구현 커밋에서 추가한다.

`PlannedEvaluation`은 계획된 `(resource_id, rule_id, perspective)` 좌표 하나이며 Assessment 계획의
단위다. `rule_version`은 일부러 없다 — version이 바뀐 좌표도 before/after가 짝을 이뤄야
`INDETERMINATE`를 표현할 수 있다(ADR-0020 §4). `AssessmentEvaluationPlan`은 이 좌표의 **집합**을
갖고 개수는 집합에서 파생하므로 둘이 어긋날 수 없다. `calculate_readiness_score`는 개수가 아니라
이 집합을 받아 완료 집합과 비교한다 — 개수 비교는 계획에 없던 평가가 누락된 평가의 자리를 채운
경우를 통과시킨다. `AssessmentCoverage`의 개수 필드는 그대로다.

`RemediationSyncTarget`은 D가 구현하는 `SyncAction` port의 반환형이므로 C의 앱 모듈이 아니라
`packages/contracts/remediation.py`에 있다. 역할 경계를 넘는 타입이 한 역할의 앱 코드에 있으면
다른 역할이 그 내부 모듈을 import해야 한다. `apps.backend.remediation`의 재노출은 유지한다.

`AssessmentComparison`은 두 immutable Assessment의 Coverage/Readiness와 Finding Resolution을
읽기 전용으로 묶는다. delta는 두 score가 존재하고, `(resource_id, rule_id, perspective)` 계획 집합,
`model_profile_id`, `rubric_version`이 모두 같을 때만 만든다. 그렇지 않으면 `comparable: false`,
`ComparisonIneligibilityReason`, `readiness_score_delta: null`을 반환한다. 계획 **개수**만 같은 것은
비교 가능 근거가 아니다. 비교 입력 report는 pagination cursor가 없어야 하고, 결과 좌표 집합과
Coverage count가 immutable planned 집합에 정확히 일치해야 한다. 불완전하거나 손상된 projection은
비교 전에 fail-closed로 거부한다. 이는 두 complete Assessment의 plan/profile/rubric/score 차이에서
반환하는 `comparable: false`와 구별되는 입력 validation이며, API는 이를 validation 오류로 변환한다.

`fixtures/m1/golden_dataset_cases.json`의 18개 `INITIAL` Case와
`fixtures/m1/golden_dataset_post_deploy_cases.json`의 18개 `POST_DEPLOY_VERIFICATION` Case는 각각
six S3 Rule × `IAC`/`AWS_ACTUAL`/`DRIFT`를 포함한다. Post-Deploy Case는 승인 apply 뒤 IaC와 Actual이
정합한 `PASS` snapshot이며 원 Assessment와 같은 assessment profile/rubric을 재사용한다.

### D 실행 port 시그니처 (M3 병렬 개발 전제)

M2에서 D live adapter가 늦어져 A/C가 대기한 상황을 반복하지 않으려면, 구현보다 port 시그니처를
먼저 고정해야 한다. 아래 표의 네 port는 모두 D가 소유한다. 이 중 A/C가 주입받아 Fixture/Mock으로
병렬 구현하는 것은 `ApplyDispatchPort`·`WorkflowRunReader`·`ActualRereadPort` **세 개**이며
(`Mockable` 의존성), `PlanRequestPort`는 D Worker 내부 호출이라 주입 대상이 아니어서 시그니처
확정에서 제외한다(절 끝의 근거 참조). 따라서 아래 확정 시그니처와 반환형은 세 port 기준이다.

| Port | 호출자 | 입력 → 출력 |
| --- | --- | --- |
| `PlanRequestPort` | D Deployment Worker 내부 | Deployment/commit → `TerraformPlan` + state `lineage`·`serial` + `PlanReadinessInput` |
| `ApplyDispatchPort` | D Deployment Worker | `DeploymentApproval` + `TerraformPlan` → dispatched run reference (idempotent) |
| `WorkflowRunReader` | D Deployment Worker | `run_id` → workflow path, repository, `ref`, conclusion, artifact digest |
| `ActualRereadPort` | C 검증 경계 | `AwsResourceQuery` → 재조회된 Actual Evidence (기존 read-only Tool 재사용) |

- 표의 네 port 모두 승인·정책 판정을 하지 않는다. 판정은 계속 A(승인)와 B(정책)가 소유한다.
- `ApplyDispatchPort`는 같은 approval로 두 번 호출돼도 새 run을 만들지 않아야 한다. 중복 방지의
  정본은 `APPROVED → APPLYING` 조건부 전이다 (ADR-0019 §5).
- `ActualRereadPort`는 새 표면이 아니라 M1 read-only AWS Resource Tool 재사용이다. 검증 단계에서
  write 표면이 생기지 않는다.

확정 대상 시그니처는 다음과 같다. 반환형 세 개(`ApplyRunReference`, `VerifiedRunOutcome`,
`AwsResourceSnapshot`)도 같은 결정에서 `packages/contracts/`에 정의한다 — 역할 경계를 넘는 타입을
앱 모듈에 두면 `RemediationSyncTarget`과 같은 이관 작업이 다시 생긴다.

```python
class ApplyDispatchPort(Protocol):
    """승인된 approval로 apply run을 dispatch한다. 이미 있는 run은 다시 만들지 않는다."""

    def dispatch_apply(
        self,
        *,
        approval: DeploymentApproval,
        state_lineage: str,
        state_serial: int,
        repository_id: str,
    ) -> ApplyRunReference: ...


class WorkflowRunReader(Protocol):
    """run_id로 Actions run을 재조회해 권위 있는 완료 사실을 만든다.

    EventBridge payload를 신뢰하지 않는다 (ADR-0019 §7).
    """

    def read_run(
        self, *, customer_id: str, repository_id: str, run_id: str
    ) -> VerifiedRunOutcome: ...


class ActualRereadPort(Protocol):
    """apply 후 AWS Actual을 다시 읽는다. 읽기 전용이며 write 표면이 없다."""

    def reread_actual(
        self, *, customer_id: str, aws_account_id: str, resource_ids: tuple[str, ...]
    ) -> tuple[AwsResourceSnapshot, ...]: ...
```

시그니처에서 합의할 점 세 가지.

- `dispatch_apply`가 `state_lineage`·`state_serial`을 받는 이유 — apply 직전 재검증을 D 안에서
  끝내고 A가 그 값을 해석할 필요를 없앤다. `serial` 단독으로는 state 재생성을 잡지 못한다
  (ADR-0019 §2).
- `read_run`이 예외를 던지지 않고 `VerifiedRunOutcome`을 돌려주는 이유 — 실패도 값이다
  (ADR-0017·0018의 규율).
- `reread_actual`이 `resource_ids`를 받는 이유 — 전체 재평가가 기본이어도(ADR-0020 §2) 읽기 대상은
  planned 집합에서 나오고, 재조회는 불일치한 리소스로 좁힌다 (ADR-0020 §8).

위에서 예고한 대로 `PlanRequestPort`는 D Worker 내부 호출이라 A/C가 주입받지 않는다. 그래서
시그니처 확정 대상에서 제외하며, 확정된 것은 나머지 세 port다.

## Contract change review

Contract 변경 PR은 변경 작성자와 해당 Contract의 Producer 및 Consumer Owner가 검토한다. Contract가 확정됐지만 구현체가 없는 경우 `Mockable` 상태로 Fixture/Mock을 사용해 병렬 개발할 수 있다.
