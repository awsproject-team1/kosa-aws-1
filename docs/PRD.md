# PRD — Cloud Governance & Compliance Agent

## Product definition

Terraform 기반 IaC와 실제 AWS 상태, 사내 정책 및 ISMS-P 요구사항을 함께 평가하고, Finding에서 Terraform Remediation, PR, Human Approval, Apply, 재평가까지 연결하는 고객 계정 배포형 플랫폼이다.

MVP의 가치는 Rule 수가 아니라 실제 AWS 환경에서 `Assessment → Finding → Remediation → Apply → Re-Assessment` 폐루프를 완주하고, AI 평가의 정확성·일관성·근거 추적성을 검증하는 데 있다.

## Goals and principles

- Customer-Deployed: Governance Platform은 고객 AWS Account에 배포한다.
- AWS + Terraform에 집중한다.
- AI는 허용된 Governance Boundary 안에서 Rule, Evidence, 판정, Severity, 점수를 선택한다.
- Code는 권한·범위·스키마·Evidence 검증·Coverage 계산을 담당한다.
- 고객 워크로드 변경은 Human Approval 뒤 GitHub Actions가 수행한다.
- MVP에서는 RAG, Vector DB, Bedrock Knowledge Base를 사용하지 않는다.

## Target scope

- 대상 리소스: EC2, RDS, ALB, S3
- 데모: Terraform으로 구성한 WordPress/LAMP 웹 서비스
- 사용자: `Admin`, `User`
- 화면: Login, Policy/Rule/Profile, Assessment, Finding/Report, Remediation Diff, PR/CI/Plan, Approval/Deployment, Audit

## Development roles

MVP는 A(Platform/Backend), B(Policy/Governance Boundary), C(AI Evaluation), D(Remediation/GitHub/Deployment) 역할로 나눠 개발한다. 역할별 상세 책임과 Repository 경계는 `docs/DESIGN.md`를 정본으로 한다. Contracts, Integration Test, C4/ADR, E2E는 공동 책임이다.

## Core workflow

```text
Policy / ISMS-P + Customer IaC + AWS Actual
→ AI Assessment → Finding / Evidence / Readiness Score / Coverage
→ Terraform Remediation → PR → plan
→ Human Approval → GitHub Actions apply → Re-Assessment
```

## Evaluation model

- 평가 단위: `Resource × Rule`
- 상태: `PASS`, `FAIL`, `MANUAL_REVIEW`, `INSUFFICIENT_EVIDENCE`, `OUT_OF_SCOPE`, `EXECUTION_ERROR`
- 점수: 기본적으로 0–100 연속 점수를 생성한다. 반복 실행 편차가 큰 경우에만 `{0, 15, 30, 50, 70, 85, 100}` Anchor 집합을 도입한다. Anchor 값의 의미와 Rule/Source Rubric은 도입 전에 고정한다.
- 서비스의 점수는 공식 인증 점수나 합격 가능성이 아닌 `ISMS-P Readiness Score`다. Coverage는 AI가 아니라 Code가 기계적으로 계산해 점수와 함께 표시한다.
- Evidence에는 정책 원문 위치 또는 content hash를 참조해 추적성을 유지한다.

## Assessment stages

- **Initial Assessment**: 현재 IaC와 실제 AWS 상태의 Compliance를 평가한다.
- **Deployment Readiness Validation**: 수정안이 배포 가능한지 검증한다.
- **Post-Deploy Verification**: 실제 AWS에 수정이 반영됐는지 확인하고 재평가한다.

이 단계는 Git Branch인 `dev`와 `main`의 의미와 분리된다.

## Success criteria

- Golden Dataset과 반복 실행으로 Correctness, Evidence Reference Accuracy, Self-Agreement, Invariance, Sensitivity, 과대평가 여부를 검증한다. 초기 목표는 명확한 PASS/FAIL 정확도, Evidence Reference 정확도, 동일 Case 판정 일치율 각각 90% 이상 및 Score 반복 편차 ±10점 이내다.
- Prompt, Model, Rubric, Rule 버전을 결과와 함께 기록해 회귀 원인을 추적한다.
- 고객 경계 밖 데이터 접근 및 승인 없는 인프라 변경이 없어야 한다.
