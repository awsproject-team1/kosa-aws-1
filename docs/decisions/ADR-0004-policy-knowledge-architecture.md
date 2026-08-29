# ADR-0004: Policy knowledge architecture without RAG

## Context

MVP는 사내 정책과 ISMS-P 근거를 AI 평가에 제공해야 하지만, 초기 문서 규모에서는 RAG/Vector DB/Bedrock Knowledge Base의 운영 복잡도가 가치보다 클 수 있다. 동시에 어떤 정책 근거를 사용했는지는 추적 가능해야 한다.

## Decision

MVP에서는 RAG, Vector DB, Bedrock Knowledge Base를 사용하지 않는다. 정책 원문은 S3에 저장하고, Policy Source, Rule, Control, Mapping, Source Reference, Exception/Manual Context는 구조화해 관리한다. Policy Context Tool은 승인된 Policy Profile 범위의 Context만 정규화해 AI Evaluator에 전달한다.

## Consequences

Evidence에는 원문 locator 또는 content hash를 기록한다. 정책 문서가 Context Window를 초과할 정도로 증가하면 Retrieval 도입 여부를 별도 ADR로 재검토한다.
