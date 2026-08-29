# ADR-0002: AI evaluation boundary

## Decision

Code는 Customer/AWS Account/Repository/Policy Profile 경계, 허용 Tool과 읽기 권한, 고객 데이터 격리, 출력 Schema, Evidence Reference, 상태 저장, Coverage를 검증한다. AI Evaluator는 허용된 Boundary 안에서 적용 Rule, 필요한 Evidence, 판정, Severity, 점수, Rationale, Source Score/Risk를 선택한다.

## Consequences

AI 결과는 자유 텍스트가 아니라 검증 가능한 구조화 출력이어야 하며, Code는 평가 의미를 미리 결정하지 않고 안전한 범위와 결과 유효성만 강제한다. 점수 정책은 ADR-0003에서 관리한다.
