# ADR-0024: 사실·해석·모름 — 평가 경계를 의미로 긋는다

## Context

ADR-0002는 경계를 "Code는 범위와 유효성을, AI는 판정을"로 정의했고, e06c55e는 Catalog가 술어를
선언한 capability를 코드가 판정하게 했다. 그런데 실행 경로에서 실제로 분기하는 조건은
"Catalog가 `expectation`을 선언했는가" 하나였다. 선언이 없는 이유가 *해석이 필요해서*인지 *근거가
없어서*인지 코드는 구별하지 않았고, 그 결과 네 가지가 측정됐다(`docs/evaluations/data/`,
2026-09-04·05).

1. **근거 없는 PASS.** baseline Profile의 S3 Rule 넷(ACL·Bucket Policy·TLS·Logging)은 S3 read
   문서에 답이 존재할 수 없다. Catalog에 AWS binding이 없으므로 근거 게이트는 건너뛰었고, 모델은
   public-access-block 플래그를 대신 인용하며 PASS를 냈다.
2. **단위가 다른 두 점수의 평균.** 코드의 score는 관측 비율(분모가 리소스 개수), 모델의 score는
   실측상 0/100. 같은 위반이 어느 엔진을 지나갔느냐에 따라 준비도에 75점 차이로 기여했고,
   미암호화 볼륨 하나라는 같은 위험이 볼륨을 더 붙일수록(1+1 → 50, 19+1 → 95) 점수를 올렸다.
3. **"모름"과 "위반"이 같은 숫자.** `INSUFFICIENT_EVIDENCE`·`MANUAL_REVIEW`가 0점으로 평균에 들어가
   "확인 못 함 + 통과"와 "위반 + 통과"가 같은 50.0이 됐다.
4. **유령 drift.** AWS 쪽만 코드로 옮기자, 양쪽 모두 비준수인 케이스(S3 3/4, ALB HTTPS+HTTP)가
   코드 FAIL + 모델 PASS 조합이 되어 "IaC는 만족하나 AWS는 아니다"라는 실재하지 않는 drift로
   보고됐다.

## Decision

경계의 기준을 **물음의 종류**로 옮긴다. 종류는 셋이고, 각각 답하는 주체가 정해져 있다.

| 종류 | 정의 | 답하는 주체 | 결과 |
| --- | --- | --- | --- |
| 사실 | 선언된 위치의 값에 대한 술어 | 코드 | PASS/FAIL, `decided_by=CODE` |
| 해석 | 문언을 상황에 대응시키는 일 | 승인된 모델 | PASS/FAIL/OUT_OF_SCOPE…, `decided_by=MODEL` |
| 모름 | 답할 근거가 없다 | 코드 | `INSUFFICIENT_EVIDENCE`, 모델 호출 없음 |

### 1. score는 status의 재진술이다

`EvaluationResult.score`는 `score_for_status(status)`다 — PASS 100, FAIL 0, 판정이 아닌 status는
`NO_SCORE`(0.0). 모든 producer(모델 어댑터, 결정적 판정기, drift 파생, 수동 검토, M0 합성)가 이
규약을 따르며, 모델이 보낸 숫자는 계약 검증(범위·유한성)만 받고 버린다.

부분 충족은 점수가 아니라 **관측 상세**다: `observed_satisfied`/`observed_total`. 리포트는 이 값으로
"네 플래그 중 `RestrictPublicBuckets` 하나가 꺼져 있다"를 말하고, 조치는 이것을 최소 변경의
목표로 쓴다.

ADR-0003의 연속 점수와 Anchor 정책은 이 결정으로 **폐기**한다. 연속성은 severity 가중 평균이
만들고, 그것은 이미 코드가 한다.

### 2. 준비도는 판정된 좌표만 평균하고, 미판정은 따로 센다

`calculate_readiness_score`는 `STATUS_SCORES`(status → 기여값)를 severity로 가중 평균한다. 결과의
`score` 필드를 읽지 않는다. `INSUFFICIENT_EVIDENCE`와 `MANUAL_REVIEW`는 평균에서 빠지고
`ReadinessScore.undetermined_evaluations`로 보고된다. 판정된 좌표가 하나도 없으면 준비도는 `None`이다.

Coverage("실행됐는가")와 미판정("판정됐는가")은 다른 축이다. 화면은 둘을 함께 보여 준다.

### 3. 근거 게이트는 fail-closed다

`ActualBedrockEvaluator.evidence_gap()`은 Rule이 요구한 capability 각각에 대해 이 resource type의
AWS_ACTUAL binding이 **선언돼 있는지**, 선언돼 있다면 `document_paths`가 **채워졌는지**를 본다.
어느 쪽이든 빠지면 `INSUFFICIENT_EVIDENCE`이며 모델을 부르지 않는다.

legacy Rule(`evaluation_type is None`)도 같은 게이트를 지난다. `LEGACY_RULE_CONTROL_KEYS`는 회귀
대조표에서 **Runtime 조회 경로**가 됐다(`control_for_rule`). Catalog가 모르는 legacy Rule만 이전처럼
모델로 간다.

### 4. 판정 출처가 결과에 남는다

`EvaluationResult.decided_by: CODE | MODEL`. 기본값은 `MODEL`이다 — 이 필드 이전에 저장된 결과는
전부 모델 판정이었다. drift 파생은 두 관점의 판정 출처가 다르고 판정이 어긋나면 `FAIL`이 아니라
`MANUAL_REVIEW`를 낸다: 근거 체계가 다른 불일치를 사실로 주장하지 않는다. 출처가 같을 때의 규칙
(ADR-0011)은 그대로다.

## Consequences

- ADR-0002의 경계 정의는 이 문서의 세 종류로 읽는다. ADR-0003의 연속 점수·Anchor는 폐기.
  ADR-0016의 "evaluator의 0–100 score를 가중 평균" 문장은 "status 기여값을 가중 평균"으로 정정.
  ADR-0011·0020의 drift 파생은 §4의 출처 규칙이 앞선다. ADR-0023 §2의 게이트는 §3처럼 fail-closed.
- 결과 계약에 `decided_by`, `observed_satisfied`, `observed_total`이 additive로 추가된다. 저장된 옛
  결과는 `decided_by=MODEL`, 관측 상세 없음으로 복원된다.
- 준비도 계약에 `undetermined_evaluations`가 추가된다(기본 0).
- 측정 도구(`scripts/measure_score_consistency.py`)는 실제 실행 경로(`ActualBedrockEvaluator`)를
  지나야 하며, 코드 판정 좌표와 모델 판정 좌표의 지표를 나눠 보고해야 한다. 이 변경 자체가 그
  도구로 회귀 측정돼야 한다.
- Catalog가 AWS 근거를 선언하지 않은 Control(S3 ACL·Bucket Policy·TLS·Logging, EC2 서브넷)은
  이제 미판정으로 **보이게** 됐다. 그 빈 곳을 메우는 것(adapter 투영·read role 권한·술어 어휘)은
  별도 작업이다 — 보이지 않게 통과시키는 선택지는 없다.
