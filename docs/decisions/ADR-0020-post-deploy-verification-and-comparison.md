# ADR-0020: Post-Deploy Verification과 before/after 비교 경계

> **상태: Accepted (2026-09-02)** — M3 C 구현은 이 결정을 따른다. C의 비교 projection과
> Contract는 구현됐고, 5번의 선행 작업(PLAN item의 planned 집합 저장과 집합을 받는
> `calculate_readiness_score`)도 반영됐다. 검증 Assessment의 `phase`/`source_assessment_id`/
> `deployment_id` 영속화와 조회 API 배선, D의 apply 후 Actual 재조회 입력은 A/D 통합 작업으로
> 남는다.
>
> **결정 대상:** 재평가 결과를 어디에 저장하는지, 무엇을 다시 평가하는지, 어떤 Model Profile로
> 평가하는지, "Finding이 해소됐다"를 어떤 값으로 표현하는지, 점수·Coverage 변화를 언제 비교
> 가능하다고 보는지, 억제된 Finding을 어떻게 표시하는지, 언제 재평가를 시작하는지.
>
> **관련:** ADR-0002, ADR-0003, ADR-0011, ADR-0013, ADR-0016, ADR-0017, ADR-0019

## Context

M3 C의 Exit criteria는 "변경된 AWS Actual을 Post-Deploy Verification으로 재평가해 Finding 및
Readiness Score 변화를 확인한다"다. 현재 코드·문서 상태에서 이 문장은 다음 공백을 남긴다.

1. **같은 Assessment에 재평가 결과를 넣을 수 없다.** result SK는
   `ASSESSMENT#{assessment_id}#RESULT#{resource_id}#RULE#{rule_id}#PERSPECTIVE#{perspective}`로
   phase를 포함하지 않고(`docs/DATABASE.md`), immutable write는 조건부이므로 같은 좌표의 재평가는
   충돌한다. Assessment record에 `phase`도 영속화되지 않으며
   `apps/backend/assessment/runtime.py`는 `AssessmentPhase.INITIAL`을 하드코딩한다.
2. **재평가 범위가 정의되지 않았다.** 이번 apply가 건드린 리소스만 볼 것인지, 원 Finding의
   `Resource × Rule`만 볼 것인지, 전체를 다시 볼 것인지에 따라 Coverage 분모와 Readiness Score의
   의미가 달라진다.
3. **재평가에 쓸 Model Profile이 정해지지 않았다.** 최신 Profile로 재평가하면 변화가 인프라
   개선인지 모델·rubric 차이인지 구분할 수 없다.
4. **Finding Resolution의 값 어휘가 없다.** `PASS` 외의 전이(`MANUAL_REVIEW`,
   `INSUFFICIENT_EVIDENCE`, `OUT_OF_SCOPE`, `EXECUTION_ERROR`, `rule_version` 변경)를 각 역할이
   임의로 해석하게 된다.
5. **점수 비교 가능성 판단이 없다.** `readiness_score`는 미완료·`EXECUTION_ERROR`에서 `null`이고
   (`docs/CONTRACTS.md`), 분모가 다른 두 Assessment의 delta는 오해를 부른다.
6. **억제(`SUPPRESSED`) Finding의 표시 규칙이 없다.** 예외는 조치 게이트인데 평가 결과에
   저장하면 만료 이후 과거 사실이 왜곡된다.
7. **Job 경계가 모호하다.** `apps/backend/jobs/lifecycle.py`의 `_link_once`가 `assessment_id`를
   write-once로 강제하므로 하나의 Job이 원 Assessment와 검증 Assessment를 동시에 가리킬 수 없다.
8. **검증 시작 시점 규칙이 없다.** apply 직후 재조회하면 AWS 전파 지연을 정책 위반으로 오판한다.
9. **Deployment 역할 Model Profile의 사용처가 없다.** Deployment Readiness는 이미 결정적 Code
   (`apps/backend/remediation/readiness.py`)인데 벤치마크는 Deployment 역할 후보 모델을 냈다.

## Decision

### 1. 검증은 새 Assessment이며 result SK는 바꾸지 않는다

- Post-Deploy Verification은 **새 `assessment_id`**로 생성한다. Assessment item에
  `phase`(`POST_DEPLOY_VERIFICATION`), `source_assessment_id`, `deployment_id`를 영속화한다.
- result/finding SK 구조는 그대로 둔다. 새 Assessment 아래에 쓰이므로 좌표가 충돌하지 않고,
  before/after 양쪽이 immutable로 보존된다.
- `AssessmentPhase`는 runtime 인자로 전달하고 Assessment record에서 복원한다. `INITIAL`
  하드코딩을 제거한다.
- 비교 결과는 두 immutable 결과 집합에서 **읽을 때 계산하는 projection**이다. 별도 판정 결과를
  새로 저장하지 않는다 (M1 Readiness Score와 같은 원칙).

### 2. 재평가 범위는 원 Assessment와 동일한 평가 계획을 기본으로 한다

- 기본값: 같은 Repository, 같은 Policy Profile **version**, 같은 적용 가능
  `Resource × Rule × Perspective` 집합을 새 commit에서 전체 재평가한다. 새 plan을 이 집합으로
  저장하므로 Coverage 정의(`docs/CONTRACTS.md`)가 그대로 성립하고 Readiness Score가 비교 가능해진다.
- 현재 MVP 규모는 S3 Rule 6건 × 3관점 = 18개 평가이므로 전체 재평가 비용이 축소 재평가의 복잡성보다
  작다.
- 축소 재평가(이번 plan이 건드린 리소스 한정)는 옵션으로 허용하되, 그 결과의 비교는
  `comparable = false`와 이유 코드를 반환한다 (아래 5번).
- Policy Profile version이 그 사이 교체됐다면 검증이 아니라 **새 Initial Assessment**로 처리한다.
  다른 allow-list로 평가한 결과를 같은 축에서 비교하지 않는다.

### 3. Model Profile과 rubric은 원 Assessment와 동일해야 한다

- 검증 Assessment는 원 Assessment의 `model_profile_id`와 `rubric_version`을 그대로 재사용한다.
- 다르면 delta를 만들지 않고 `comparable = false`와 이유 코드를 반환한다. 이유 코드는
  `ComparisonIneligibilityReason.MODEL_PROFILE_MISMATCH`와 `RUBRIC_VERSION_MISMATCH`로 **분리**
  한다. 두 값이 각각 다를 수 있고, 어느 쪽이 어긋났는지가 후속 조치를 가르기 때문이다.
  M1 `DRIFT` 파생이 이미 같은 규칙을 쓴다 — 서로 다른 Profile/rubric에서 나온 두 판정은 비교
  대상이 아니다.
- Profile이 그 사이 교체됐다면 비교가 아니라 **새 Initial Assessment로 처리한다.**
- 이 제약 때문에 Model Profile 교체는 검증 대기 중인 Deployment가 없을 때만 배포한다.
- **선행 작업:** `POST_DEPLOY_VERIFICATION` phase의 Golden Case가 현재 0건이다
  (`fixtures/m1/golden_dataset_cases.json`은 18건 전부 `INITIAL`). 이 phase의 품질 Gate를 돌리려면
  Case 추가가 선행되고, 그 Case는 원 Assessment와 같은 `rubric_version`을 써야 한다.

### 4. Finding Resolution은 Code의 결정적 diff다

- 매칭 키는 `(resource_id, rule_id, perspective)` **세 값**이고, `rule_version`은 키가 아니라
  대응된 두 결과 사이의 **동등성 검사** 대상이다. `rule_version`을 키에 넣으면 version이 바뀐
  좌표가 before/after 각각 짝 없는 항목으로 갈라져 "version이 달라 비교 불가"라는 판정 자체를
  내릴 수 없다. 키에서 빼고 값으로 비교해야 `INDETERMINATE`가 표현된다.
- 이 세 값은 planned 집합의 좌표(5번)와 같은 구성이다. 매칭 키와 비교 가능성 판정이 서로 다른
  좌표를 쓰면 두 판정이 어긋난다.
- 값 어휘는 다음 다섯 개다.

| 값 | 조건 |
| --- | --- |
| `RESOLVED` | 원 Finding 좌표의 새 결과가 `PASS` |
| `UNRESOLVED` | 새 결과가 여전히 `FAIL` |
| `REGRESSED` | 원 결과가 `PASS`였는데 새 결과가 `FAIL` (원 Finding이 없던 좌표의 신규 위반) |
| `INDETERMINATE` | 새 결과가 `MANUAL_REVIEW`, `INSUFFICIENT_EVIDENCE`, `EXECUTION_ERROR`이거나 `rule_version`이 달라 비교 불가 |
| `NO_LONGER_APPLICABLE` | 새 결과가 `OUT_OF_SCOPE` (리소스 소멸 포함) |

- 이 판정은 AI가 하지 않는다. `DRIFT` 파생과 같은 근거로, 두 immutable 결과의 기계적 비교다.
  모델에게 before/after를 요약하게 하면 판정 정본이 둘로 갈린다.
- `rule_version`이 달라진 경우 `INDETERMINATE`로 두고 이유 코드를 남긴다. 다른 version의 Rule은
  다른 질문이므로 해소로 읽지 않는다.
- **`EvaluationStatus`에 값을 새로 추가하지 않는다.** 리소스 소멸은 `OUT_OF_SCOPE` 결과로 기록한다.
  값을 늘리면 모든 소비자와 golden fixture가 함께 바뀐다. 소멸과 "Rule 비적용"의 구분은 결과의
  `rationale`·`evidence_references`에 남기고, `FindingResolution`은 두 경우 모두
  `NO_LONGER_APPLICABLE`로 같게 판정한다 — 후속 조치가 같기 때문이다.
- 리소스가 사라졌으면 **세 관점 모두에** `OUT_OF_SCOPE` 결과를 쓴다. planned 집합은 원 Assessment
  에서 고정돼 그 리소스를 여전히 포함하므로, 한 관점만 비우면 집합이 어긋나 `comparable = false`가
  된다. `OUT_OF_SCOPE`는 completed 집합에는 들어가고 점수 계산에서만 빠지므로
  (`apps/backend/assessment/readiness.py`는 `EXECUTION_ERROR`만 completed에서 제외한다)
  Coverage와 score가 모두 성립한다.
- 비교 입력은 `Finding`이 아니라 **`EvaluationResult` 집합**이다. `Finding`은 status를
  `FAIL`/`MANUAL_REVIEW`/`INSUFFICIENT_EVIDENCE`로 제한하므로 `PASS`를 표현하지 못하고,
  `REGRESSED`(before `PASS` → after `FAIL`)를 Finding만으로는 계산할 수 없다.

### 5. 점수·Coverage 변화는 비교 가능할 때만 표시한다

- delta는 다음 조건을 모두 만족할 때만 계산한다.
  1. 두 `readiness_score`가 모두 non-null (= 두 평가 계획이 완전히 Coverage됨)
  2. 두 Assessment의 planned `(resource_id, rule_id, perspective)` 집합이 동일
  3. `model_profile_id`와 `rubric_version`이 동일 (3번)
- 하나라도 어긋나면 `comparable = false`와 이유 코드를 반환하고 delta를 만들지 않는다. Frontend는
  `comparable = false`에서 숫자 변화를 표시하지 않고 이유를 보여준다. 이유 코드는
  `SOURCE_READINESS_UNAVAILABLE`, `VERIFICATION_READINESS_UNAVAILABLE`,
  `PLANNED_EVALUATIONS_MISMATCH`, `MODEL_PROFILE_MISMATCH`, `RUBRIC_VERSION_MISMATCH` 다섯 개이며,
  어긋난 것이 여럿이면 모두 반환한다. 순서는 결정적이다.
- **비교 입력은 완전한 스냅샷이어야 한다.** `AssessmentReport`는 페이지 단위로 조회될 수 있고
  (`next_cursor`/`findings_next_cursor`), 첫 페이지만 넘기면 누락된 좌표가 조용히
  `INDETERMINATE`가 되고 delta도 부분 집합 기준이 되어 **예외 없이 잘못된 리포트**가 나온다.
  비교 경계는 cursor가 남은 report를 fail-closed로 거부한다.

**선행 작업 — 이것 없이는 2번 조건을 판정할 수 없다.** *(2026-09-02 반영 완료)*

- planned 집합을 **이미 존재하는 `ASSESSMENT#{assessment_id}#PLAN` item에 속성으로 추가해
  저장한다.** 그 item은 지금 planned 적용 가능 `Resource × Rule × Perspective`의 **개수**만 담는다
  (`docs/DATABASE.md` Item layout). Assessment 시작 시 이미 쓰는 항목이므로 write가 늘지 않고
  속성만 늘어난다.
- 조회 시 재구성은 채택하지 않는다. 비용 때문이 아니라 **원리적으로 불가능**하기 때문이다.
  planned 집합은 (리소스 목록 × Rule × 관점)에서 나오는데 리소스 목록은 시간이 지나면 달라진다.
  결과에서 거꾸로 세는 것도 안 된다 — 결과는 완료된 것만 알려주고 계획됐다가 누락된 항목은 보이지
  않는다. `apps/backend/assessment/readiness.py`가 내부에서 만드는 집합은 planned가 아니라
  **completed** 집합이며, planned는 `planned_evaluations: int` 개수로만 들어온다.
- 같은 작업에서 `calculate_readiness_score`의 인자를 개수에서 집합으로 바꾸고
  `len(completed) != planned_evaluations` 비교를 `completed != planned` 집합 비교로 바꾼다. 개수
  비교는 계획에 없던 평가가 누락된 평가를 대신 채운 경우를 통과시킨다.
- `AssessmentCoverage`의 개수 필드는 그대로 두고, 집합은 비교 가능성 판정에만 쓴다.
- planned 집합이 DynamoDB item 한도에 닿을 규모가 되면 S3 artifact로 옮기고 PLAN item에는 digest만
  남긴다. 현재 18건이므로 M3·M4에서는 해당하지 않는다.
- `DRIFT` 관점은 Readiness Score에서 여전히 제외한다(ADR-0016). 다만 Drift 해소 여부는 Finding
  Resolution으로 별도 표시한다. 데모에서 "drift가 사라졌다"는 점수가 아니라 이 값으로 말한다.

### 6. 예외는 평가 게이트가 아니다

- 고객 예외(`RemediationException`)는 재평가를 막지 않는다. 위반이면 Finding은 그대로 생성된다.
- 억제 표시는 **조회 시점에 예외를 join해 표시만** 하고 결과나 Finding에 저장하지 않는다. 예외는
  만료되므로 저장하면 만료 이후 과거 사실이 왜곡된다.
- 조치 억제 판정은 계속 `RemediationPolicy.decide()`의 두 시각 규칙(ADR-0017)만 사용한다.

### 7. Deployment 1건 = Job 1건, `assessment_id`는 검증 Assessment를 가리킨다

- Deployment Job은 `PLAN → WAITING_APPROVAL → APPLY → POST_DEPLOY_VERIFICATION → COMPLETED`를
  하나의 Job revision 사슬로 진행한다. 외부 완료 Event마다 revision이 오른다(ADR-0019 §7).
- Job의 write-once `assessment_id`는 **검증 Assessment**에 사용한다. 원 Assessment는 Deployment
  record의 `source_assessment_id`가 참조한다.
- `JobResponse`에 필드를 추가하지 않는다. 폴링 projection은 최소로 유지하고 비교 결과는
  `GET /deployments/{deploymentId}/verification`으로 노출한다.

### 8. 검증 시작 시점과 재시도

- **재조회는 immutable write 앞에서 끝낸다.** 결과를 쓴 뒤 재시도하면 같은 result SK에 두 번째
  조건부 write가 들어가 충돌한다. 최종 읽기값 하나만 결과로 쓴다. 이 순서가 1번의 "result SK를
  바꾸지 않는다"와 양립하는 유일한 방법이다.
- **1회차는 지연 없이 읽는다.** 고정 30초를 모든 배포에 무조건 붙이면 드물게 일어나는 전파 지연
  때문에 매 배포가 30초 느려진다. apply가 고친 항목이 여전히 위반으로 보일 때만 **15초 → 45초**
  간격으로 재조회한다. 총 3회는 ADR-0013의 "총 세 번" 규칙을 재사용한다.
- 재조회 대상은 불일치한 리소스로 좁힌다. 전체 재평가(2번)는 유지되지만 재시도가 전체를 다시
  읽지는 않는다. Bedrock 호출은 최종 읽기값 1회에만 발생한다.
- 3회 후에도 다르면 `VERIFICATION_FAILED`가 아니라 `VERIFICATION_INDETERMINATE`로 두고 사람에게
  보낸다. 전파 지연과 실제 미반영을 자동으로 구분할 수 없기 때문이다.
- 지연·횟수는 이 ADR이 정하는 값이며 개별 구현이 바꾸지 않는다. 변경은 이 ADR 개정으로 한다.
  다만 이 값들은 **M1 범위가 S3 단독**이라는 전제에서 정했다. sandbox E2E 1회로 실제 전파 시간을
  관측해 재확인하고, EC2/RDS/ALB로 확장할 때 다시 본다.

### 9. Deployment 단계에는 LLM을 두지 않는다

- Deployment Readiness는 결정적 Code이며 모델을 호출하지 않는다. Post-Deploy Verification의 평가는
  Assessment Profile을 재사용한다(3번).
- 따라서 MVP에서 Deployment 역할 Model Profile을 배정하지 않는다.
  `docs/evaluations/BEDROCK_MODEL_SELECTION.md`의 Deployment 후보는 근거 기록으로만 남긴다.
- 결정적 판정 단계를 임의로 LLM화하지 않는다. ADR-0018이 제거한 "판정 정본이 둘"인 구조가 다시
  생긴다.

## 고정돼야 하는 불변식

1. 검증 Assessment는 원 Assessment와 다른 `assessment_id`를 갖고, 원 결과를 덮어쓰지 않는다.
2. `model_profile_id`·`rubric_version`이 다른 두 Assessment는 비교되지 않는다.
3. Finding Resolution은 두 immutable 결과에서 계산되며 AI 호출이 없다.
4. planned 집합이 다르면 delta가 계산되지 않고 `comparable = false`가 반환된다.
5. 페이지가 남은 부분 report는 비교 입력으로 받아들여지지 않는다.
6. 예외는 재평가 결과에 저장되지 않는다.
7. 재조회 3회 후에도 불일치면 자동 실패가 아니라 사람 판단으로 간다.

## Consequences

- before/after 양쪽 결과가 각각 immutable Assessment로 남아 감사와 데모 재현이 가능하다.
- result SK를 바꾸지 않으므로 M1 저장·조회·Coverage 코드가 그대로 재사용된다.
- 전체 재평가가 기본이므로 비교 규칙이 단순해지는 대신 Bedrock 호출 수가 Deployment마다 평가 계획
  전체만큼 발생한다. 현재 규모(18개)에서는 수용 가능하며, Rule 확장 시 축소 재평가와
  `comparable=false` 표기를 재검토한다.
- Model Profile 동일성 강제 때문에 Profile 교체 시점이 Deployment 수명과 결합된다.
- Finding Resolution이 Code 판정이므로 Golden Dataset 확장 없이도 결과가 결정적이다. 다만
  재평가 자체의 품질 Gate는 `POST_DEPLOY_VERIFICATION` Golden Case가 0건이라 아직 돌릴 수 없다.
- planned 집합이 추가되지만 새 item이 아니다. 이미 쓰는 `ASSESSMENT#{assessment_id}#PLAN`에
  `planned_coordinates` 속성이 하나 늘고, 같은 작업에서 `calculate_readiness_score`가 개수 대신
  집합을 받는다. 집합이 계획의 정본이고 Coverage 분모는 거기서 파생되므로 개수와 집합이 어긋날 수
  없다. 집합이 없는 옛 plan은 재구성하지 않고 readiness를 `null`로 두며
  `get_planned_evaluations()`는 fail-closed로 거부한다 — 결과에서 계획을 되짚을 수 없기 때문이다.
- `runtime.py`의 `INITIAL` 하드코딩 제거는 M1 경로에 영향을 준다. 기존 호출부가 명시적으로
  `INITIAL`을 넘기도록 바꾸고 테스트로 고정한다.

## Rejected alternatives

- **같은 Assessment에 phase를 추가해 재평가 결과를 append:** result SK에 phase가 없어 좌표가
  충돌하고, SK에 phase를 넣으면 기존 M1 저장·조회·Coverage 경로를 모두 바꿔야 하므로 거부한다.
- **이번 plan이 건드린 리소스만 재평가를 기본값으로:** Coverage 분모와 Readiness Score가 원
  Assessment와 달라져 M3 Exit criteria의 "점수 변화 확인"이 성립하지 않으므로 기본값에서 거부한다.
- **최신 Model Profile로 재평가:** 변화의 원인을 인프라와 모델 중 무엇으로도 귀속할 수 없으므로
  거부한다.
- **AI에게 before/after 비교·요약을 판정하게 하기:** 결정적으로 계산 가능한 값을 확률적 판정으로
  바꾸고 판정 정본을 둘로 만들므로 거부한다.
- **억제된 Finding을 결과에 `SUPPRESSED`로 저장:** 예외 만료 후 과거 결과가 사실과 달라지므로
  거부한다.
- **불일치를 즉시 `VERIFICATION_FAILED`로 확정:** AWS 전파 지연을 위반으로 오판하므로 거부한다.
- **모든 배포에 30초 고정 지연 후 첫 재조회:** 드물게 일어나는 전파 지연 때문에 매 배포를 30초
  느리게 만드므로 거부한다. 1회차는 즉시 읽고 불일치일 때만 15초·45초로 물러난다.
- **결과를 쓴 뒤 재조회·재시도:** 같은 result SK에 두 번째 조건부 write가 들어가 immutable 규칙과
  충돌하므로 거부한다. 재조회는 write 앞에서 끝낸다.
- **`rule_version`을 매칭 키에 포함:** version이 바뀐 좌표가 before/after 각각 짝 없는 항목으로
  갈라져 `INDETERMINATE` 판정 자체를 표현할 수 없으므로 거부한다.
- **리소스 소멸에 `EvaluationStatus` 값을 새로 추가:** 모든 소비자와 golden fixture가 함께 바뀌고,
  `OUT_OF_SCOPE`로 이미 표현 가능하므로 거부한다.
- **planned 집합을 조회 시 재구성:** 리소스 목록이 시간에 따라 달라지고 결과에서는 누락된 평가가
  보이지 않아 원리적으로 불가능하므로 거부한다.
- **계획 개수만 비교해 `comparable`을 판정:** 개수가 같아도 집합은 다를 수 있으므로 거부한다.

## Open decision

- **Owner:** C(재평가 Agent·비교 projection) + A(검증 Assessment 저장·조회 API) + B(재평가 적용
  범위와 예외 표시 규칙)
- **Needed by:** M3 C/A 착수 전. 특히 1번(새 Assessment)과 3번(Profile 동일성)은 저장 구조와
  비교 의미를 동시에 결정하므로 구현 전에 필요하다.
- **Blocks:** M3 A(결과 조회 API, Assessment record 필드 추가), M3 B(재평가 적용 범위 검증),
  M3 C(Before/After 비교, Finding Resolution), M4 C(품질 목표 확인), 데모의 점수 변화 화면.
- **Proposed options:** 위 Decision 9개 항목. 각 항목의 대안과 거부 이유는 Rejected alternatives에
  있다.
- **Final record (2026-09-02):** Decision 1–9를 채택한다. C는
  `FindingResolution`/`AssessmentComparison` Contract와 complete immutable input을 받는 결정적
  projection을 구현했다. 계획 집합은 단순 count가 아니라 `(resource_id, rule_id, perspective)`
  전체로 비교하고, 매칭 키도 같은 세 값이며 `rule_version`은 동등성 검사 대상이다. 부분 report는
  비교 입력으로 거부된다. A는 `phase`/`source_assessment_id`/`deployment_id` 영속화와
  `ASSESSMENT#{assessment_id}#PLAN` item의 planned 집합 저장·조회를, D는 apply 완료 뒤의 Actual
  재조회 입력을 제공한다. planned 집합 저장(5번 선행 작업)은 2026-09-02에 들어갔다 —
  `AssessmentEvaluationPlan`이 좌표 집합을 갖고, Worker가 그것을 PLAN item에 쓰며,
  `DynamoDbAssessmentReportStore.get_planned_evaluations()`가 비교 경계에 그 집합을 돌려준다.
  남은 것은 `phase`/`source_assessment_id`/`deployment_id` 영속화와 검증 endpoint 배선이다.
