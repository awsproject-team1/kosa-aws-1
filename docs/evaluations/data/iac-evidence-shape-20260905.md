# IAC 관점 `EXECUTION_ERROR` 8건의 원인 — 근거 표기 — 2026-09-05

ISMS-P 기준선 v2(`profile-isms-p-baseline@v2`)로 게시한 Profile `isms-p@v1`의 평가
`asm-4f2500fe`에서 `EXECUTION_ERROR`가 8건 나왔다. 이 문서는 그 원인을 **재생해서** 기록한다 —
runner는 예외 종류만 남기고 worker 로그에는 사유가 없었으므로, 같은 evaluator·prompt·IaC 문서
(commit `a3e6467`)·Rule·model profile(`assessment-nova-lite-m1-v3`, `amazon.nova-lite-v1:0`)로
같은 좌표를 `attempts=1`로 다시 불러 원문 응답을 읽었다.

허용 오차나 합격선을 정하지 않는다. 값과 그 해석만 남긴다.

## 1. 8건의 구성

| 구분 | 건수 | 내용 |
| --- | --- | --- |
| IAC `BedrockEvaluationError` | 4 | `ISMSP-RDS_ENCRYPTION_AT_REST`, `ISMSP-RDS_LOG_EXPORTS`, `ISMSP-ALB_ACCESS_LOGGING`, `ISMSP-S3_BUCKET_POLICY_RESTRICTED` |
| DRIFT 파생 | 4 | 위 4건의 IAC 관점이 없어 drift를 낼 수 없음 ("A perspective failed to execute") |

같은 실행의 다른 IAC 좌표 11개는 정상 판정됐다(FAIL 8, PASS 3). AWS_ACTUAL은 전부 코드 판정이라
영향이 없다. 이전 평가(`asm-4cb88485`, `asm-aa4f27f8`, `asm-c1fd8566`)에서 legacy Rule
`RDS-ENCRYPT-001`·`ALB-LOGGING-001`이 같은 모양으로 실패한 기록이 있다 — **기준선이 새로 만든
문제가 아니라 다시 드러낸 문제**다.

## 2. 재생 결과 (좌표 5개 × 2회)

| 좌표 | 1회 | 2회 |
| --- | --- | --- |
| RDS_ENCRYPTION_AT_REST | 거부 | 거부 |
| RDS_LOG_EXPORTS | 거부 | 거부 |
| ALB_ACCESS_LOGGING | 거부 | 거부 |
| S3_BUCKET_POLICY_RESTRICTED | **FAIL** (`terraform:main.tf#L1-L30`) | 거부 |
| S3_ENCRYPTION_AT_REST (대조군, 라이브에서 PASS) | 거부 | **PASS** (`terraform:main.tf#L31-L45`) |

거부 **7 / 10**, 전부 같은 사유: `evidence_references must be a non-empty string`. 모든 응답이
`stopReason=end_turn`이었다 — 잘림이 아니다.

## 3. 응답의 실제 모양

```json
{"status":"FAIL","score":0,
 "rationale":"The DB instance 'aws_db_instance.assessment' has storage encryption disabled ...",
 "evidence_references":[
   {"reference":"terraform:main.tf","evidence":"No access_logs block is present ..."}
 ]}
```

판정도 인용 locator도 옳다. `evidence_references`의 원소가 **문자열이 아니라 객체**이고, 게이트는
원소가 문자열이어야 한다는 이유로 응답 전체를 거부했다. 변형이 셋 있었다.

| 객체 모양 | 허용된 locator 포함? |
| --- | --- |
| `{"reference": "terraform:main.tf", "evidence": "..."}` | 예 |
| `{"locator": "main.tf", "evidence": "..."}` | 아니오 (`terraform:` 접두사 없음) |
| `{"file": "multiresource.tf", "line": 105, "content": "..."}` | 아니오 |
| `{"evidence": "...", "reference": "RDS.STORAGE_ENCRYPTED"}` | 아니오 (capability key를 locator로 씀) |

prompt는 "Every evidence reference must come from allowed_evidence_references"라고만 하고 원소가
문자열이라는 말이 없다. 모델은 허용 목록의 값을 **객체 안에** 넣는 것으로 그 지시를 지켰다고
본 것이다.

## 4. 처리

1. **표기 보정.** 원소가 객체이고 `reference`/`locator`/`evidence_reference` 중 하나에 문자열이
   있으면 그 문자열을 꺼내 **같은** 허용 목록 검사를 받게 한다. `_strip_json_fence`(코드 펜스)·
   `_unescaped`(`\uXXXX`)와 같은 성격이다. 접두사 없는 `main.tf`도 그 파일이 허용 목록에
   `terraform:main.tf`로 있을 때만 그것으로 읽는다. 허용 목록은 넓어지지 않는다 — 위 표의
   셋째·넷째 모양은 여전히 거부된다.
2. **사유를 남긴다.** `_judged`가 버리는 시도마다, runner가 최종 실패마다 WARNING을 쓴다.
   rationale에는 사유의 고정 문구(콜론 앞)를 싣는다. 콜론 뒤는 모델 문자열일 수 있으므로 싣지
   않는다.

   보정 뒤 같은 재생(5 좌표 × 2회): 수락 **6 / 10** (전: 3 / 10). 남은 거부 4건은 전부 정당하다 —
   capability key `RDS.STORAGE_ENCRYPTED`를 locator로 인용(2), locator 없는 객체(1), 산문
   `resource "aws_db_instance" ... in multiresource.tf`(1). Runtime은 좌표마다 두 번 묻는다.

3. **prompt는 그대로.** "array of strings"를 명시하면 빈도가 줄 것이다. 그러나 이 prompt는 측정으로
   정해졌고(`_SYSTEM_PROMPT` 주석, A/B n=8) 바꾸면 `prompt_version`과 회귀 측정이 따라와야 한다.
   별도 작업으로 남긴다.

## 재현

```bash
AWS_PROFILE=mfa python scratchpad/replay_iac.py 2   # 좌표 5개 × 2회, 원문 응답 출력
```

## 5. prompt로 고치려던 시도 — 측정으로 기각

남은 거부(capability key 인용, RDS 암호화 좌표에서 3/3)를 prompt에서 막으려고 v4 문장을
시험했다: "evidence_references must be an array of plain strings, each copied exactly from
allowed_evidence_references (...). Do not wrap a reference in an object, do not cite a file name
or line on its own, and do not cite required_evidence or optional_evidence keys". 판정 기준 문장은
그대로 두고 형식 지시만 더했다. 같은 harness(`measure_score_consistency.py --repetitions 3`)와
같은 좌표 재생으로 v3와 나란히 쟀다.

| | v3 (현행) | v4 (시험) |
| --- | --- | --- |
| harness 모델 판정 30회 중 계약 위반 | **0** | **5** |
| harness 기대 status 정확도 (모델) | 0.9 | 0.9 |
| 라이브 좌표 5개 × 3회 재생 수락 (표기 보정 포함) | **9 / 15** | **5 / 15** |

v4에서 모델은 "plain strings"를 **HCL 조각을 문자열로 붙이라**는 뜻으로 읽었다 —
`resource "aws_db_instance" "assessment" {... storage_encrypted = false... }` 같은 문자열이
근거로 왔고, 그것은 locator가 아니라 원문이다. 형식을 더 자세히 지시할수록 이 모델은 지시의
어휘를 출력에 되풀이한다(§3의 객체 인용도 같은 성격이다). 그래서 prompt는 v3 그대로 두고,
표기 차이는 응답 쪽 보정(§4-1)으로만 푼다. v4 원 측정값: `score-consistency-20260905-v4-raw.md`.

이 문서 기준으로 남는 것: RDS 암호화 좌표의 capability key 인용은 v3에서도 반복되며(3/3), 그
좌표는 두 번의 시도가 모두 그렇게 끝나면 `EXECUTION_ERROR`로 남는다 — rationale에 사유가 적힌다.
