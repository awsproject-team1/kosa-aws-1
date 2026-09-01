# ADR-0020: Post-Deploy Verification과 before/after 비교 경계

> **상태: Proposed (2026-09-02)** — 팀 합의 전이다. 이 ADR이 `Accepted`가 되기 전에는 C가
> 재평가 Assessment 생성과 비교 projection을, A가 검증 Assessment 저장 경계를 구현하지 않는다.
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
- 다르면 비교하지 않고 실패한다(`COMPARISON_PROFILE_MISMATCH`). M1 `DRIFT` 파생이 이미 같은
  규칙을 쓴다 — 서로 다른 Profile/rubric에서 나온 두 판정은 비교 대상이 아니다.
- 이 제약 때문에 Model Profile 교체는 검증 대기 중인 Deployment가 없을 때만 배포한다.

### 4. Finding Resolution은 Code의 결정적 diff다

- 매칭 키는 `(resource_id, rule_id, rule_version, perspective)`다. `finding_id`가 이 좌표에서
  결정적으로 만들어지므로(ADR-0016) 두 Assessment 사이에서 안정적으로 대응한다.
- 값 어휘는 다음 다섯 개다.

| 값 | 조건 |
| --- | --- |
| `RESOLVED` | 원 Finding 좌표의 새 결과가 `PASS` |
| `UNRESOLVED` | 새 결과가 여전히 `FAIL` |
| `REGRESSED` | 원 결과가 `PASS`였는데 새 결과가 `FAIL` (원 Finding이 없던 좌표의 신규 위반) |
| `INDETERMINATE` | 새 결과가 `MANUAL_REVIEW`, `INSUFFICIENT_EVIDENCE`, `EXECUTION_ERROR`이거나 `rule_version`이 달라 비교 불가 |
| `NO_LONGER_APPLICABLE` | 새 결과가 `OUT_OF_SCOPE`이거나 리소스가 더 이상 존재하지 않음 |

- 이 판정은 AI가 하지 않는다. `DRIFT` 파생과 같은 근거로, 두 immutable 결과의 기계적 비교다.
  모델에게 before/after를 요약하게 하면 판정 정본이 둘로 갈린다.
- `rule_version`이 달라진 경우 `INDETERMINATE`로 두고 이유 코드를 남긴다. 다른 version의 Rule은
  다른 질문이므로 해소로 읽지 않는다.

### 5. 점수·Coverage 변화는 비교 가능할 때만 표시한다

- delta는 다음 조건을 모두 만족할 때만 계산한다.
  1. 두 `readiness_score`가 모두 non-null (= 두 평가 계획이 완전히 Coverage됨)
  2. 두 Assessment의 planned `(resource_id, rule_id, perspective)` 집합이 동일
  3. `model_profile_id`와 `rubric_version`이 동일 (3번)
- 하나라도 어긋나면 `comparable = false`와 이유 코드를 반환하고 delta를 만들지 않는다. Frontend는
  `comparable = false`에서 숫자 변화를 표시하지 않고 이유를 보여준다.
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

- apply 완료 Event 확인 후 **30초 고정 지연** 뒤 첫 재조회를 시작한다.
- 기대와 다른 Actual을 읽으면 기존 재시도 규칙(작업별 총 3회, `docs/DESIGN.md`)을 재사용해 다시
  조회한다. AWS 전파 지연과 실제 미적용을 한 번의 읽기로 구분할 수 없기 때문이다.
- 3회 후에도 다르면 `VERIFICATION_FAILED`가 아니라 `VERIFICATION_INDETERMINATE`로 두고 사람에게
  보낸다. 전파 지연을 정책 위반으로 확정하면 데모와 신뢰가 함께 깨진다.
- 지연·횟수는 이 ADR이 정하는 값이며 개별 구현이 바꾸지 않는다. 변경은 이 ADR 개정으로 한다.

### 9. Deployment 단계에는 LLM을 두지 않는다

- Deployment Readiness는 결정적 Code이며 모델을 호출하지 않는다. Post-Deploy Verification의 평가는
  Assessment Profile을 재사용한다(3번).
- 따라서 MVP에서 Deployment 역할 Model Profile을 배정하지 않는다.
  `docs/evaluations/BEDROCK_MODEL_SELECTION.md`의 Deployment 후보는 근거 기록으로만 남긴다.
- 결정적 판정 단계를 임의로 LLM화하지 않는다. ADR-0018이 제거한 "판정 정본이 둘"인 구조가 다시
  생긴다.

## Consequences

- before/after 양쪽 결과가 각각 immutable Assessment로 남아 감사와 데모 재현이 가능하다.
- result SK를 바꾸지 않으므로 M1 저장·조회·Coverage 코드가 그대로 재사용된다.
- 전체 재평가가 기본이므로 비교 규칙이 단순해지는 대신 Bedrock 호출 수가 Deployment마다 평가 계획
  전체만큼 발생한다. 현재 규모(18개)에서는 수용 가능하며, Rule 확장 시 축소 재평가와
  `comparable=false` 표기를 재검토한다.
- Model Profile 동일성 강제 때문에 Profile 교체 시점이 Deployment 수명과 결합된다.
- Finding Resolution이 Code 판정이므로 Golden Dataset 확장 없이도 결과가 결정적이다.

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

## Open decision

- **Owner:** C(재평가 Agent·비교 projection) + A(검증 Assessment 저장·조회 API) + B(재평가 적용
  범위와 예외 표시 규칙)
- **Needed by:** M3 C/A 착수 전. 특히 1번(새 Assessment)과 3번(Profile 동일성)은 저장 구조와
  비교 의미를 동시에 결정하므로 구현 전에 필요하다.
- **Blocks:** M3 A(결과 조회 API, Assessment record 필드 추가), M3 B(재평가 적용 범위 검증),
  M3 C(Before/After 비교, Finding Resolution), M4 C(품질 목표 확인), 데모의 점수 변화 화면.
- **Proposed options:** 위 Decision 9개 항목. 각 항목의 대안과 거부 이유는 Rejected alternatives에
  있다.
- **Final record:** 미정. 합의 시 상태를 `Accepted`로 바꾸고 `FindingResolution`,
  `AssessmentComparison` Contract 추가와 `docs/DATABASE.md`의 Assessment 필드 확장을 같은 PR에서
  진행한다.
