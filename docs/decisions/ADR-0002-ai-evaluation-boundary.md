# ADR-0002: AI evaluation boundary

## Decision

Code는 Customer/AWS Account/Repository/Policy Profile 경계, 허용 Tool과 읽기 권한, 고객 데이터 격리, 출력 Schema, Evidence Reference, 상태 저장, Coverage를 검증한다. AI Evaluator는 허용된 Boundary 안에서 적용 Rule, 필요한 Evidence, 판정, Severity, 점수, Rationale, Source Score/Risk를 선택한다.

## Consequences

AI 결과는 자유 텍스트가 아니라 검증 가능한 구조화 출력이어야 하며, Code는 평가 의미를 미리 결정하지 않고 안전한 범위와 결과 유효성만 강제한다. 점수 정책은 ADR-0003에서 관리한다.

## Rule applicability mechanism

"AI Evaluator가 적용 Rule을 선택한다"는 것은 **Plan 분모에서 Rule을 빼는 것이 아니라**, 각
`Resource × Rule × Perspective` 좌표에 대해 AI가 적용 여부까지 판정하는 것으로 구현한다.

- **Code는 Plan을 넓게 고정한다.** `PolicyContextResolver`가 Profile allow-list에서 phase·
  resource_type이 맞는 Rule을 모두 planned coordinate로 확정한다. 이 집합이 Coverage 분모이자
  M3 before/after 비교 경계다(ADR-0016, ADR-0020 §5). AI 판단으로 분모를 바꾸면 완료 집합이
  planned와 달라져 Readiness가 영구히 `None`이 되고 두 Assessment가 comparable하지 않게 된다.
- **AI는 적용성을 `OUT_OF_SCOPE`로 판정한다.** 어떤 Rule이 그 Resource를 실제로 규율하지 않으면
  AI는 `PASS`/`FAIL`이 아니라 `OUT_OF_SCOPE`를 반환한다. Coverage는 이를 completed로 세어 Plan을
  채우고, Readiness는 `_NON_SCORING_STATUSES`로 점수에서 제외하며, `OUT_OF_SCOPE`는 Finding이
  되지 않는다. 이렇게 "AI가 적용 Rule을 선택"하면서도 Coverage·감사·재현성 계약을 보존한다.
- Profile을 관리자가 정의하는 것은 **선택 가능한 Rule의 boundary(allow-list)**를 정하는 것이고,
  그 안에서 어떤 Rule이 실제로 적용되는지는 AI가 좌표별로 판정한다.
