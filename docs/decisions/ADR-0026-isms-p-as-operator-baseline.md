# ADR-0026: ISMS-P는 고객이 올리지 않는다 — 운영자 기준선으로 게시한다

## Context

이 서비스의 목적은 사용자가 ISMS-P 인증기준과 사내 정책에 대해 현재 상태의 개선점을 찾도록
돕는 것이다. 처음 설계는 그 둘을 다르게 다뤘다. 사내 정책은 고객이 올리고 모델이 후보를 뽑아
리뷰어가 승인하며(ADR-0023), ISMS-P는 **모든 고객에게 같은 인증기준**이므로 저장소에 커밋된
Registry로 두고 운영자 bootstrap이 고객 파티션에 게시한다 — 그래서 `PolicySourceKind.ISMS_P`,
`fixtures/rules/sources.json`의 `isms-p-2023`, `control/x.y.z` locator, 게시 요청의 `baseline`,
Profile의 `ISMS_P` Segment, 준비도의 원본별 분리가 이미 있다(`docs/POLICY_INGESTION.md`).

그런데 그 Registry의 내용은 ISMS-P가 아니었다. 16개 legacy Rule이 사내 체크리스트와 ISMS-P
조항 5개를 함께 인용할 뿐, 인증기준 101개 항목 자체는 어디에도 없었다. 그래서 2026-09-05에
ISMS-P 점검표(xlsx, 334 unit)를 **고객 문서로 업로드**해 모델 추출에 태웠고, 그 결과가
ADR-0025다: chunk 실패율 19–25%, 150개 상한 초과, 세 번의 실행 모두 저장 실패. 원인을 고쳐도
남는 사실이 있다 — 인증기준은 고객마다 다시 추출할 이유가 없고, 확률적 모델 경로에 태울 이유는
더욱 없다. 같은 문서를 고객 수만큼 Bedrock에 보내 매번 다른 후보 집합을 받는 것은 비용이자
비일관성이다.

## Decision

**ISMS-P 인증기준은 운영자 기준선 Registry로 한 번 등록하고, bootstrap이 고객 파티션에
게시한다.** 고객은 ISMS-P를 업로드하지 않는다. Profile 게시 때 `baseline`으로 고른다.

### 1. 별도 Registry 디렉터리

`fixtures/baselines/isms-p-2023/`는 `load_rule_registry`가 읽는 같은 네 파일 모양이다.

| 파일 | 내용 |
| --- | --- |
| `sources.json` | legacy Registry의 `isms-p-2023@2023-10-31` 항목을 **바이트 그대로** 복사 |
| `controls.json` | 인증기준 항목마다 Control 하나, `ISMS-P-x.y.z` (101개) |
| `rules.isms-p.json` | 항목마다 MANUAL Rule 하나, `ISMSP-x.y.z@2023-10-31` (101개) |
| `profiles.json` | `profile-isms-p-baseline@v1`: 101개 Rule 전부, `ISMS_P` Segment 하나 |

legacy Registry(`fixtures/rules/`)에 섞지 않는다. 그 디렉터리는 "세 관점으로 평가되는 legacy
Rule만 담는다"는 계약을 갖고 있고(`fixtures/README.md`), 그것을 고정한 테스트가 있다
(`len(registry.rules) == 16`, drift 계획이 모든 Rule을 덮는다). Source 항목을 복사하는 이유는
bootstrap의 불변 key 검사다 — 같은 `POLICY_SOURCE` item이 두 Registry에서 다른 바이트로 나오면
두 번째 게시가 "different immutable content"로 fail-closed한다.

### 2. 항목은 전부 MANUAL Rule이다

인증기준 항목은 "경영진의 참여", "정보시스템 접근" 같은 조직 통제이며, 그 판정은 심사원이
증적을 보고 내린다. 그래서 각 Rule은 `evaluation_type=MANUAL`,
`control_key=ORGANIZATIONAL_CONTROL_MANUAL_REVIEW`, `resource_types=[AWS::Governance::Assessment]`
이고, 평가는 `ManualReviewEvaluator`가 도구 없이 `MANUAL_REVIEW` 좌표를 남긴다(ADR-0023 §7).
준비도 평균에서는 빠지고 `undetermined_evaluations`로 세어진다(ADR-0024 §2). 즉 **ISMS-P
기준선은 점수를 만들지 않는다 — 검토해야 할 101개 좌표를 만든다.** 그것이 인증 준비의 실제
모양이고, 자동 판정할 수 있는 부분(S3·EC2·RDS·ALB의 기술 통제)은 이미 legacy Rule이 같은
ISMS-P 조항을 인용하며 맡고 있다.

severity는 Catalog의 MANUAL 통제 기본값(MEDIUM) 하나다. 항목 사이의 등급 차이는 이 Registry가
정할 일이 아니라 심사 맥락에서 정할 일이다.

### 3. 생산 경로는 스크립트 하나다

`scripts/build_isms_p_baseline.py`가 로컬 원문(`policies-local/isms-p-2023-10-31.xlsx`)에서 네
파일을 결정적으로 만든다. 발췌 digest는 `scripts/policy_source_digest.py`와 같은 규칙
(`control/x.y.z` → 항목 번호·항목명·상세내용)이다. `--check`는 커밋본과 새 생성본을 대조하고,
원문이 없는 환경(CI)은 건너뛴다(ADR-0004). 파일에는 항목 번호·항목명·분야명과 digest만 들어간다
— 상세내용·확인사항 문장은 싣지 않는다.

손으로 옮겨 적지 않는다. 101개를 손으로 적으면 어느 항목이 빠졌는지 아무도 모른다.

### 4. 게시와 선택

`scripts/publish_policy_catalog.py --registry isms-p-2023`가 고객 파티션에 103개 item을 조건부
write로 게시한다(멱등). 게시된 `profile-isms-p-baseline@v1`은 `GET /policy-profiles`에 나타나고,
리뷰어가 Profile 게시 때 `baseline`으로 고른다 — 사람의 선택은 그대로 남는다. 그 Rule에는
고객 승인 record가 없다: 고객이 올린 문서가 아니라 코드 리뷰를 거쳐 커밋된 Registry이기
때문이다(`docs/POLICY_INGESTION.md`, `ProfileBaseline`).

## Consequences

- ISMS-P 평가는 Bedrock을 부르지 않는다. 고객 수와 무관하게 같은 101개 좌표가 만들어진다.
- 고객이 ISMS-P 점검표를 사내 문서로 올리는 경로는 막지 않는다 — 막을 이유가 없다(사내에서
  변형한 점검표일 수 있다). 다만 콘솔은 기준선이 이미 있음을 말한다.
- 기준선 Rule의 `control_catalog_version`은 생성 시점의 Catalog(`2026-09-05`)에 고정된다.
  Catalog 판이 바뀌어도 이 Rule은 바뀌지 않는다 — 인증기준이 바뀐 것이 아니기 때문이다. 항목이
  개정되면(예: 2023-10-31 → 다음 고시) 새 `source_version`으로 새 Registry를 만든다.
- 로컬 원문 파일의 전체 digest는 커밋된 `ab99…`와 다르다(재저장된 파일). 발췌 digest 106개는
  전부 일치하므로 Rule이 가리키는 내용은 같다. 이 불일치는 이 ADR 이전부터 있었고, 원문 보유자가
  `sources.json`의 digest를 실제 파일로 갱신하는 것은 별도 결정이다 — 라이브 `POLICY_SOURCE`
  item이 `ab99…`이므로 지금 바꾸면 bootstrap이 fail-closed한다.
- `fixtures/README.md`의 "이 Registry의 Rule은 전부 legacy Rule"은 `fixtures/rules/`에만 해당한다.
  `fixtures/baselines/`는 실행 의미를 가진 MANUAL Rule을 담는다.
