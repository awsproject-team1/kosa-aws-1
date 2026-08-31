# Multi-Agent 모델 선정 결과

> 이 문서는 추정이나 외부 벤치마크가 아니라 **Amazon Bedrock Converse API를 실제로 호출한 결과**를 근거로 작성했습니다.
>
> 측정 당시 기준은 `dev`의 `c6a05a2`이며, 현재 통합 검토 기준은 `origin/dev`의 `4d68f5a`입니다.
> 아래 네 결과는 **실측 추천 후보**이며 active runtime assignment가 아닙니다.
>
> 재현 방법은 문서 맨 아래 [부록: 재현 방법](#부록-재현-방법)을 참고하세요.

## 최종 결론 (한눈에 보기)

| Runtime 역할 | 실측 추천 후보 | 실측 근거 | 요청당 중앙 토큰* | 실측 중앙 지연 | 상태 |
| --- | --- | --- | ---: | ---: | --- |
| Parent (Policy Q&A 포함) | **Gemma 3 4B IT** (`google.gemma-3-4b-it`) | 기존 routing+Q&A 4 Case, 20/20 유효, 결정 일치율 100% | 164 | 666 ms | 추천 후보, 승인 Profile 전환 대기 |
| Assessment | **Nova Micro** (`amazon.nova-micro-v1:0`) | S3 FAIL Case 5/5, 결정 일치율 100%, score 편차 0 | 450 | 881 ms | 추천 후보, Golden 재검증·승인 전환 대기 |
| Remediation | **Devstral 2 123B** (`mistral.devstral-2-123b`) | legacy composite 전체 Gate 5/5 | 664 | 5,563 ms | 추천 후보, 승인 Profile 전환 대기 |
| Deployment | **Nova Lite** (`amazon.nova-lite-v1:0`) | composite의 Deployment boundary 6개 항목 5/5 | 726 | 1,749 ms | 추천 후보, 승인 Profile 전환 대기 |

\* `usage.totalTokens`의 반복 실행 중앙값입니다. Bedrock 응답에 달러 비용은 포함되지 않고 이번 평가에서 외부 가격표를 사용하지 않았으므로 금액을 만들지 않았습니다. Deployment의 latency/token은 composite 응답 전체 측정값이며 Deployment-only 호출 수치는 아닙니다.

### 결론 해석

최신 설계에는 별도 `POLICY_QA` Model Profile과 결합 `REMEDIATION_DEPLOYMENT` Model Profile이 없습니다.

- Policy Q&A는 **Parent 내부의 동기 capability**입니다.
- Assessment, Remediation, Deployment는 각각 별도 Queue/Worker/Subgraph와 승인 Model Profile을 사용합니다.
- 기존 benchmark의 `parent`, `policy_qa`, `assessment`, `remediation_deployment`는 런타임 Agent 목록이 아니라 과거 평가 capability 구분입니다.

기존 결과를 최신 네 runtime 역할에 다음과 같이 적용했습니다.

1. 기존 `parent`와 `policy_qa` 결과를 합쳐 Parent Profile 추천 후보를 재집계했습니다.
2. `assessment` 결과를 Assessment Profile 추천 후보 측정에 사용했습니다.
3. `remediation_deployment` 전체 Gate의 patch 생성·적용 결과를 Remediation 추천 후보 측정에 사용했습니다.
4. 같은 composite 결과에서 `deployment_id`, `commit_sha`, `plan_hash`, approval binding, Human Approval, OIDC 6개 항목을 분리해 Deployment 추천 후보를 측정했습니다.

> ModelProfile Contract와 persistence는 현재 구현되어 있습니다. 승인된 M0 Assessment fixture/profile은
> `assessment-nova-lite-m0-v1`이며 모델은 Nova Lite(`amazon.nova-lite-v1:0`)입니다. Nova Micro는
> 실측 추천 후보이지만 별도 Golden 재검증과 승인 전에는 runtime profile을 변경하지 않습니다.
> Parent, Remediation, Deployment도 역할별 Golden 재검증과 승인된 Profile 전환을 기다리며,
> 이 문서의 추천 결과만으로 active assignment를 바꾸지 않습니다.

---

## 기존 실측 capability 결과

아래 표는 측정 당시 benchmark shape의 historical aggregate입니다. 최신 `EvaluationResult`가
요구하는 authoritative `perspective`와 `model_profile_id` 호환은 현재 `bench/runner.py`에
반영되어 모델 응답이 아니라 benchmark expected/runtime metadata로 재구성됩니다.

| 기존 benchmark capability | 실측 1위 | 유효 실행 | 결정 일치율 | 중앙 토큰 | 중앙 지연 | 최신 설계에서의 의미 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| Parent routing | Gemma 3 4B IT | 15/15 | 100% | 164 | 659 ms | Parent의 routing 일부 |
| Policy Q&A | Voxtral Mini 3B 2507 | 5/5 | 100% | 210 | 610 ms | Parent 내부 Q&A capability |
| Assessment | Nova Micro | 5/5 | 100% | 450 | 881 ms | Assessment 추천 후보 실측 근거 |
| Remediation+Deployment composite | Devstral 2 123B | 5/5 | 100% | 664 | 5,563 ms | Remediation 추천과 Deployment boundary 실측 근거 |

이 표의 네 capability 1위 모델은 네 active runtime Profile 배정을 의미하지 않습니다. Parent는 routing과 Q&A를 결합 재집계했고, Deployment는 composite 응답의 Deployment 전용 검증 항목을 별도로 재집계했습니다. 또한 이 historical aggregate는 측정 당시 benchmark shape에 대한 결과이며, 저장된 실제 호출 결과를 최신 Contract로 다시 검증한 결과가 아닙니다. 최신 `EvaluationResult`의 authoritative `perspective`와 `model_profile_id` 호환은 현재 runner가 benchmark의 expected/runtime metadata에서 재구성합니다.

---

## 왜 이 모델인가 (runtime 역할별 실측 추천 근거)

### Parent (Policy Q&A 포함) 추천 후보 → Gemma 3 4B IT

**최신 역할:** 자연어 요청의 intent와 selector를 해석하고, 정책 질문은 Parent 안에서 직접 답변합니다. 실행 의도는 `ASSESSMENT`, `REMEDIATION`, `DEPLOYMENT` 중 하나를 제안하지만 Job 생성, scope 검증, 승인 결정은 Backend가 수행합니다.

기존 실측 데이터에서 다음 네 Case를 하나의 Parent Profile 평가로 재집계했습니다.

- 정책 질문 routing: 기존 기대값 `POLICY_QA`, 동기 처리
- Assessment 요청 routing
- Remediation/plan 준비 요청 routing
- 승인된 `S3-PUBLIC-001` Policy Q&A

45개 모델 × 4 Case × 5회인 기존 **900회 호출 결과**를 다시 호출하지 않고 결합했습니다.

| 모델 | 유효 실행 | 최소 Case 결정 일치율 | 중앙 지연 | 중앙 토큰 | 판단 |
| --- | ---: | ---: | ---: | ---: | --- |
| **Gemma 3 4B IT** | **20/20** | **100%** | **666 ms** | 164 | 실측 추천 후보 |
| GLM 4.7 Flash | 20/20 | 100% | 731 ms | 157 | 속도 근접 대안 |
| Qwen3-Coder-30B-A3B | 20/20 | 100% | 745 ms | **155** | 완전 유효 후보 중 최소 토큰 |
| Qwen3 32B | 20/20 | 100% | 762 ms | 174 | 대안 |
| Ministral 14B 3.0 | 20/20 | 100% | 814 ms | 177 | 대안 |

Gemma 3 4B는 routing과 Policy Q&A를 합친 네 Case를 모두 5회씩 통과한 후보 중 가장 빨라 실측 추천 후보로 도출했습니다. Qwen3-Coder는 중앙 토큰이 9개 적지만 중앙 지연이 79 ms 높았습니다. Parent runtime Profile 전환은 최신 route Case 재검증과 승인 후 별도로 진행해야 합니다.

> 토큰 총량을 응답속도보다 우선하면 **Qwen3-Coder-30B-A3B**가 대안입니다.

#### Parent 재검증 조건

기존 routing Case는 `POLICY_QA`와 `REMEDIATION_DEPLOYMENT`라는 과거 출력 label을 사용합니다. 최신 설계에서는 Policy Q&A가 Parent-local 처리이고 `REMEDIATION`과 `DEPLOYMENT`가 분리되므로 다음 Case를 추가해 재검증해야 합니다.

- Policy 질문 → Parent-local synchronous response
- Assessment intent → `ASSESSMENT`
- Patch/PR intent → `REMEDIATION`
- readiness/plan/apply 상태 intent → `DEPLOYMENT`
- 모호한 intent와 selector confirmation

### Assessment 추천 후보 → Nova Micro

**최신 역할:** IaC Snapshot과 AWS Actual Evidence를 승인된 Rule과 비교해 `IAC`, `AWS_ACTUAL`, `DRIFT` 관점의 구조화된 `EvaluationResult`를 생성합니다. 현재 승인된 M0 runtime fixture/profile은 Nova Lite 기반 `assessment-nova-lite-m0-v1`입니다.

**실측 Case:** 네 public-access 설정이 모두 비활성화된 S3 bucket을 `S3-PUBLIC-001`로 평가했습니다. 기대 결과는 `FAIL`, `severity=HIGH`, score 0–30, 두 evidence reference와 정확한 rule/rubric/scoring version입니다. 이 historical aggregate는 측정 당시 benchmark shape의 결과이며, 최신 authoritative `perspective`와 `model_profile_id`는 현재 runner가 expected/runtime metadata에서 재구성합니다.

| 모델 | Contract/의미 검증 | 유효 실행 | 결정 일치율 | Score 편차 | 중앙 지연 | 중앙 토큰 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| **Nova Micro** | 통과 | **5/5** | **100%** | **0** | **881 ms** | 450 |
| Qwen3-Coder-30B-A3B | 통과 | 5/5 | 100% | 0 | 921 ms | 449 |
| Nova Pro | 통과 | 5/5 | 100% | 0 | 1,160 ms | 449 |
| Llama 3 70B | 통과 | 5/5 | 100% | 0 | 3,362 ms | **364** |

모든 비교 후보가 상태, severity, score, evidence와 당시 Contract 검증을 5/5 통과했습니다. Nova Micro는 Qwen3-Coder보다 토큰이 1개 많지만 40 ms 빨랐고, Llama 3 70B보다 86개 토큰을 더 사용하지만 약 3.8배 빨라 실측 추천 후보로 도출했습니다. 별도 Golden 재검증과 승인 전에는 Nova Lite runtime profile을 Nova Micro로 변경하지 않습니다.

> 토큰 최우선 대안은 **Llama 3 70B**입니다.

#### Assessment 재검증 조건

- `IAC`, `AWS_ACTUAL`, `DRIFT` 각 perspective
- EC2/RDS/ALB/S3
- PASS, MANUAL_REVIEW, INSUFFICIENT_EVIDENCE, OUT_OF_SCOPE, EXECUTION_ERROR
- 복수 Rule과 Evidence 부족/충돌

### Remediation 추천 후보 → Devstral 2 123B

**최신 역할:** 확정 Finding과 IaC Snapshot을 바탕으로 repository 범위 안의 최소 Terraform patch와 PR 준비물을 생성합니다. AWS Write나 Apply를 직접 수행하지 않습니다.

**기존 합성 Case:** `modules/s3/main.tf`의 S3 public-access block 네 속성만 `false`에서 `true`로 바꾸는 unified diff를 생성하고 합성 base에 실제 적용했습니다. 같은 응답에서 commit/plan/approval binding과 OIDC-only 경계도 확인했습니다.

| 모델 | 합성 품질 Gate | 유효 실행 | Patch 실제 적용 | 중앙 지연 | 중앙 토큰 |
| --- | --- | ---: | --- | ---: | ---: |
| **Devstral 2 123B** | **PASS** | **5/5** | 정확 | **5,563 ms** | 664 |
| GLM 5 | FAIL | 4/5 | 1회 실패 | 6,903 ms | **589** |
| Qwen3-Coder-30B-A3B | FAIL | 0/5 | 기대 결과 불일치 | 1,002 ms | 628 |

Devstral만 허용 경로, 정확한 네 줄 변경, 실제 patch 적용 결과와 전체 composite Gate를 5/5 통과해 실측 추천 후보로 도출했습니다. GLM 5는 75개 토큰이 적지만 4/5로 유효율 Gate를 넘지 못했습니다. Remediation 전용 재검증과 승인된 Profile 전환 전에는 runtime assignment를 바꾸지 않습니다.

#### Remediation 재검증 조건

- Finding과 `IaCSnapshot.base_commit_sha` binding
- patch 없음이 정답인 Actual-only drift
- 복수 파일/금지 경로/과도한 resource 생성
- Terraform 관리 밖 리소스의 `MANUAL_REVIEW`
- PR 준비와 Deployment 실행 권한의 분리

### Deployment 추천 후보 → Nova Lite

**최신 역할:** refresh된 Terraform Plan의 readiness를 검증하고, Human Approval과 `commit_sha`/`plan_hash`를 재확인하며, GitHub Actions OIDC의 Plan/Apply 완료 Event와 post-deploy verification을 처리합니다.

기존 composite 225행에서 다음 6개 Deployment boundary 검증만 분리했습니다.

- 정확한 `deployment_id`, `commit_sha`, `plan_hash`
- `DeploymentApproval.matches()` 기반 approval binding
- `requires_human_approval=true`
- `apply_mechanism=GITHUB_ACTIONS_OIDC_ONLY`

| 모델 | Deployment boundary 통과 | 중앙 지연* | 중앙 토큰* | 판단 |
| --- | ---: | ---: | ---: | --- |
| **Nova Lite** | **5/5** | **1,749 ms** | 726 | 실측 추천 후보 |
| Devstral 2 123B | 5/5 | 5,563 ms | 664 | 대안 |
| Qwen3 VL 235B A22B | 5/5 | 5,722 ms | 637 | 대안 |
| Voxtral Small 24B 2507 | 5/5 | 6,131 ms | 663 | 대안 |
| GLM 5 | 5/5 | 6,903 ms | **589** | 토큰 최소 대안 |
| Ministral 3B | 4/5 | **1,433 ms** | 666 | 유효율 Gate 실패 |

\* 기존 composite 응답 전체의 latency/token입니다. Deployment-only prompt를 호출한 수치는 아닙니다.

8개 모델이 6개 Deployment boundary 항목을 5/5 통과했고, Nova Lite가 완전 통과 후보 중 가장 빨라 실측 추천 후보로 도출했습니다. GLM 5는 137개 토큰이 적지만 약 3.9배 느리고, Ministral 3B는 더 빠르지만 4/5라 제외했습니다. Deployment 전용 재검증과 승인된 Profile 전환 전에는 runtime assignment를 바꾸지 않습니다.

> 토큰 총량을 최우선으로 하면 **GLM 5**가 대안입니다.

#### Deployment 재검증 조건

기존 subset은 식별자와 승인·실행 경계만 측정했습니다. 실제 배포 전 다음 Case를 추가해야 합니다.

- refresh plan readiness와 현재 Actual 적용 가능성
- plan/apply 완료 Event 처리와 checkpoint 재개
- 불명확한 Apply 결과의 `MANUAL_REVIEW`
- 승인 없는 Apply 차단
- post-deploy Actual Compliance/Drift 재평가

---

## 평가 기준

| 기준 | 설명 | 특히 중요한 Profile |
| --- | --- | --- |
| 구조화 출력 유효성 | JSON 파싱과 역할별 Contract/필수 필드 준수 | 전체 |
| 판단·라우팅 정확도 | Parent-local Q&A 또는 올바른 Workflow 제안 | Parent |
| 근거 인용 정확도 | 승인된 rule ID/version과 evidence reference 일치 | Parent Q&A, Assessment |
| Finding 정확도 | perspective, status, severity, score, Evidence와 버전 일치 | Assessment |
| 반복 안정성 | Case별 결정 일치율과 Assessment score 편차 | 전체 |
| Diff 최소성·적용성 | 허용 repository/path만 변경하고 base에 실제 적용 | Remediation |
| Readiness·승인 경계 | refresh plan, commit/plan binding, Human Approval, OIDC-only Apply | Deployment |
| 지연 | API 호출부터 응답 수신까지의 시간 | 전체, 특히 Parent |
| 토큰 | 실제 `usage.totalTokens`; 비용 계산의 입력값 | 전체 |

### 선정 순서

1. 의미 검증 유효율 90% 이상
2. Case별 결정 일치율 또는 역할별 exact check 통과율 90% 이상
3. Assessment는 반복 score 최대 편차 10 이하
4. Gate 통과 후보를 유효율과 반복 안정성 순으로 정렬
5. 앞 기준이 같으면 중앙 지연, 중앙 토큰 순으로 정렬

최저 토큰만으로 고르지 않았습니다. 역할 정확도와 반복 안정성을 먼저 보장하고 같은 품질에서 latency를 우선한 뒤 토큰을 비교했습니다.

## 평가 규모와 결과 무결성

- Region: `us-east-1`
- 대상: `ACTIVE` + `ON_DEMAND` + Text→Text 모델 45개
- 기존 최종 강화 비교: 45개 모델 × 6개 Case × 5회 = **1,350회**
- 탐색·Shortlist·강화 평가 포함 세션 전체 실제 호출: **3,115회**
- Parent 통합 재집계: 기존 `parent`+`policy_qa` 900행, 추가 API 호출 없음
- Deployment boundary 재집계: 기존 composite 225행, 추가 API 호출 없음
- 최종 JSON의 `(role, case_id, model_id, run_number)` 1,350개 조합 모두 고유
- 저장 summary와 `bench.runner.summarize()` 재계산 결과 일치
- 원시 Prompt·응답·credential은 저장하지 않고 hash, 검증 항목, latency, usage와 안정된 오류 종류만 저장

45개 전체의 기존 capability 집계는 [`data/bedrock-model-evaluation-20260831.md`](data/bedrock-model-evaluation-20260831.md)에서 확인할 수 있습니다.

## 최신 설계 적용 상태

| 항목 | 상태 |
| --- | --- |
| Parent routing+Q&A 결합 후보 재집계 | 완료 — Gemma 3 4B 20/20 추천 후보 도출 |
| Assessment 단일 S3 Case | 완료 — Nova Micro 5/5 추천 후보 도출 |
| Remediation composite 전체 Gate | 완료 — Devstral 5/5 추천 후보 도출 |
| Deployment boundary subset 재집계 | 완료 — Nova Lite 5/5 추천 후보 도출 |
| 최신 Parent output/route Case 재호출 | 미완료 |
| Assessment Golden Dataset 확장 | 미완료 |
| Remediation 전용 평가 | 미완료 |
| Deployment 전용 평가 | 미완료 |
| active Model Profile runtime Contract/persistence | 구현 완료 — 승인된 M0 Assessment Profile은 `assessment-nova-lite-m0-v1` |
| 추천 후보의 approved runtime Profile promotion | 미완료 — 역할별 Golden 재검증·승인 대기 |

ModelProfile Contract와 persistence는 현재 구현되어 있습니다. 승인된 active M0 Assessment fixture/profile은 Nova Lite 기반 `assessment-nova-lite-m0-v1`이며, Nova Micro는 측정상 추천 후보일 뿐 Golden 재검증과 승인 전에는 runtime profile로 promotion하지 않습니다. Parent, Remediation, Deployment 추천 후보도 역할별 Golden 재검증과 승인된 Profile 전환을 기다립니다.

---

## 부록: 재현 방법

### 기존 45개 모델 평가 재현

실행 전에 `AWS_BEARER_TOKEN_BEDROCK` 또는 표준 AWS credential을 로컬 환경에 설정합니다. 실제 credential을 채팅, 명령행, 코드, 결과 문서 또는 Git에 넣지 않습니다.

```powershell
.venv\Scripts\python.exe -m pip install -r bench\requirements.txt
.venv\Scripts\python.exe bench\run_bench.py all `
  --all-available-models --runs 5 --max-workers 8 --execute
```

현재 CLI의 `parent`, `policy_qa`, `assessment`, `remediation_deployment`는 기존 benchmark capability key이며 최신 런타임 Profile 이름이 아닙니다.

`--execute`를 빼면 유료 API를 호출하지 않는 dry run입니다.

### 재집계 방법

- Parent: 기존 `parent`와 `policy_qa` 행을 하나의 `parent_profile`로 묶어 `summarize()` 적용
- Deployment: 기존 `remediation_deployment` 행에서 `deployment_id`, `commit_sha`, `plan_hash`, `approval_binding`, `human_approval`, `apply_mechanism`이 모두 `true`인 실행을 통과로 집계

재집계는 저장된 sanitized JSON만 사용하며 추가 Bedrock API 비용이 발생하지 않습니다.

### 관련 파일

- `docs/DESIGN.md`: 최신 Parent/Assessment/Remediation/Deployment 구조
- `docs/decisions/ADR-0012-natural-language-orchestration-and-model-profiles.md`: Model Profile 결정
- `docs/CONTRACTS.md`: 구현된 ModelProfile Contract와 active Profile 조건
- `bench/config.py`: 기존 benchmark capability별 후보 Model ID
- `bench/cases.py`: 기존 합성 입력과 기계 검증 기대값
- `bench/run_bench.py`: 모델 발견, 병렬 Converse 호출과 결과 저장
- `bench/runner.py`: Contract/의미 검증, 품질 Gate와 sanitized report
- `bench/results/`: Git 제외된 실행별 상세 JSON/Markdown
- `docs/evaluations/data/bedrock-model-evaluation-20260831.md`: 기존 45개 모델 전체 집계

### 요청당 비용 계산 방법

달러 비용이 필요하면 실행 시점·Region·model ID의 AWS 공식 Bedrock 단가를 고정해 계산해야 합니다.

```text
요청당 비용 = (inputTokens / 1,000,000 × input 단가)
            + (outputTokens / 1,000,000 × output 단가)
```

모델마다 input/output 단가가 다르므로 total token이 가장 적다고 실제 달러 비용도 반드시 가장 낮은 것은 아닙니다.

### 감사 메타데이터

- 기준 dev commit: `c6a05a2`
- boto3: `1.43.78`
- 실행 설정: `runs=5`, `max_workers=8`, `temperature=0`
- 최종 결과 생성 시각: `2026-08-31T02:31:41.794727+00:00`
- `bench/cases.py` LF-normalized SHA-256: `b715ccf02116e0cb68da8e49c822ff9390959d3c4102879c00f9be51a9afd5da`
- `bench/config.py` LF-normalized SHA-256: `b6f64e32499dd470bdf08fd2ee4e691e4e6f325fff90af69e327c5540141ed38`
- 최종 평가 실행 당시 `bench/runner.py` LF-normalized SHA-256: `ed310132e2492de5a17e1d7bd11a7419450a90f0e332db59e6df0fc98c9bf107`
- 현재 재평가용 `bench/runner.py` LF-normalized SHA-256: `cd9c5c97671a853d4f2b6d029daff672d765353dee7b133d37e8a000eef196fc`
- 전체 집계 SHA-256: `f9e366c73a2be52814d069d0ef4611e584e2e3bfeb9cbec8e6b793a59d9f4587`
