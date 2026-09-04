# ADR-0003: Continuous scoring with reliability-triggered anchors

## Context

AI scoring must express nuanced compliance evidence without relying on a Code formula, while repeated evaluations need predictable variance.

## Decision

AI Evaluator uses a continuous 0–100 score by default. Golden Dataset evaluation targets PASS/FAIL accuracy, Evidence Reference accuracy, and same-case agreement of at least 90%, with repeated Score variance within ±10 points. If the variance persistently exceeds that threshold, enable the fixed Anchor set `{0, 15, 30, 50, 70, 85, 100}` and define its Rubric meanings before use.

## Consequences

Score policy, model, prompt, rubric, rule, evidence references, Token/Latency, and validation results must be recorded. Model, Prompt, Rubric, Rule, Policy Document, Context Retrieval, or Tool changes require Golden Dataset and repeated-run regression evaluation.

## 정정 2026-09-05 — 연속 점수와 Anchor를 폐기한다 (ADR-0024)

이 문서의 전제("AI scoring must express nuanced compliance evidence")는 측정으로 반증됐다.
72회 라이브 평가에서 모델의 score는 0과 100뿐이었고(`docs/evaluations/data/score-validity-20260905.md`),
코드의 관측 비율은 분모가 리소스 개수라 같은 위험이 리소스를 더 붙일수록 점수를 올렸다.

- `score`는 status의 재진술이다: PASS 100, FAIL 0, 판정 아닌 status 0 (`score_for_status`).
  `ScoringMode.ANCHORED`와 `SCORE_ANCHORS`는 계약에 남아 있으나 어느 producer도 쓰지 않는다.
- 부분 충족은 `observed_satisfied`/`observed_total`로 결과에 남는다.
- 준비도는 결과의 `score`가 아니라 status 기여값(`STATUS_SCORES`)을 severity로 가중 평균하며,
  `INSUFFICIENT_EVIDENCE`·`MANUAL_REVIEW`는 `undetermined_evaluations`로 따로 센다.
- 회귀 측정 지표는 나뉜다: 모델 판정 좌표는 status 반복 일치와 기대 status 정확도, 코드 판정
  좌표는 정확도만. 코드 판정에 분산 지표를 매기는 것은 정보가 아니다.
- Golden Case의 `expected_score_min/max`는 status 고정값(FAIL 0, PASS 100)을 포함하는 범위여야
  한다. 기존 fixture(FAIL 0–30, PASS 100)는 그대로 성립한다.
