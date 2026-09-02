# M4 데모 IaC 참조와 시나리오 (ADR-0021 §1)

이 문서는 WordPress/LAMP 데모의 Terraform이 **어디에 있는지**와 **무엇을 위반시키는지**만
기록한다. 데모 Terraform 원본은 이 플랫폼 저장소에 두지 않는다 — ADR-0021 §1에 따라 팀이
소유한 **별도 sandbox repository**에 두고, 이 저장소에는 참조(repository 식별자와 시나리오)만
남긴다. `fixtures/terraform/`이 비어 있는 것은 이 결정의 결과이지 누락이 아니다.

데모 IaC를 플랫폼 저장소에 두면 "승인된 고객 Repository에 GitHub App으로 접근한다"는 apply
경계(ADR-0007, ADR-0019 §6)를 우회한 채 데모만 성공하게 된다. 데모가 실제 고객 repository
경계를 통과해야 데모 성공이 곧 제품 경로의 검증이 된다.

## 1. 데모 저장소 식별자

데모 IaC는 아래 좌표의 **별도 저장소**에 둔다. 실제 owner/repository 이름과 sandbox 계정 ID는
승인된 값으로 채우며, 원문·자격 증명은 이 저장소에 커밋하지 않는다.

| 항목 | 값(승인 시 확정) | 비고 |
| --- | --- | --- |
| Repository | `<DEMO_OWNER>/<DEMO_REPOSITORY>` | 데모 전용, 플랫폼 저장소와 분리 |
| Default branch | `main` | apply 대상은 이 branch의 merge commit |
| AWS 계정 | `<EXPECTED_AWS_ACCOUNT_ID>` | 승인된 sandbox 계정, `EXPECTED_AWS_ACCOUNT_ID` 검증 통과 |
| Region | `<AWS_REGION>` | 승인된 Profile Region과 일치 |
| 대상 리소스 | S3 버킷(6개 S3 Rule 대상) | LAMP/WordPress 스택 중 S3 부분 |

`<...>` 자리는 데모 실행 직전에 플랫폼의 Deployment 생성·runtime configuration에 주입한다.
이 문서에는 실제 값을 남기지 않는다.

## 2. 데모 저장소 전제조건 (ADR-0019, ADR-0021 §1)

데모 저장소는 apply 경로가 검증되도록 아래를 갖춘다. 이는 고객 IaC repository와 같은 조건이다.

- **Terraform version pin (§1):** `.terraform.lock.hcl`을 커밋하고 provider 버전을 고정한다.
  버전이 흔들리면 같은 commit에서 다른 plan이 나와 `plan_hash` 재검증이 깨진다.
- **plan/apply workflow 수동 설치 (§6):** 이 저장소의 `ci/terraform/terraform-plan.yml`과
  `ci/terraform/terraform-apply.yml`을 데모 저장소 `.github/workflows/`로 **복사**해 설치한다.
  Platform(GitHub App)은 이 파일을 만들거나 수정하지 않는다(App에 `workflows: write` 없음).
- **protected Environment (§6):** apply job은 required reviewers가 붙은 protected Environment를
  2차 게이트로 둔다(`customer-terraform-apply`).
- **OIDC Role 분리 (ADR-0007, §6):** plan job은 `TerraformPlanRole`, apply job은
  `TerraformDeploymentRole`을 assume한다. OIDC trust는 exact repository·environment subject로
  제한한다.
- **state backend/lock (§2):** versioned·encrypted·TLS-only·bucket-owner-enforced S3 bucket과
  DynamoDB lock table. state key는 `(repository_id, workspace)`로 분리하고 workspace 이름은
  `{customer_id}-{repository_id}`다.

## 3. 위반 토글과 S3 Rule 1:1 매핑

의도적 위반은 코드에 상수로 박지 않고 **변수/모듈 토글**로 만든다. 데모 전후 상태를 같은
저장소에서 재현해야 하기 때문이다(ADR-0021 §1). 각 토글은 여섯 S3 Rule
(`fixtures/rules/rules.s3.json`, version `2026-08-31`) 중 정확히 하나에 대응한다.

토글 이름은 `docs/M4-DEMO-POLICY-COVERAGE.md`가 정한 **`demo_toggle` 안정 의미 키**를 그대로
쓴다. 그 여섯 키가 B → D 인계 계약이므로, 이 문서가 다른 이름을 예시로 들면 데모 저장소가 어느
쪽을 따라야 하는지 갈린다.

**극성은 준수 = `true`** 다. 안정 키가 준수 상태를 가리키는 이름(`block_public_access`,
`default_encryption` …)이므로 그 방향을 그대로 따르며, `disable_encryption = false` 같은
이중부정을 만들지 않는다.

- 토글 `false` → **위반 상태.** Initial Assessment에서 해당 Rule이 `FAIL`로 잡힌다.
- 토글 `true` → **준수 상태.** Post-Deploy Verification에서 해소(Resolution)로 확인된다.

`remediation` 열은 `fixtures/rules/remediation.json`의 eligibility로, 데모에서 자동 Patch가
열리는지 Manual Review로 떨어지는지를 나타낸다.

| `demo_toggle` | S3 Rule | severity | 위반 상태(토글 `false`) | 준수 상태(토글 `true`) | remediation |
| --- | --- | --- | --- | --- | --- |
| `block_public_access` | `S3-PUBLIC-001` | CRITICAL | `aws_s3_bucket_public_access_block` 없음 또는 모두 `false` | 네 플래그 모두 `true` | AUTOMATIC |
| `object_ownership_enforced` | `S3-ACL-001` | MEDIUM | `acl`/`aws_s3_bucket_acl`로 접근 관리 | ACL 미사용, `BucketOwnerEnforced` | AUTOMATIC |
| `bucket_policy_restricted` | `S3-POLICY-001` | HIGH | Bucket Policy가 `Principal:*`/넓은 네트워크 허용 | 범위 제한 Policy | MANUAL_ONLY |
| `default_encryption` | `S3-ENCRYPT-001` | HIGH | SSE 미설정 | `aws_s3_bucket_server_side_encryption_configuration` 설정 | MANUAL_ONLY |
| `tls_only` | `S3-TLS-001` | MEDIUM | `aws:SecureTransport` deny 없음 | TLS-only deny statement | AUTOMATIC |
| `access_logging` | `S3-LOGGING-001` | MEDIUM | `aws_s3_bucket_logging` 없음 | 서버 액세스 로깅 활성화 | MANUAL_ONLY |

토글 조합은 데모 runbook(`docs/M4-DEMO-RUNBOOK.md`)의 시나리오 단계에서 지정한다. 데모 저장소가
Terraform 변수 이름을 다르게 쓰더라도 위 여섯 키에 1:1로 매핑해야 하며, 그 매핑을 데모 저장소
README에 남긴다. 여섯 키 중 하나라도 구현하지 않거나 다른 Rule version을 대상으로 하면 M4 Demo
Coverage를 충족하지 않는다.

## 4. 세 관점(IAC/AWS_ACTUAL/DRIFT) 재현

데모는 세 관점을 모두 산출한다(ADR-0011, ADR-0016).

- **IAC:** 토글이 위반 상태(`false`)인 commit의 Terraform 본문을 평가한다.
- **AWS_ACTUAL:** apply 후 read-only AWS Resource Tool로 실제 리소스를 재조회해 평가한다.
- **DRIFT:** 두 판정의 불일치를 Code로 파생한다(AI 판정 아님).

Initial Assessment는 토글이 위반 상태(`false`)일 때, Post-Deploy Verification은 Remediation과
토글 전환(`true`) apply 뒤에 같은 Profile·rubric으로 재평가한다(ADR-0020). 두 Assessment의 planned
`(resource_id, rule_id, perspective)` 집합은 동일해야 비교가 성립한다(fail-closed).

## 관련 문서

- 실행 절차: `docs/M4-DEMO-RUNBOOK.md`
- 실행 경계 결정: `docs/decisions/ADR-0019-approved-deployment-execution-boundary.md`
- 재평가·비교 결정: `docs/decisions/ADR-0020-post-deploy-verification-and-comparison.md`
- 데모·릴리스 gate: `docs/decisions/ADR-0021-demo-and-release-readiness-gate.md`
- workflow template: `ci/terraform/README.md`
