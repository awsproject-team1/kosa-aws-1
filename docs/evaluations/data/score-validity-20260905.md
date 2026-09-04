# Score validity measurement — 2026-09-05

`prompt_version=assessment-three-perspective-rubric-v3`, model `amazon.nova-lite-v1:0`,
temperature 0. 측정 도구는 `scripts/measure_score_consistency.py`(24 Case × 3회, 총 72회)와
라이브 authored Rule 4건에 대한 직접 A/B다. 허용 오차나 합격선을 새로 정하지 않는다 — 값과
그 해석만 남긴다(ADR-0003).

## 1. 점수는 status의 재진술이다

| 측정 | 값 |
| --- | --- |
| 관측된 score 값 | **0.0과 100.0 뿐** (72회 중 그 외 0회) |
| Case별 score range | 전 Case 0.0 |
| status 자기일치율 | 전 Case 1.0 |
| 방향성(before→after 전이) | 9/9 정상 |

같은 입력은 항상 같은 답을 준다. 그러나 **연속 점수가 등급을 담지 않는다.** score는 PASS면 100,
FAIL이면 0이며, status 외의 정보를 전혀 나르지 않는다. 부분 준수 Case(전체 4건 중)에서도 중간값이
한 번도 나오지 않았다.

따라서 현재 score를 "얼마나 지켰는가"로 읽으면 안 된다. 의미가 있는 것은 status와, 그 status들을
severity로 가중 평균한 **Readiness Score**다. 후자는 모델이 아니라 코드가 계산하므로 실제로
연속값을 갖는다.

## 2. status 정확도 21/24 (88%), 오류는 모두 한 방향

틀린 3건은 전부 **위반을 PASS로 판정**한 false negative다.

| Case | 기대 | 판정 |
| --- | --- | --- |
| `ec2-public-ip-actual` | FAIL | PASS ×3 |
| `s3-three-of-four-actual` (4개 중 3개 차단) | FAIL | PASS ×3 |
| `alb-https-plus-http-actual` (HTTPS+HTTP 동시) | FAIL | PASS ×3 |

종류별로는 self-agreement 8/9, partial-compliance **2/4**, 전이 9/9, phrasing 불변성 2/2.
즉 정확도 손실은 **부분 준수 리소스에 집중**돼 있고, "모두"·"전용"을 요구하는 Rule에서 일부만
충족한 상태를 통과로 본다.

라이브 authored Rule에서는 반대 방향 오류도 관측됐다: AES256 기본 암호화가 적용된 버킷을
`S3_ENCRYPTION_AT_REST` FAIL로 판정(false positive). 모델에게는
`ApplyServerSideEncryptionByDefault: {SSEAlgorithm: AES256}`이 그대로 전달됐다.

## 3. 시도했다가 되돌린 개선: 근거 위치를 prompt에 넣기

**가설.** Catalog는 capability마다 근거의 위치를 이미 선언한다
(`EvidenceCapabilityBinding.document_paths`, AWS_ACTUAL에서 authoritative). 그런데 평가 prompt는
그 위치를 한 번도 전달하지 않아, 모델은 `S3.ENCRYPTION` 같은 불투명한 key와 문서 전체만 보고
어느 field가 대상인지 추측해야 했다. 실제로 그 추측을 틀린 사례가 있다 — ACL 규칙이 ownership
controls 대신 public-access-block 플래그를 근거로 들었다.

**측정.** 같은 Rule·문서·모델로 `required_evidence_locations` 유무만 바꿔 3회씩 평가했다.

| Control | 기대 | 없이 | 넣고 |
| --- | --- | --- | --- |
| S3_BLOCK_PUBLIC_ACCESS | FAIL | ERR, FAIL, FAIL | ERR ×3 |
| S3_ENCRYPTION_AT_REST | PASS | ERR, FAIL, FAIL | **PASS, PASS**, ERR |
| S3_BUCKET_ACL_DISABLED | PASS | PASS ×3 | PASS, PASS, ERR |
| S3_SERVER_ACCESS_LOGGING | FAIL | FAIL ×3 | ERR ×3 |

`ERR`은 `evidence reference is outside approved evidence` — 모델이 **경로를 근거 reference로
인용**해 근거 게이트가 응답 전체를 거부한 것이다. "이것은 읽을 위치이지 근거가 아니다"라고
prompt에 명시한 뒤에도 빈도가 오히려 늘었다.

**결론: 되돌렸다.** 암호화 오탐 하나는 실제로 고쳐지지만(FAIL→PASS), 그 대가로 게이트에 걸려
버려지는 평가가 훨씬 늘어 전체 정확도는 2/4 → 0/4로 나빠진다. 근거 게이트를 무르게 해서 통과
시키는 선택지는 취하지 않는다 — 그것은 모델이 지어낸 근거를 받아들이는 길이다.

## 4. 부수 관측: 근거 인용 실패가 baseline에도 있다

개선 없이도 `evidence reference is outside approved evidence`가 12회 중 2회 나타났다. 이 실패는
`EXECUTION_ERROR`가 되어 Coverage에서 빠지므로, 조용한 유실이 아니라 보이는 미완료로 남는다.
빈도를 별도로 추적할 가치가 있다.

## 남은 판단

- Anchor 도입(ADR-0003)은 여전히 사람 결정 사항이다. 이 측정은 "연속 점수가 등급을 담지 않는다"는
  근거를 제공하지만, Anchor로 바꾼다고 등급이 생기지는 않는다 — status는 그대로이므로 표시만
  달라진다.
- 부분 준수 false negative를 줄이는 것이 점수 입도보다 우선한다. Rule의 `evaluation_rubric`은
  이미 "any/only"를 명시하고 있으므로(예: "Fail when any block-public-access setting is
  disabled"), 다음 후보는 rubric을 더 강하게 쓰는 것이 아니라 **결정적 판정으로 옮기는 것**이다:
  `document_paths`가 이미 값의 위치를 알고 있으므로 "네 플래그가 모두 true인가" 같은 술어는
  모델 없이 코드가 답할 수 있다.
