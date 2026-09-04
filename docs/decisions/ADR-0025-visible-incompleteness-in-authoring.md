# ADR-0025: 추출하지 못한 단위는 보이는 미완료로 남는다

## Context

ADR-0023은 후보 추출에 전부-아니면-전무 게이트를 두었다: 문서 하나는 여러 chunk로 나뉘고,
chunk 하나가 모든 시도를 소진하면 실행 전체가 실패하며 부분 후보를 저장하지 않는다. 의도는
분명하다 — **정책 요구사항이 조용히 사라지면 안 된다.** 리뷰어가 "후보 12건"을 보고 승인했는데
사실은 문서의 절반만 훑은 것이었다면, 승인은 자기가 무엇을 승인했는지 모르는 승인이다.

그 규칙이 쓰인 시점의 문서는 20 unit(4 chunk)짜리 markdown 사내 정책이었다. 2026-09-05에
ISMS-P 점검표(`isms-p-2023-10-31.xlsx`, **334 unit → 67 chunk**)를 라이브 sandbox에 올리고
같은 prompt를 어댑터 내부로 재생해 원문 응답을 읽었다
(`docs/evaluations/data/authoring-isms-p-20260905.md`).

| 관측 | 값 |
| --- | --- |
| chunk 최종 실패율 (3회 시도 후) | **17–33%** (같은 입력, 실행마다 다름) |
| 응답 종료 사유 | 전부 `end_turn` — 잘림이 아니다 |
| 문서 완주 확률 `(1-0.17)^67` | **0.0004%** |

즉 이 문서는 게이트를 통과할 수 없다. 실패는 요구사항이 사라지는 것을 막지 못했고, **아무것도
저장되지 않게** 만들었다. 세 번의 실행이 모두 실패했고 리뷰어는 빈 화면을 봤다. 20 unit 문서가
지금까지 성공해 온 것은 chunk가 적어서다(`(1-0.17)^4 ≈ 47%`).

## Decision

**유실을 없애는 대신 보이게 만든다.** 완전성 요구를 무르게 하는 것이 아니라, 완전하지 않다는
사실을 값으로 나른다. Assessment가 실행 오류 좌표를 `EXECUTION_ERROR`로 남기는 것과 같은 성격이다
(ADR-0024 §3의 "모름"과도 같다 — 답할 수 없다는 것 자체가 답이다).

### 1. 실패한 chunk는 `UnclassifiedUnits`가 된다

`BedrockPolicyCandidateExtractor.extract`는 요구사항 tuple 대신 `ExtractionOutcome`을 돌려준다.
한 chunk가 모든 시도를 소진하면 그 chunk의 locator가 사유 코드와 함께 결과에 실린다.

| 사유 | 뜻 |
| --- | --- |
| `MODEL_RESPONSE_INVALID` | 응답 자체를 신뢰할 수 없었다(비-JSON, 금지 필드 외 형식 위반 등) |
| `INCOMPLETE_CLASSIFICATION` | 모든 unit을 분류하지 않았다(`ChunkAccountingError`) |

**게이트는 그대로다.** 거부된 응답에서 요구사항이 하나도 나오지 않는다는 점은 변하지 않았다 —
부분 응답을 반쯤 받아들이는 경로는 여전히 없다. 달라진 것은 그 거부의 **범위**뿐이다: 문서
전체가 아니라 그 chunk.

chunk는 1 unit씩 겹치므로, 실패한 chunk의 경계 unit이 이웃 chunk에서 분류됐다면 목록에서 뺀다.
빼지 않으면 미분류 수가 겹침만큼 부풀려져 리뷰어가 실제보다 나쁜 상태를 본다.

### 2. `PoisonedResponseError`는 예외로 남는다

모델이 `judgment`/`severity`/`score`/`source_score`/`anchor`를 내놓은 응답은 미분류로 적어 넘기지
않고 실행 전체를 세운다. 확률적 실수가 아니라 **모델이 경계를 넘으려 한 사실**이고, 그것을
"분류하지 못한 unit"으로 기록하면 그 사실이 사라진다(ADR-0023 §3).

### 3. 미분류는 저장되고, 세어지고, 화면에 나온다

- 저장: `POLICY_AUTHORING_UNCLASSIFIED` item 하나. key digest는 locator 집합에서 결정적으로
  유도하므로 at-least-once worker 재시도가 중복 item을 만들지 않는다.
- 집계: `AuthoringManifest.counts["unclassified"]`. READY manifest는 모든 count를 실어야 하므로
  이 값이 빠진 manifest는 계약이 거부한다.
- API: `GET …/candidates` 응답의 `unclassified[]`(locator + 사유 코드). 정책 원문은 없다(ADR-0004).
- 콘솔: 집계 타일 "미분류 단위"와 별도 표, 그리고 승인 전 경고 문구.

### 4. READY의 뜻이 좁아진다

READY는 이제 **"이 문서를 전부 훑었다"가 아니라 "훑은 만큼의 후보가 완전하다"**를 뜻한다. 이
차이를 리뷰어가 모르면 안 되므로, 미분류가 있는 실행은 화면이 그것을 먼저 말한다. 승인 자체를
코드가 막지는 않는다 — 부분 문서라도 승인할 가치가 있는지는 사람이 정할 일이고, 코드가 정할
일은 그 사람이 사실을 보고 정하게 하는 것이다.

## Consequences

- ADR-0023 §4의 "부분 후보를 저장하는 fail-soft 경로는 허용하지 않는다"는 이 ADR로 **좁혀진다**:
  허용하지 않는 것은 *미완료를 감춘 채* 부분 후보를 저장하는 것이다. 미완료가 값으로 실리고
  승인 화면에 나오면 그것은 fail-soft가 아니라 기록이다.
- `PolicyAuthoringResult`에 `unclassified`가 additive로 추가되고 `result_digest`에 들어간다. 옛
  실행은 빈 tuple로 복원되며 그 뜻은 예전과 같다 — 문서의 모든 unit이 분류됐다.
- `AUTHORING_RESULT_SEGMENTS`(손으로 유지하는 읽기 경로 목록)에 `UNCLASSIFIED`가 들어간다. 이
  목록이 뒤처지면 write-back 검증이 방금 쓴 item을 못 찾아 **모든 실행이 fail-closed한다** — 실제로
  구현 중에 그렇게 됐고, 그래서 "writer가 쓰는 segment ⊆ reader가 읽는 segment"를 파생 테스트로 고정했다.
- 이 결정은 chunk 실패율 자체를 낮추지 않는다. `authoring-isms-p-20260905.md` §4의 나머지 두
  선택지(chunk 축소, 문서 분할)는 여전히 열려 있고, 미분류 수가 그 효과의 측정 지표가 된다.
