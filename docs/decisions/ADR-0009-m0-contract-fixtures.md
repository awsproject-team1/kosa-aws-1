# ADR-0009: M0 contract fixtures and deployment binding

## Context

M0 역할 B/C/D는 구현 순서가 다르지만 Policy Context, AI Evaluation, Remediation과
Deployment가 같은 boundary를 사용해야 한다. 구현 branch에 직접 의존하면 병렬 개발과
contract review가 어려워진다.

## Decision

`packages/contracts/`에 Policy Source/Rule/Profile/Source Reference, Golden Dataset Case,
Artifact/IaC Snapshot/Patch/Read-Only AWS Query/Terraform Plan/Approval을 immutable runtime
contract로 둔다. `fixtures/m0/`의 S3 public access Case와 Terraform remediation Case를
Producer/Consumer의 결정적 mock으로 사용한다.

Policy Profile은 Rule allow-list만 표현한다. Customer, Repository, AWS Account의 접근 권한은
Profile 또는 AI input에 위임하지 않고 Backend JWT scope가 검증한다. Read-Only AWS Tool은
두 read operation만 가지며, Apply는 Approval의 deployment ID, `commit_sha`, `plan_hash`가
Plan과 모두 일치할 때 GitHub Actions OIDC에서만 실행한다.

## Consequences

각 역할은 M0 fixture와 contract test만으로 병렬 구현을 시작할 수 있다. 새 Tool operation,
Artifact Type, Rule field 또는 Approval binding 변경은 Contract producer와 consumer review,
fixture 갱신, contract test를 요구한다.
