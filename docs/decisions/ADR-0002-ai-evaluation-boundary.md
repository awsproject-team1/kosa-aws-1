# ADR-0002: AI evaluation boundary

## Decision

Code는 Customer/AWS Account/Repository/Policy Profile 경계, 허용 Tool과 읽기 권한, 고객 데이터 격리, 출력 Schema, Evidence Reference, 상태 저장, Coverage를 검증한다. AI Evaluator는 허용된 Boundary 안에서 판정, 점수, Rationale과 허용된 Evidence 부분집합을 선택한다.

**2026-09-03 정정:** 초안은 AI가 적용 Rule과 Severity도 선택한다고 적었으나, 구현은 처음부터 적용 Rule을 Profile allow-list가, Severity를 승인된 Rule이 정하도록 했다(`apps/backend/assessment/bedrock.py`가 `severity=rule.severity`를 복원한다). 의도 문서의 "AI가 최종 권한을 갖지 않는 결정" 원칙에 코드가 맞으므로 문서를 코드에 맞춘다. Source Score/Risk는 구현되지 않았고 도입 계획도 없다.

## Consequences

AI 결과는 자유 텍스트가 아니라 검증 가능한 구조화 출력이어야 하며, Code는 평가 의미를 미리 결정하지 않고 안전한 범위와 결과 유효성만 강제한다. 점수 정책은 ADR-0003에서 관리한다.
