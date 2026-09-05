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

### 5. 자동 판정 근거가 있는 항목은 자동 판정 Rule도 갖는다 (2026-09-05 보완)

MANUAL 101개만으로 평가하면 판정 좌표가 0개라 준비도가 `None`이다(ADR-0024 §2 — 판정 없는 좌표는
점수 없음). 그것은 설계대로이지 오류가 아니지만, Catalog가 이미 코드로 판정할 수 있는 사실에
대해서까지 사람을 기다릴 이유는 없다.

**Catalog의 자동 판정 통제(15개)마다 Rule 하나**(`ISMSP-<CONTROL_KEY>`)를 두고, 그 통제의 사실이
답하는 인증기준 항목들을 `source_references`로 인용한다. 매핑은 사람이 정한 표이며
`scripts/build_isms_p_baseline.py`의 `AUTOMATABLE_MAPPING`이 그 기록이다.

| 인증기준 항목 | 근거가 되는 통제 |
| --- | --- |
| 2.6.1 네트워크 접근 | SG ingress 제한, EC2 공인 IP 없음, RDS 비공개 |
| 2.6.2 정보시스템 접근 | S3 ACL 비활성, S3 bucket policy 제한, SG ingress 제한, RDS 접근 제한 |
| 2.6.4 데이터베이스 접근 | RDS 접근 제한, RDS 비공개 |
| 2.6.6 원격접근 통제 | SG ingress 제한 |
| 2.6.7 인터넷 접속 통제 | EC2 공인 IP 없음 |
| 2.7.1 암호정책 적용 | S3·EBS·RDS 저장 암호화, S3 TLS, ALB HTTPS |
| 2.9.4 로그 및 접속기록 관리 | S3·RDS·ALB 로깅 |
| 2.10.2 클라우드 보안 | S3 public access block, EC2 공인 IP 없음, RDS 비공개 |
| 2.10.3 공개서버 보안 | S3 public access block, ALB HTTPS |
| 2.10.4 전자거래 및 핀테크 보안 | ALB HTTPS, S3 TLS |
| 2.10.5 정보전송 보안 | ALB HTTPS, S3 TLS |

세 가지가 이 모양을 정했다.

- **통제 하나에 Rule 하나, 항목은 인용.** 항목마다 Rule을 복제하면 같은 사실(예: SG 0.0.0.0/0
  개방)이 인용 항목 수만큼 점수에 들어간다. `readiness.py`는 같은 사실을 두 번 세지 않는다.
- **자동 근거가 있는 항목도 MANUAL Rule을 그대로 갖는다.** 자동 판정은 그 항목 확인사항의
  일부에만 답한다(2.7.1은 정책 수립·키 관리도 묻는다). `controls.json`이 항목별로 MANUAL + 자동
  Rule을 함께 가리키므로 `ControlRuleCoverage`가 "이 항목은 몇 개 Rule로 얼마나 평가됐는가"를
  말한다. 화면은 "ISMS-P 준비도 N점"이 아니라 **"자동 판정 가능한 11개 항목 기준 N점 · 101개
  항목은 사람 검토 대기"**로 말해야 한다.
- **legacy Rule 16개를 복사하지 않고 실행 의미를 가진 Rule로 만든다.** legacy Rule은
  `control_for_rule`의 손 매핑에 묶인 legacy 경로를 타고, 이 Rule들은 authoring이 만든 Rule과 같은
  경로(Catalog 술어 → 코드 판정, ADR-0024)를 탄다. 실행 유형은 HYBRID(IAC + AWS_ACTUAL + DRIFT),
  severity·evidence·rubric은 전부 Catalog 값이다 — 여기서 새로 쓰면 코드 술어와 어긋난다.

이 상한은 Catalog의 상한이다. 영역 1(관리체계)과 영역 3(개인정보)은 자동 근거가 0개이고, IAM·KMS·
백업·CloudTrail·GuardDuty 통제가 Catalog에 들어오면 약 20–25개 항목까지 간다. 나머지는 사람 검토
기록 기능(별도 ADR)이 답이다.

**게시.** `profile-isms-p-baseline`은 `v2`(116 Rule)다. 게시된 `v1` 판본 item은 불변으로 남고,
bootstrap이 current pointer만 조건부로 옮긴다(`current_version = :current`, `record_profile`과
같은 규칙). 같은 판본에 다른 내용이면 여전히 fail-closed하고, 동시에 옮겨진 pointer는 덮어쓰지
않는다.

### 6. 자동 조치 허용 범위는 통제에서 물려받는다 (2026-09-05 보완)

기준선 v2로 평가하자 FAIL Finding 23건이 전부 `MANUAL_REVIEW (RULE_NOT_IN_SCOPE)`였다. 조치
판정이 legacy Registry의 `remediation.json`만 읽었고, 기준선 Rule은 어디에도 등록돼 있지 않았기
때문이다 — ADR-0017대로 등록 없는 Rule은 모든 자동 조치가 닫힌다(옳은 기본값이지만, 잊어서 닫힌
것과 판단해서 닫힌 것은 다르다).

- 허용 범위는 Rule이 아니라 **통제**에 대한 판단이다(Rule만으로 유일한 안전 상태가 정해지고
  교체·데이터 손실이 없는가). 같은 통제를 구현하는 기준선 Rule은 legacy Rule의 판단을 **그대로
  물려받는다** — 생성 스크립트가 `LEGACY_RULE_CONTROL_KEYS`로 legacy `remediation.json`을 통제별로
  읽어 `fixtures/baselines/isms-p-2023/remediation.json`을 만든다. 물려받을 판단이 없는 통제는
  생성이 실패한다(조용히 닫히지 않게).
- 결과: `AUTOMATIC` = S3 public access block · S3 ACL 비활성 · S3 TLS · RDS 비공개(4). 나머지 11개는
  `MANUAL_ONLY` — bucket policy의 의도한 범위, 로그 목적지, 암호화 키, SG의 허용 출처, 공인 IP
  제거 후의 접근 경로는 Rule이 정하지 않고, EBS·RDS 저장 암호화는 교체가 필요하다.
- MANUAL Rule 101개는 허용 범위를 갖지 않는다. FAIL Finding을 만들지 않으므로(항상 `MANUAL_REVIEW`)
  판단할 조치가 없다.
- API의 조치 판정은 두 Registry의 범위를 합쳐 본다(`load_remediation_policy`). Rule id가 겹치면
  거부한다 — 어느 판단이 이기는지 말할 수 없다.

### 7. "사람이 판정할 일"과 "아직 지원하지 않는 일"을 가른다 (2026-09-05 보완)

MANUAL 101개 중 15개는 조직 통제가 아니라 **기술 통제**다 — 2.5.1~2.5.6 계정·인증·권한(IAM),
2.7.2 암호키(KMS), 2.9.3 백업, 2.9.5 로그 점검·2.11.3 이상행위(CloudTrail/GuardDuty), 2.10.1
보안시스템, 2.10.8 패치·2.10.9 악성코드·2.11.2 취약점(SSM/Inspector), 2.12.1 재해 대비. 사람에게 가는
이유가 "판단이 필요해서"가 아니라 "Catalog가 아직 근거를 못 읽어서"이고, 그 둘은 다른 답을 부른다
(전자는 검토, 후자는 Catalog 확장).

- Catalog에 두 번째 MANUAL 통제 `TECHNICAL_CONTROL_NOT_YET_SUPPORTED`를 둔다. runtime 경로는 같다
  (governance 좌표, `MANUAL_REVIEW`, 준비도 제외). `ManualReviewEvaluator`는 이 통제의 rationale을
  고정 접두사 `Not yet supported:`로 시작하고, 콘솔은 그것으로 "지원 예정"을 갈라 센다.
- 항목 목록(`NOT_YET_SUPPORTED_ITEMS`)은 생성 스크립트에 있고 값은 필요한 AWS 근거 계열이다 —
  매핑이 아니라 "왜 아직 안 되는가"의 기록. 자동 근거가 있는 항목과 겹치면 생성이 실패한다.
- Rule item은 불변 key에 게시되므로 내용이 바뀐 이 개정은 새 version이다: `2023-10-31.r2`
  (원문 판본 + Registry 개정). Profile은 `v3`. `v1`·`v2` 판본과 r1 Rule item은 그대로 남는다.

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
