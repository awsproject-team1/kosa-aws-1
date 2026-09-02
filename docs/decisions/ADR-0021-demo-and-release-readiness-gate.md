# ADR-0021: Demo와 Release readiness gate

> **상태: Accepted (2026-09-02)** — M4는 이 release gate를 따른다. 실제 고객 sandbox 데모와
> 릴리스 PR은 이후 실행 증적을 채워야 하며, 이 결정만으로 Gate가 통과한 것은 아니다.
>
> **결정 대상:** 데모 IaC를 어디에 두는지, 품질 Gate 미달이 릴리스를 막는지, 관측·비용 검증의
> 통과 기준이 무엇인지, `dev → main` PR에 무엇을 첨부하는지.
>
> **관련:** ADR-0001, ADR-0003, ADR-0004, ADR-0008, ADR-0019, ADR-0020

## Context

M4 Exit criteria는 "WordPress/LAMP Demo에서 폐루프 E2E가 재현되고, 품질·운영·문서 기준을 충족해
사람이 `dev → main` 통합 PR을 만들 수 있다"다. 현재 다음이 정해지지 않아 역할마다 다른 준비를
하게 된다.

1. **데모 Terraform의 위치.** `fixtures/terraform/`은 비어 있고, 데모 IaC가 이 저장소에 들어가는지
   별도 고객 repository에 들어가는지 문서에 없다.
2. **품질 Gate의 차단력.** 목표는 PASS/FAIL 정확도·Evidence 정확도·판정 일치율 90% 이상과 Score
   편차 ±10점 이내다. 미달일 때 릴리스를 막는지, 목표를 조정하는지, 누가 판단하는지 없다.
   세 관점 Golden fixture는 추가됐지만 protected customer runtime의 반복 평가 리포트는 아직 없다.
3. **관측·비용 검증의 통과 기준.** "오류·성능·비용 관측 검증"이 무엇을 측정하면 충족인지 없다.
4. **`dev → main` PR의 필수 첨부물.** `CONTRIBUTING.md`의 Done 기준은 일반 PR 기준이며 릴리스
   PR에 필요한 산출물 목록이 없다.

## Decision

### 1. 데모 Terraform은 별도 고객 repository에 둔다

- 데모 WordPress/LAMP Terraform은 팀이 소유한 **별도 sandbox repository**에 두고, 이 저장소에는
  참조(repository 식별자와 시나리오 문서)만 남긴다.
- 이유: apply 경로는 "승인된 고객 Repository에 GitHub App으로 접근한다"는 경계(ADR-0007)를 통과해야
  실제로 검증된다. 데모 IaC를 플랫폼 저장소에 두면 그 경계를 우회한 채 데모만 성공한다.
- 데모 저장소는 ADR-0019의 전제조건을 갖춘다: `.terraform.lock.hcl` 커밋, plan/apply workflow
  수동 설치, protected Environment, state backend/lock table.
- 의도적 위반은 S3 Rule 6건에 1:1 대응하는 변수/모듈 토글로 만들고, 어떤 토글이 어떤 Rule을
  위반시키는지 데모 runbook에 기록한다. 위반을 코드에 상수로 박지 않는 이유는 데모 전후 상태를
  같은 저장소에서 재현해야 하기 때문이다.
- 데모 계정은 승인된 sandbox 계정을 사용하고 `EXPECTED_AWS_ACCOUNT_ID` 검증을 그대로 통과한다.

### 2. 품질 Gate는 릴리스를 막는다

- `dev → main` PR은 Golden Dataset 반복 평가 리포트를 첨부한다. 목표 미달이면 릴리스를 진행하지
  않는다.
- 미달 시 선택지는 두 개이며 둘 다 문서로 남긴다.
  - rubric/prompt/Golden Case를 재고정하고 재실행한다.
  - Score 편차가 지속적으로 ±10점을 넘으면 Anchor 전환을 ADR-0003 절차로 결정한다.
- 목표치를 낮추는 판단은 개인이 하지 않는다. 목표 변경은 `docs/PRD.md`와 ADR 개정으로만 한다.
- M4 이전에 IAC/DRIFT 관점 Golden Case를 추가해 세 관점 모두 목표를 측정할 수 있게 한다. 관점
  하나를 측정하지 못한 상태의 리포트는 Gate 통과 근거로 쓰지 않는다.

### 3. 관측·비용 검증의 통과 기준

데모 폐루프 1회 실행에 대해 다음을 runbook에 기록하고, 값이 비어 있으면 미충족으로 본다.

| 항목 | 기준 |
| --- | --- |
| Assessment 성공률 | 계획된 평가 중 `EXECUTION_ERROR` 0건 |
| Bedrock 호출 | 역할별 호출 수, 토큰, p95 지연 기록 |
| Queue 건전성 | DLQ depth 0, Queue age 최대값 기록 |
| Job 재개 | checkpoint 재개 횟수와 3분 재큐잉 동작 기록 |
| plan/apply | 실패 0건, 승인 없는 apply 0건 |
| 감사 | Remediation·Approval·Apply·Verification audit event가 모두 존재 |
| 비용 | 데모 1회의 Bedrock·Lambda·저장소 비용 합계 기록 |

- 비용은 절대 상한을 두지 않는다. 최초 실행값을 기준선으로 남기고 이후 회귀를 비교한다.
- 민감한 Prompt·정책 원문·IaC 전체가 로그에 없음을 같은 실행에서 확인한다.

### 4. `dev → main` PR 필수 첨부물

- 데모 폐루프 E2E 실행 기록 (Assessment → Finding → Remediation → PR → plan → 승인 → apply →
  Post-Deploy Verification)
- Golden Dataset 반복 평가 리포트와 목표 대비 결과 (2번)
- 관측·비용 기록 (3번)
- Secret scan과 Python/Frontend/Terraform 검증 결과
- 문서 Freshness 확인: `docs/PRD.md`, `docs/DESIGN.md`, `docs/API.md`, `docs/CONTRACTS.md`,
  `docs/DATABASE.md`, `docs/architecture/`, `docs/decisions/`가 구현과 일치하고 `Proposed` ADR이
  남아 있지 않음
- `PROGRESS.md`의 M0–M3 Exit criteria 충족 상태

## Consequences

- 데모가 실제 고객 repository 경계를 통과하므로 데모 성공이 제품 경로의 검증이 된다. 반면 데모
  준비에 별도 저장소·Environment·state backend 설정이 필요하다.
- 품질 Gate가 릴리스를 막으므로 M4 이전에 Golden Case 확장이 필수 작업이 된다.
- 관측 기준이 값의 존재로 정의되므로 "관측했다"의 판단이 사람마다 달라지지 않는다.
- 릴리스 PR 체크리스트가 고정되어 역할별 준비물이 사전에 분배된다.

## Rejected alternatives

- **데모 Terraform을 이 저장소 `fixtures/terraform/`에 두기:** GitHub App·OIDC·승인 Repository
  경계를 우회하므로 거부한다. 데모가 통과하고 제품 경로는 검증되지 않는 상태가 된다.
- **품질 Gate를 권고로 두기:** 목표 미달을 개인이 판단해 릴리스할 수 있게 되므로 거부한다.
- **비용 상한을 지금 숫자로 고정:** 실측 기준선이 없어 임의 값이 되므로 거부한다. 기준선 기록으로
  대체한다.

## Open decision

- **Owner:** Shared(릴리스 게이트) + D(데모 IaC와 runbook) + C(품질 Gate 리포트) + A(관측·비용 기록)
- **Needed by:** M4 착수 전. 데모 저장소 결정은 M3 D의 apply 경로 검증 대상과 같으므로 M3 중반에
  필요하다.
- **Blocks:** M4 전체 항목과 `dev → main` 통합 PR.
- **Proposed options:** 위 Decision 4개 항목.
- **Final record (2026-09-02):** Decision 1–4를 채택한다. C는 여섯 S3 Rule의
  `IAC`/`AWS_ACTUAL`/`DRIFT` Golden Case(총 18개)가 version-pinned fixture 및 반복 gate contract로
  실행됨을 확인했다. 고객 Bedrock 반복 실행 리포트는 protected customer runtime과 demo IaC가 준비된
  뒤 이 Gate의 증적으로 별도 첨부한다.
