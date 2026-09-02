# ADR-0022: M4 Golden customer-runtime observation handoff

> **상태: Accepted (2026-09-03)** — M4 B·C 구현 기본값으로 채택한다. A·D는 통합 리뷰에서 producer 필드와 실제 protected run binding을 확인한다.
>
> **Owner:** C(quality gate consumer/report) + A(customer runtime observation producer/관측 결합) + D(demo repository/deployment binding)
>
> **관련:** ADR-0002, ADR-0003, ADR-0011, ADR-0014, ADR-0019, ADR-0020, ADR-0021

## Context

ADR-0021은 실제 customer sandbox의 Golden 반복 평가 리포트를 `dev → main` release evidence로 요구한다. 그러나 다음 경계가 정해지지 않았다.

1. 로컬 benchmark가 customer runtime 실행을 대신할 수 있는지 여부
2. 18개 Golden Case 중 `DRIFT`를 Bedrock에 호출할지 Code로 파생할지 여부
3. raw Prompt/응답, customer resource identifier, IaC/policy body를 공개 release 첨부물에 넣지 않고도 실행을 검증하는 방식
4. A의 관측·비용 자료와 D의 demo commit/deployment가 C 품질 리포트와 같은 실행임을 결합하는 키

## Decision

### 1. 실제 평가는 customer-deployed Assessment runtime만 생산한다

로컬 `bench/` 결과, fixture evaluator, expected 값 echo, 임의 생성 observation은 release evidence가 아니다. A가 보호된 customer runtime에서 실행한 결과를 identifier-only observation bundle로 내보내고 C gate가 이를 소비한다. Bundle은 `runtime_mode=CUSTOMER_SANDBOX`와 platform commit, approved Model Profile 전체를 포함한다.

이 문자열만으로 실행 진위를 자동 증명한다고 주장하지 않는다. Release reviewer는 별도 보호 저장소의 run URL/승인 기록을 확인하고, 공개 저장소에는 그 실행에서 계산한 digest와 sanitized report만 첨부한다.

### 2. M4 live gate는 Post-Deploy 18 Case를 5회 반복한다

`fixtures/m1/golden_dataset_post_deploy_cases.json`의 6 Rule × `IAC`/`AWS_ACTUAL`/`DRIFT`를 사용한다. 각 Case는 정확히 5개 run을 가져야 하며 누락·추가·중복 run은 분모 조정 없이 거부한다.

- `IAC`, `AWS_ACTUAL`: approved Assessment Model Profile의 Bedrock 실행, 총 12 Case × 5 = 60 calls
- `DRIFT`: 같은 `(Rule, run_number)`의 IAC/Actual에서 `derive_drift_results()`로 파생, 총 6 Case × 5 = 30 results
- 전체: 18 Case, 90 observations

DRIFT가 Bedrock 사용량을 보고하거나 Code 파생값이 IAC/Actual 쌍과 다르면 입력 전체를 거부한다.

Initial 18 Case는 fixture/closed-loop Coverage에 남고 ADR-0011 의미 회귀를 검증한다. M4 실제 반복 리포트는 `PROGRESS.md`와 ADR-0021이 요구한 Post-Deploy 18 Case를 대상으로 한다.

### 3. Observation bundle은 private input, report는 sanitized output이다

Bundle은 다음 식별자와 평가 메타데이터만 허용한다.

- opaque execution ID, offset-aware 생성 시각
- platform commit SHA
- demo repository commit, deployment ID, artifact set의 SHA-256
- exact Model Profile ID/role/Region/model/prompt/rubric/Golden version
- case/run/rule version/phase/perspective/status/severity/score
- evidence reference, resource ID hash, input artifact hash, output hash
- Bedrock latency/token 또는 Code-derived 표지
- provider message가 아닌 안정된 error code

Schema에는 raw Prompt/응답, rationale, credential, Role ARN, account ID, repository URL, resource ID 원문, policy text, IaC body 필드가 없다. Strict exact-key parser가 추가 필드를 거부한다. Private bundle은 고객 승인 저장소에 보관하고 Git에 커밋하지 않는다.

Sanitized report에는 per-case/per-perspective/전체 정확도, Evidence 정확도, 일치율, score 편차, 오류 수, 호출 수, 토큰 합계, p95 지연과 observation-set digest만 남긴다. Evidence reference나 resource hash도 공개 report에는 복사하지 않는다.

### 4. Gate와 handoff 결합

- **A producer:** customer runtime observation bundle과 같은 `execution_id`의 관측·비용 집계를 제공한다.
- **D producer:** 실제 demo repository commit/deployment/artifact set 값을 제공하며 bundle에는 원문 대신 SHA-256으로 결합한다.
- **C consumer:** exact approved Profile/Golden fixture와 bundle을 검증하고 sanitized PASS/FAIL report를 만든다.
- **Release reviewer:** protected run/approval과 세 digest가 가리키는 실행을 확인한다.

품질 기준은 각 Case, 각 perspective, 전체에서 status/score/evidence 정확도와 same-case agreement 90% 이상, score spread 10 이하이며 `EXECUTION_ERROR`와 provider error는 0건이어야 한다. 한 Case라도 실패하면 전체 Gate는 실패한다.

## Consequences

- 실제 자격 증명 없이도 validator와 dry-run을 구현·검증할 수 있지만 release PASS 증적은 만들 수 없다.
- 18 Case를 모두 Bedrock 호출하지 않아 ADR-0011의 AI/Code 경계를 지키고 호출 수·비용을 과대 계상하지 않는다.
- 공개 report만으로 raw 결과를 재구성할 수 없으므로 보호 저장소의 private bundle과 run 기록을 release reviewer가 함께 확인해야 한다.
- A/D producer가 필드 이름이나 digest 대상을 변경하려면 이 ADR과 `docs/CONTRACTS.md`, parser/test를 같은 변경에서 갱신한다.

## Rejected alternatives

- **Fixture evaluator 결과를 release evidence로 사용:** 실제 모델·customer runtime을 검증하지 않으므로 거부한다.
- **로컬 benchmark에서 synthetic prompt를 직접 호출:** 승인된 artifact resolver와 runtime scope를 우회하므로 거부한다.
- **DRIFT도 Bedrock으로 호출:** production의 결정적 파생과 결과가 갈리고 비용을 잘못 기록하므로 거부한다.
- **raw observation을 PR에 첨부:** 고객 resource/evidence가 공개될 수 있어 거부한다.
- **18 Case 전체 평균만 평가:** 일부 Rule/perspective 실패를 다른 Case가 가릴 수 있어 거부한다.
