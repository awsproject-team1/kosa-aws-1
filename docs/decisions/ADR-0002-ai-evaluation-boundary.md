# ADR-0002: AI evaluation boundary

## Decision

Code는 Customer/AWS Account/Repository/Policy Profile 경계, 허용 Tool과 읽기 권한, 고객 데이터 격리, 출력 Schema, Evidence Reference, 상태 저장, Coverage를 검증한다. AI Evaluator는 허용된 Boundary 안에서 판정, 점수, Rationale과 허용된 Evidence 부분집합을 선택한다.

**2026-09-03 정정:** 초안은 AI가 적용 Rule과 Severity도 선택한다고 적었으나, 구현은 처음부터 적용 Rule을 Profile allow-list가, Severity를 승인된 Rule이 정하도록 했다(`apps/backend/assessment/bedrock.py`가 `severity=rule.severity`를 복원한다). 의도 문서의 "AI가 최종 권한을 갖지 않는 결정" 원칙에 코드가 맞으므로 문서를 코드에 맞춘다. Source Score/Risk는 구현되지 않았고 도입 계획도 없다.

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

## 정정 2026-09-05 — 경계는 물음의 종류로 긋는다 (ADR-0024)

"AI Evaluator가 판정과 점수를 선택한다"는 이 문서의 Decision은 **해석**이 필요한 물음에만 해당한다.
선언된 위치의 값에 대한 술어(**사실**)는 코드가 판정하고, 답할 근거가 없는 좌표(**모름**)는 코드가
`INSUFFICIENT_EVIDENCE`로 닫으며 모델을 부르지 않는다. 점수는 어느 주체도 선택하지 않는다 —
status가 정한다(`score_for_status`). 모든 결과는 `decided_by`로 판정 주체를 남긴다.
