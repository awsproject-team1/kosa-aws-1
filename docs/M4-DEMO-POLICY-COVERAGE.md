# M4 Demo Policy·Rule·근거·Coverage 검증

이 문서는 ADR-0021의 별도 고객 sandbox repository 경계를 유지하면서 M4 B가 검증하는 항목을 설명한다. 정책 원문과 Demo Terraform 본문은 이 저장소에 두지 않는다. 실행 정본은 `fixtures/m4/demo_policy_coverage.json`과 `apps/backend/policy/demo.py`다.

## 고정 범위

- 시나리오: `wordpress-lamp-s3-governance-v1`
- Policy Profile: `profile-mvp-baseline@v2`
- 대상: `AWS::S3::Bucket`
- Rule: Profile allow-list의 6개 version-pinned S3 Rule
- 평가 좌표: 각 Rule의 `INITIAL`/`POST_DEPLOY_VERIFICATION` × `IAC`/`AWS_ACTUAL`/`DRIFT`, 총 36개
- 근거: Rule `SourceReference`와 해당 Rule을 인용하는 Control의 version-pinned `SourceReference`

검증 명령:

```bash
python3 scripts/validate_m4_demo_policy_coverage.py
```

성공 결과는 Rule 6개, Control 5개, 고유 정책 근거 12개, Golden Case 36개를 보고한다. Profile/Rule version, Control mapping, SourceReference locator, case ID, phase 또는 perspective가 정본과 다르면 실패한다.

## Demo toggle handoff (B → D)

`demo_toggle`은 외부 Demo repository가 Terraform 변수/모듈 토글에 매핑해야 하는 안정된 의미 키다. 변수 이름이나 구현을 이 저장소에서 강제하지 않지만, D runbook은 각 키의 초기 위반 상태와 apply 후 기대 상태를 기록해야 한다.

| demo_toggle | Rule | 설명 |
| --- | --- | --- |
| `block_public_access` | `S3-PUBLIC-001@2026-08-31` | S3 Block Public Access 네 설정 |
| `object_ownership_enforced` | `S3-ACL-001@2026-08-31` | ACL 의존 제거와 Object Ownership |
| `bucket_policy_restricted` | `S3-POLICY-001@2026-08-31` | Bucket Policy 접근 범위 제한 |
| `default_encryption` | `S3-ENCRYPT-001@2026-08-31` | 기본 서버 측 암호화 |
| `tls_only` | `S3-TLS-001@2026-08-31` | 비 TLS 요청 거부 |
| `access_logging` | `S3-LOGGING-001@2026-08-31` | 서버 액세스 로깅 |

외부 repository가 이 여섯 키 중 하나를 구현하지 않거나 다른 Rule version을 대상으로 하면 M4 Demo Coverage를 충족하지 않는다. 실제 repository 식별자, commit SHA, plan hash와 실행 증적은 customer-approved runtime에서 생성하고 릴리스 evidence manifest에 별도로 결합한다.

## Coverage 해석

- **평가 Coverage**는 Profile의 6 Rule × 두 phase × 세 perspective 좌표가 모두 존재하는지 설명한다. `EXECUTION_ERROR`는 완료로 세지 않는 기존 Assessment Coverage 규칙을 그대로 따른다.
- **Control Coverage**는 이번 S3 Profile이 어떤 상위 Control을 인용하는지 설명한다. Control에 EC2 등 Profile 밖 Rule이 함께 있으면 해당 Control 전체가 완전 평가됐다는 뜻이 아니다. `ControlRuleCoverage`의 evaluated/total을 사용한다.
- **DRIFT**는 별도 AI 정책 판정이 아니라 같은 Rule의 IAC/Actual 결과에서 Code가 결정적으로 파생한다. Golden Case 좌표에는 포함되지만 실제 Bedrock 호출 수로 세지 않는다.
- **근거 Coverage**는 정책 원문 문장이 아니라 `{source_id}@{source_version}#{locator}`와 content hash로 추적한다. 원문이나 추출 text를 release report에 복사하지 않는다.

## A·C·D 결합 규칙

- A는 Assessment의 planned/completed 좌표와 관측 집계만 제공하며 B manifest의 Rule/근거를 바꾸지 않는다.
- C는 exact case ID와 승인 Model Profile/rubric으로 반복 평가하고 결과 집계를 release evidence에 제공한다.
- D는 외부 repository의 toggle 매핑과 commit/plan/apply 사실을 제공한다. 저장소에 Demo IaC를 복사해 이 검증을 우회할 수 없다.
- 세 입력 중 하나라도 누락되면 M4 전체 release gate는 미충족이다. Fixture validator 통과를 실제 sandbox E2E 통과로 표현하지 않는다.
