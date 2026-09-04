# ADR-0023: 정책 문서에서 승인된 Rule까지의 Authoring 경계

> **상태: Accepted (2026-09-03)** — 고객 정책 업로드부터 Assessment 실행까지의 경로를 이 결정으로
> 잇는다.
>
> **결정 대상:** 제품이 평가할 수 있는 범위를 무엇이 정의하는지, AI가 무엇을 제안하고 무엇을
> 제안할 수 없는지, 승인된 Rule이 Runtime에 어떻게 도달하는지, 자동 평가할 수 없는 요구사항을
> 어떻게 남기는지.
>
> **관련:** ADR-0002, ADR-0004, ADR-0011, ADR-0015, ADR-0017, ADR-0020

## Context

ADR-0015가 고객 정책 업로드·정규화·승인 경계를 정했고, ADR-0004가 정책 원문을 저장소 밖에
두기로 했다. 그러나 정규화된 문서에서 **승인 가능한 Rule을 만드는 경로**가 없었다. 그 결과
Runtime은 `fixtures/rules/`에 커밋된 Rule을 읽었고, 고객이 무엇을 업로드하든 평가 결과는
같았다 — 업로드·정규화·승인 단계 전체가 결과에 아무 영향을 주지 않는 장식이었다.

구체적으로 다음이 비어 있었다.

1. Requirement를 추출하는 production 코드
2. 제품이 자동 평가할 수 있는 통제와 근거 수집 능력의 정본
3. 승인 시 `RULE#{rule_id}#VERSION#{version}` item을 만드는 경로
4. Runtime이 fixture 대신 고객 partition을 읽는 배선
5. 자동 평가할 수 없는 요구사항을 왜곡 없이 남기는 방법

## Decision

### 1. 자동화 경계는 code-owned Governance Control Catalog가 정의한다

`apps/backend/policy/control_catalog.py`가 제품이 아는 통제, 그 통제가 적용되는 resource type,
지원하는 실행 유형, 사용 가능한 evidence capability, 실제 Tool binding을 선언한다.

**Catalog는 정책 문서의 내용을 저장하는 곳이 아니다.** 고객 정책 문서와 제품이 실제로 실행할 수
있는 평가 기능 사이의 경계를 정의한다. 이 경계가 없으면 "AI가 그렇게 판단했다"가 곧 "제품이
평가할 수 있다"가 되고, 실행 경로가 없는 Rule이 승인 가능해진다.

Catalog는 세 지원 수준을 구분한다.

| `automation_support` | 뜻 |
| --- | --- |
| `AVAILABLE` | 지금 자동 평가할 수 있다 |
| `KNOWN_UNSUPPORTED` | 제품이 아는 통제이지만 실행 경로가 없다 |
| `MANUAL` | 사람 검토로만 종결된다 |

**"Catalog에 존재한다"와 "지금 자동 평가할 수 있다"를 같은 의미로 쓰지 않는다.** 지우면 추출기가
그 통제를 다른 통제로 잘못 매핑하고, `AVAILABLE`로 두면 실행되지 않을 Rule이 승인 가능해진다.
`EC2_SNAPSHOT_NOT_PUBLIC`이 이 자리에 있다 — M1 planner가 Snapshot work를 만들지 못한다.

### 2. AWS와 IaC의 evidence capability는 비대칭이다

AWS Actual adapter는 구조화된 projected document를 돌려주므로 `document_paths`가 실제 경로를
가리키는지 검증할 수 있고, Runtime은 그 경로로 **모델을 부르기 전에** 근거 유무를 판정한다.

IaC evaluator는 raw HCL 텍스트를 받고 Evidence locator는 `terraform:{path}` 파일 단위다. 따라서
IaC binding이 갖는 Terraform hint는 prompt 경계와 리뷰 화면 설명에만 쓰는 **non-authoritative**
값이며, 자동 판정 근거도 pre-flight hard gate도 아니다.

두 값을 같은 필드에 담으면 hint가 attribute-level 증거로 오해되어, HCL을 파싱하지도 않은 채
자동 판정의 근거가 된다. IaC attribute-level 사전 검증에는 별도의 HCL parser/projection 계층이
필요하며 이번 범위에 없다.

**정정 2026-09-05 — 게이트는 fail-closed다 (ADR-0024 §3).** 처음 구현은 Rule이 요구한 capability에
이 resource type의 AWS_ACTUAL binding이 없으면 검사를 건너뛰고 모델에게 물었다. 그러면 Catalog가
"이 Control은 AWS 근거가 없다"고 이미 아는 좌표에서 모델이 다른 field를 근거로 인용한다 — baseline의
S3 ACL Rule이 public-access-block 플래그를 근거로 PASS를 낸 것이 그 경우다. 선언이 없으면 그 좌표는
`INSUFFICIENT_EVIDENCE`이고 모델 호출은 없다. legacy Rule도 `LEGACY_RULE_CONTROL_KEYS`로 같은
게이트를 지난다.

### 3. IaC attribute-level pre-flight를 만들지 않는다

만들려면 HCL을 파싱해 resource·attribute 단위로 투영하는 계층이 필요하다. 그것 없이 hint만으로
gate를 걸면, 파싱하지 않은 코드에 대해 "근거가 있다/없다"를 주장하게 된다. 그 주장은 실제 위반과
구별되지 않는다. 그래서 IaC는 hard gate 없이 평가하고, 결과 evidence만 파일 경로 allow-list로
제한한다.

### 4. LLM은 Rule을 제안하고, 판정하지 않는다

`ExtractedRequirement`에는 `judgment`·`severity`·`score`·`source_score`·`anchor`를 **정의하지
않는다.** prompt로 금지하는 것과 schema에 자리가 없는 것은 다르다 — 자리가 있으면 언젠가 채워진다.

- severity는 Catalog의 `default_severity`가 정하고, AI는 `severity_guidance` 텍스트만 쓴다.
  리뷰 API는 그 값을 read-only `proposed_severity`로 노출한다.
- `SourceReference`는 AI 출력에서 복사하지 않는다. AI는 locator만 돌려주고, 서버가 정규화 문서에서
  digest를 조회해 만든다. 모델이 digest를 지어내면 Evidence는 검증 가능한 값이 아니라 모델의
  주장이 된다.
- Catalog 밖 evidence를 요구하면 그 항목을 **빼고 Rule을 만들지 않는다.** 후보 자체를 거절한다.
  빼고 만들면 승인된 Rule과 AI가 제안한 Rule이 달라지고 그 차이가 아무 데도 기록되지 않는다.
- LLM은 코드를 생성하지 않고 예외를 승인하지 않는다.

### 5. `UNSUPPORTED`와 `OUT_OF_SCOPE`는 다른 것이다

`CandidateClassification.UNSUPPORTED`는 **authoring** 단계의 답이다: "이 요구사항으로 제품이 만들
수 있는 Rule이 없다." `EvaluationStatus.OUT_OF_SCOPE`는 **Runtime**의 답이다: "승인된 이 Rule은
이 대상에 적용되지 않는다."

두 Enum 사이에 alias나 범용 변환 함수를 만들지 않는다. 서로 다른 질문에 답하므로, 하나로 합치면
"만들 수 없었다"와 "적용되지 않았다"가 결과에서 구별되지 않는다.

같은 이유로 **AUTOMATABLE 후보가 검증에 실패해도 자동으로 MANUAL로 바꾸지 않는다.** 그것은 검증
실패로부터 사람이 승인 가능한 Rule을 만들어내는 일이다. 실패한 후보는 rejection code와 함께
보존하되 Rule로 변환하지 않는다.

### 6. 모든 Assessment가 Profile 판본을 고정한다

`policy_profile_version`은 verification 전용 pin이 아니라 **모든 phase**가 갖는 값이다. Assessment
생성 시점의 current pointer를 읽어 저장하고, Runtime은 latest pointer를 따라가지 않고 그 판본을
직접 조회한다.

고정하지 않으면 실행 도중 게시된 새 Profile이 이미 계획된 평가의 Rule 집합을 바꾸고, 그 사실이
결과 어디에도 남지 않는다. Profile은 `POLICY_PROFILE#{id}#VERSION#{version}`(immutable 이력)과
`POLICY_PROFILE#{id}`(current pointer)로 나눠 저장하며, pointer 교체는 `expected_current_version`
낙관적 동시성으로 보호한다.

이에 따라 Runtime configuration과 Policy Catalog의 책임을 분리한다.

    Runtime configuration — 이 고객이 어떤 Repository와 AWS Resource를 읽을 수 있는가
    DynamoDB Policy Catalog — 이 고객이 어떤 게시된 Policy Profile을 쓸 수 있는가

Profile을 배포 JSON key에 두면 고객이 정책을 승인·게시할 때마다 인프라 배포가 필요해진다 —
"승인 직후 평가에 쓸 수 있다"는 목표와 정면으로 충돌한다.

### 7. MANUAL은 새 Perspective이고, 좌표는 Repository 단위로 안정적이다

`EvaluationPerspective.MANUAL`을 additive하게 더한다. 사람이 검토해야 할 조직 통제를 기존
IAC/AWS_ACTUAL/DRIFT 중 하나로 표현하면, 그 결과가 "IaC를 읽고 내린 판단"처럼 보인다.

`ManualReviewEvaluator`는 Bedrock도 AWS/GitHub Tool도 호출하지 않는다. `MANUAL_REVIEW` 상태와
고정 rationale, Rule의 `SourceReference`만으로 결과를 만든다. 그러면 왜 결과를 만드는가 —
**좌표를 남기기 위해서다.** 빼면 Coverage가 그 통제를 아예 모르고, Initial과 Post-Deploy
Verification의 planned set이 달라져 비교가 성립하지 않는다.

대상은 `AWS::Governance::Assessment` 유형의 `governance:{repository_id}`다. **Assessment ID를 쓰지
않는다** — 같은 Repository의 Initial과 Verification이 서로 다른 좌표를 가지면, 정확히 비교하려고
만든 결과가 비교를 불가능하게 만든다.

readiness에서는 **숫자 평균만** 제외한다(`_NON_SCORING_PERSPECTIVES`). 0점이 평균을 끌어내리면 그
숫자는 "아직 검토되지 않았다"가 아니라 "위반이 있다"로 읽힌다. Coverage·plan 완료·Finding에는
그대로 포함된다. **제외 기준은 Perspective이지 status가 아니다** — 기존 IAC/AWS_ACTUAL의
`MANUAL_REVIEW` 점수 의미는 바뀌지 않는다.

### 8. 실행 유형이 Perspective 집합을 정한다

| `evaluation_type` | 실행 Perspective |
| --- | --- |
| `None` (legacy) | IAC + AWS_ACTUAL + DRIFT |
| `IAC` | IAC |
| `AWS` | AWS_ACTUAL |
| `HYBRID` | IAC + AWS_ACTUAL + DRIFT |
| `MANUAL` | MANUAL |

`None`은 authoring 이전에 커밋된 fixture Rule이며 기존 동작을 그대로 보존한다.

**IAC-only와 AWS-only Rule은 Drift로 보내지 않는다.** 한쪽만 평가하는 것이 그 Rule의 정의이므로,
`derive_drift_results()`가 없는 쪽을 "누락된 Perspective"로 읽어 `MANUAL_REVIEW`를 만들면 실제
불일치와 구별되지 않는다.

계획된 좌표 생성·Perspective별 Rule 선택·runner 선택·Drift 대상 선택은 모두
`EvaluationExecutionPlanner` 하나를 통과한다. 답이 여러 곳에 있으면 그 답들이 어긋나고, 채워지지
않는 좌표(coverage 미완료)나 계획에 없는 결과(저장 실패)가 생긴다.

## Consequences

- Runtime은 고객 partition의 `lifecycle == APPROVED` Rule만 평가한다. `fixtures/rules/`는
  bootstrap과 테스트 입력으로만 남고, M0 synthetic 경로는 기존 fixture 방식을 유지한다.
- 정책 원문은 `ExtractionUnit` 안에만 존재하며 그 타입에는 직렬화가 없다. 리뷰어가 보는 문장은
  모델이 쓴 재진술이다.
- 후보 추출은 전용 큐와 전용 IAM Role을 갖는 Worker가 처리한다. 정책 원문을 읽는 권한과 고객 AWS
  계정을 읽는 권한을 한 Role에 두지 않는다.
- **운영 전환:** `policy_profile_version`이 없는 기존 Assessment record는 backfill하거나 queue를
  비운 뒤 배포한다. 최신 pointer로 조용히 대체하지 않는다. 같은 이유로 `entity_type`/`lifecycle`이
  없는 기존 Rule item은 bootstrap을 다시 실행해 갱신한다.
- 자동 승인과, 같은 source version을 다른 모델로 재추출하는 것은 이번 범위에 없다. 후자는
  fail-closed한다.

## 보완 2026-09-05 — 전부-아니면-전무 게이트는 chunk 단위로 좁아진다

§4의 "부분 후보를 저장하는 fail-soft 경로는 허용하지 않는다"는 **ADR-0025로 좁혀졌다.**

이 규칙이 쓰인 시점의 문서는 20 unit(4 chunk)이었다. 334 unit / 67 chunk짜리 ISMS-P 점검표에서
chunk 최종 실패율이 17–33%로 관측됐고, 그 규칙 아래 완주 확률은 0.0004%다
(`docs/evaluations/data/authoring-isms-p-20260905.md`). 규칙이 "요구사항이 조용히 사라지는 것을
막는다"에서 "아무것도 저장하지 못한다"로 바뀐 것이다.

허용하지 않는 것은 이제 *미완료를 감춘 채* 부분 후보를 저장하는 것이다. 실패한 chunk의 locator는
`UnclassifiedUnits`로 결과·저장소·API·화면에 실린다. 청크 안에서의 게이트는 그대로다 — 거부된
응답은 후보를 하나도 내지 못하고, `PoisonedResponseError`는 여전히 실행 전체를 세운다.
