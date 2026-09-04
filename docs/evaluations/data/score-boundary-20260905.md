# 경계 재설정 후 계측 — 2026-09-05

ADR-0024가 바꾼 것(status 고정 점수, 미판정 분리, fail-closed 게이트, `decided_by`, drift 출처
규칙)을 **어떤 도구로 확인하는가**를 기록한다. 이 파일은 dry-run 수치와 라이브 실행 지시만 담는다.
dry-run은 가짜 모델이므로 모델 정확도의 근거가 아니다 — 여기서 근거인 것은 **어느 좌표가 어느
경로를 지나는가**뿐이다.

## 1. 계측기가 실제 경로를 지난다

`scripts/measure_score_consistency.py`의 첫 버전은 `BedrockStructuredEvaluator`를 직접 만들었다.
그러면 `ActualBedrockEvaluator`가 하는 근거 게이트와 결정적 판정이 측정에서 빠진다 — e06c55e가
고쳤다고 주장한 세 false negative를, 그 세 건을 찾아낸 도구로 확인할 수 없었다.

이제 AWS_ACTUAL Case는 Case 문서를 `MockAwsResourceTool`의 응답으로 실어 `ActualBedrockEvaluator`를
지난다. 게이트 → 결정적 판정 → 모델 순서가 Worker와 같다. IAC Case는 그대로 모델 어댑터다.

지표는 판정 주체별로 나뉜다(`by_decision_source`). 코드 판정 좌표는 기대 status 정확도만, 모델
판정 좌표는 정확도와 반복 일치를 본다. `model_calls`는 실제 Bedrock 호출 횟수다.

## 2. dry-run 분포 (24 Case × 2회)

| 판정 주체 | Case | 실행 | 비고 |
| --- | --- | --- | --- |
| CODE | **13** | 26 | Bedrock 호출 0회. 측정된 false negative 세 건 중 둘(`s3-three-of-four-actual`, `alb-https-plus-http-actual`)이 여기 있고 둘 다 FAIL |
| MODEL | 11 | 22 | IAC 8건 전부 + EC2 공인 IP 2건 + `rds-private-open-sg-actual` |

Bedrock 호출은 48회 실행 중 **22회**다. 같은 Case 집합을 예전 harness로 돌리면 48회였다.

모델 경로에 남은 AWS_ACTUAL 세 건은 각각 이유가 다르다.

- `ec2-public-ip-actual` / `ec2-private-actual` — Rule의 전제("private tier")를 문서가 말하지
  못한다. adapter가 서브넷 속성을 읽지 않는다(기존 문서화된 gap).
- `rds-private-open-sg-actual` — "0.0.0.0/0이 3306을 여는가"는 사실인데 술어 어휘에 CIDR이 없다.
  dry-run에서는 가짜 모델이 `INSUFFICIENT_EVIDENCE`를 냈고, 라이브 측정에서는 실제 모델이
  `OUT_OF_SCOPE`로 회피한 그 Case다.

세 건 모두 다음 작업(adapter 투영·술어 어휘)의 대상이며, 그 뒤에는 코드 경로로 넘어온다.

## 3. 라이브 회귀 측정 — 아직 실행되지 않음

ADR-0003이 요구하는 회귀 측정은 Bedrock 자격 증명이 있는 환경에서 아래로 실행한다. 결과는
이 디렉터리에 `score-boundary-<date>-live.md`로 남긴다.

```bash
AWS_PROFILE=<mfa session> python scripts/measure_score_consistency.py \
  --repetitions 5 --output boundary.json --markdown boundary.md
```

읽을 것:

- `by_decision_source.CODE.expected_status_accuracy`는 1.0이어야 한다. 아니면 술어나 문서 모양이
  틀린 것이지 모델 문제가 아니다.
- `by_decision_source.MODEL`의 정확도·일치율을 `score-validity-20260905.md`의 값(21/24, 일치 8/9)과
  나란히 둔다. 이번 변경은 모델 prompt에서 등급화 문장 하나만 지웠고(측정상 무영향), 모델 경로의
  정확도가 달라질 이유는 없다 — 달라졌다면 그것이 발견이다.
- `model_calls`가 비용이다.
