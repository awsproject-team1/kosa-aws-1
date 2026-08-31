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
  "evidence_references": ["source locator or content hash"],
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
- `SourceReference`: 정책 원문 안의 locator와 content hash. Rule과 평가 Evidence는
  이 값을 이용해 추적한다.
- `PolicyRule`: versioned rule, severity, 적용 평가 단계, Resource 유형과 하나 이상의
  Source Reference
- `PolicyRuleReference`: Rule ID와 version을 함께 고정하는 Profile 참조
- `PolicyProfile`: `rule_references`로 구성된 versioned allow-list. Repository/AWS Account 권한은
  Profile이 아니라 Backend의 JWT scope에서 강제한다.

Policy Context Tool은 선택된 Profile의 Rule과 Source Reference만 전달한다. AI가
Profile 밖의 Rule 또는 임의 Policy Source를 선택할 수 없다.

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
