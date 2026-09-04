# ADR-0018: Remediation 정책 판정 소비와 C-owned Worker 오케스트레이션

> **상태: Accepted (2026-09-01)**
>
> **결정:** A가 B의 `RemediationPolicy.decide()`를 호출하고 판정을 영속화한다. C가
> revision-bound Remediation Agent/Worker 오케스트레이션을 소유한다. D는 C Worker에 주입되는
> Patch/Sync 실행 port와 이후 GitHub/Terraform/Deployment Worker를 소유한다.
>
> **관련:** ADR-0007, ADR-0013, ADR-0016, ADR-0017

## Context

B의 정책 경계는 Finding 하나를 `TERRAFORM_PATCH`, `ACTUAL_SYNC`, `MANUAL_REVIEW`,
`SUPPRESSED` 중 하나로 판정한다. 기존 A/C 구현에는 다음 공백이 있었다.

1. A API가 B 정책과 고객 예외를 호출하지 않고 C의 별도 `RemediationStrategy`로 Job을 골랐다.
2. C context builder가 IaC/Actual 결과만으로 Patch/Sync/Manual을 다시 판정해 조치 정본이 둘이었다.
3. `GENERATE_REMEDIATION`을 소비할 Worker가 없었고 ADR 초안은 이를 D 소유로 제안했다.
4. `ACTUAL_SYNC`가 D Deployment Worker 명령인 `RUN_DEPLOYMENT`로 잘못 연결됐다.

팀 결정은 Assessment/Remediation Agent와 그 오케스트레이션이 C 소유라는 것이다. D의 경계는
결정적인 GitHub/Terraform/AWS 실행 adapter와 Deployment Worker이며, 정책 판정을 다시 계산하거나
Remediation Agent를 소유하지 않는다.

## Decision

### 1. 역할과 코드 소유권

| 경계 | 소유자 | 책임 |
| --- | --- | --- |
| Remediation HTTP API, authorization, target/exception read, Job/Outbox/Queue, revision, decision/audit persistence | A | JWT-derived customer scope에서 B 정책을 호출하고 action별 durable workflow를 만든다 |
| `RemediationPolicy.decide()`와 허용 범위/예외/Manual Review 의미 | B | 순수 판정만 하며 상태·Queue·GitHub·Terraform을 소유하지 않는다 |
| `RemediationContext`, Remediation Agent/Worker, command/action 검증, evidence/readiness 오케스트레이션 | C | 저장된 판정을 authoritative work와 함께 재조회해 정확히 한 실행 port를 호출한다 |
| Patch/Sync port 구현, GitHub branch/commit/PR, Terraform plan/apply, Deployment Worker | D | C에 주입되는 결정적 실행 도구와 승인 후 배포를 구현한다 |

`apps/backend/remediation/worker.py`의 `RemediationWorker`는 C 소유다. D는 이 Worker를 소유하거나
정책을 재계산하지 않는다.

### 2. `RemediationDecision`만 action 정본이다

`RemediationStrategy`는 제거한다. C의 `RemediationContext`는 다음 immutable 사실만 보존한다.

- authoritative `Finding`
- exact `IaCSnapshot`
- deduplicated evidence references

`build_remediation_context()`는 Finding과 IAC/AWS_ACTUAL 결과의 Resource·Rule·version·perspective
identity를 검증하고 evidence를 합칠 뿐 action을 선택하지 않는다. Deployment Readiness도
context strategy를 보지 않는다. non-actionable decision은 plan 단계에 도달할 수 없기 때문이다.

### 3. A가 정책 판정을 호출한다

단건 사용자 요청 `POST /findings/{findingId}/remediations`의 순서는 고정한다.

1. `START_REMEDIATION` 권한과 JWT customer scope를 검증한다.
2. C context와 authoritative Finding을 읽는다. Client는 Finding/lifecycle/tenant 필드를 보내지 않는다.
3. A-owned `RemediationTargetReader`와 customer-scoped `RemediationExceptionReader`를 호출한다.
4. offset-aware server time과 함께 B `RemediationPolicy.decide()`를 한 번 호출한다.
5. decision의 Finding/Resource/Rule/version/perspective가 context와 정확히 같은지 검증한다.
6. decision을 immutable remediation/audit record에 저장한 뒤 action별로 응답·dispatch한다.

| Decision action | HTTP | Job/Outbox | C command |
| --- | --- | --- | --- |
| `TERRAFORM_PATCH` | `202` + decision + Job | 생성 | `GENERATE_REMEDIATION` |
| `ACTUAL_SYNC` | `202` + decision + Job | 생성 | `SYNC_ACTUAL_STATE` |
| `MANUAL_REVIEW` | `200` + `manual_review_code`, Job `null` | 생성하지 않음 | 없음 |
| `SUPPRESSED` | `200` + `exception_id`, Job `null` | 생성하지 않음 | 없음 |

`RUN_DEPLOYMENT`는 D Deployment Worker 명령으로 남으며 C Remediation Worker가 소비하지 않는다.

### 4. decision/context/Job/Outbox는 durable state다

Actionable decision은 remediation context, decision, revision-zero Job, 최소 `WorkflowTask` Outbox,
`REMEDIATION_DECIDED` audit event와 같은 conditional transaction에 저장한다. Queue payload는 ADR-0013
규칙대로 `job_id`, `expected_revision`, `command`만 포함하며 decision이나 customer scope를 복사하지
않는다.

`MANUAL_REVIEW`와 `SUPPRESSED`는 remediation decision와 audit event만 같은 transaction에 기록한다.
Job/Outbox를 만들지 않는다. Worker는 판정을 다시 계산하지 않으므로 판정 뒤 등록된 예외가 이미
저장된 action을 소급 변경하지 않는다. 대기 작업 취소가 필요하면 별도 A operation으로 다룬다.

고객 예외는 A가 customer partition에 immutable record와 audit event로 저장한다. ID, customer,
approver, approval time은 Backend가 발급하고, reason은 enum이며 expiry가 필수다. B는 판정 시 전달된
예외만 읽는다.

### 5. C Worker는 revision-bound stored decision을 집행한다

C `RemediationWorker.handle(task)`는 다음을 fail-closed로 강제한다.

- 허용 command는 `GENERATE_REMEDIATION`, `SYNC_ACTUAL_STATE`뿐이다.
- A repository에서 `job_id + expected_revision`으로 work를 다시 읽는다.
- missing/stale/mismatched customer/job/revision/context/decision은 실행 port 호출 전에 거부한다.
- decision identity는 context Finding과 정확히 같아야 한다.
- command/action matrix는 고정이다.
  - `GENERATE_REMEDIATION` ↔ `TERRAFORM_PATCH`
  - `SYNC_ACTUAL_STATE` ↔ `ACTUAL_SYNC`
- `MANUAL_REVIEW`/`SUPPRESSED`는 Worker에 도달할 수 없다.
- action에 맞는 injected port 하나만 호출하고 validated result를 idempotent result store에 기록한다.

C가 소비하는 D port는 다음 의미다.

- Patch port: stored context/decision으로 snapshot-bound `RemediationPatch`를 만든다.
- Sync port: 새 patch 없이 current snapshot commit을 later Plan input인 `RemediationSyncTarget`으로 준비한다.

C는 GitHub API, Terraform CLI/Plan/Apply 또는 customer workload write를 구현하지 않는다.

### 6. Queue와 runtime 배선

Assessment Queue dispatcher는 `ASSESS_RESOURCE`만 허용한다. 별도 Remediation Queue dispatcher는
C의 두 command만 허용한다. Queue별 allow-list를 느슨하게 합치지 않는다.

이번 결정은 API/service/repository/Worker의 mockable code boundary를 완성한다. D의 live Patch/Sync
adapter, GitHub/Terraform 실행, C Remediation Lambda의 customer runtime composition과 CloudFormation
event-source 배선은 이 ADR에서 구현 완료로 주장하지 않는다. 이 배선은 D port와 customer-approved
runtime identity가 준비된 뒤 통합한다.

## Consequences

- 예외가 있는데 Patch가 생성되는 이중 판정 경로가 사라진다.
- A/B/C 흐름은 D live adapter 없이 fixture/mock으로 통합 테스트할 수 있다.
- Worker 재시도는 Queue payload가 아닌 같은 immutable decision/context와 revision을 읽는다.
- C Agent orchestration과 D deterministic integration 책임이 분리된다.
- `SYNC_ACTUAL_STATE`가 remediation 준비와 deployment execution을 명확히 분리한다.
- 실제 M2 Exit criteria의 Branch/Commit/PR/Plan은 D live adapter와 runtime 배선이 끝날 때까지 미완료다.

## Rejected alternatives

- **D가 Remediation Worker를 소유:** 팀의 Agent 소유권 결정과 충돌하고 policy/context orchestration을
  integration adapter 소유권과 섞으므로 거부한다.
- **C `RemediationStrategy`를 decision과 함께 유지:** 같은 action이 두 값에 존재해 재불일치가
  가능하므로 거부한다.
- **`ACTUAL_SYNC`에 `RUN_DEPLOYMENT` 재사용:** C remediation 준비가 Human Approval 이전 D
  deployment 실행으로 보이게 하므로 거부한다.
- **decision을 Queue에 포함:** durable authoritative state와 최소 payload 원칙(ADR-0013)을
  위반하므로 거부한다.

## Final record

이 ADR의 소유권·command·single-source 결정은 확정됐다. 자동 batch remediation은 M2 범위 밖이며,
단건 사용자 트리거만 지원한다. D live adapter나 외부 AWS/GitHub/Terraform E2E를 이 결정의 완료
조건으로 포함하지 않는다.

## 보완 2026-09-05 — 조치 prompt의 `remediation_guidance` (ADR-0024 §G)

C Remediation Agent의 prompt는 Finding의 `rationale` 외에 Control 설명, 승인된 Rule의
`evaluation_rubric`(고객 partition에서 읽을 수 있을 때), IaC hint, 그리고 plan 검사가 읽을
attribute·기대값(`plan_checks`)을 싣는다. 결정적 판정의 rationale은 AWS API 투영 경로라서 모델이
그것을 Terraform attribute로 혼자 번역해야 했다 — 매핑은 Catalog에 있다. 경계는 그대로다: 모델은
여전히 바뀔 파일과 내용만 정하고, 변경이 검사를 만족하는지는 readiness의 코드가 판정한다.
