# PRD — Cloud Governance & Compliance Agent

## Product definition

Terraform 기반 IaC와 실제 AWS 상태, 사내 정책 및 ISMS-P 요구사항을 함께 평가하고, Finding에서 Terraform Remediation, PR, Human Approval, Apply, 재평가까지 연결하는 고객 계정 배포형 플랫폼이다.

MVP의 가치는 Rule 수가 아니라 실제 AWS 환경에서 `Assessment → Finding → Remediation → Apply → Re-Assessment` 폐루프를 완주하고, AI 평가의 정확성·일관성·근거 추적성을 검증하는 데 있다.

## Goals and principles

- Customer-Deployed: Governance Platform은 고객 AWS Account에 배포한다.
- AWS + Terraform에 집중한다.
- AI는 허용된 Governance Boundary 안에서 Rule, Evidence, 판정, Severity, 점수를 선택한다.
- 명시적 UI/API 요청은 해당 기능 Workflow로 직접 진입한다. 자연어 요청만 Parent
  Orchestrator Agent가 의도·후보 Scope를 해석한다. Parent는 Policy Q&A를 직접
  처리하고, Assessment, Remediation, Deployment Workflow 중 하나로 라우팅한다.
- Parent와 각 Workflow는 역할별 Golden Dataset 평가로 승인된 Model Profile을 사용한다.
  Model Profile 변경은 재평가와 승인 없이는 적용하지 않는다.
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
→ Initial Assessment (IaC compliance / Actual compliance / Drift)
→ Finding / Evidence / Readiness Score / Coverage
→ Terraform Remediation → PR → plan
→ Human Approval → GitHub Actions apply → Re-Assessment
```

명시적 버튼/API는 위 Workflow의 시작점을 직접 선택한다. 자연어 입력은 Parent
Orchestrator Agent가 의도를 해석하고 필요한 Repository, AWS Account, Policy Profile,
Resource Scope를 수집한 뒤 적절한 Workflow를 제안한다. Assessment, Remediation,
Deployment처럼 비용·상태·변경을 유발할 수 있는 실행은 Backend의 scope 검증과 사용자
확인 뒤에만 시작한다.

## Evaluation model

- 평가 단위: `Resource × Rule`
- 상태: `PASS`, `FAIL`, `MANUAL_REVIEW`, `INSUFFICIENT_EVIDENCE`, `OUT_OF_SCOPE`, `EXECUTION_ERROR`
- 점수: 기본적으로 0–100 연속 점수를 생성한다. 반복 실행 편차가 큰 경우에만 `{0, 15, 30, 50, 70, 85, 100}` Anchor 집합을 도입한다. Anchor 값의 의미와 Rule/Source Rubric은 도입 전에 고정한다.
- 서비스의 점수는 공식 인증 점수나 합격 가능성이 아닌 `ISMS-P Readiness Score`다. Coverage는 AI가 아니라 Code가 기계적으로 계산해 점수와 함께 표시한다.
- Evidence에는 정책 원문 위치 또는 content hash를 참조해 추적성을 유지한다.

## Assessment stages

- **Initial Assessment**: 같은 Terraform 관리 대상에 대해 IaC Compliance, AWS Actual
  Compliance, IaC–Actual Drift를 구분해 평가한다. 각 결과는 같은 `Resource × Rule`의
  `IAC`, `AWS_ACTUAL`, `DRIFT` 관점과 Evidence를 가진다.
- **Remediation**: IaC가 수정돼야 하는 정책 위반 또는 Drift에는 원하는 안전한 상태를
  확정하는 Terraform Patch와 PR을 만든다. IaC가 이미 안전하고 Actual만 drift된 경우에는
  Patch 없이 현재 IaC commit을 배포 대상으로 삼아 Actual을 동기화한다. IaC에 매핑되지
  않는 리소스나 안전한 조치를 만들 수 없는 경우는 `MANUAL_REVIEW`로 처리한다.
- **Deployment Readiness Validation**: 수정 IaC와 refresh된 Terraform Plan으로 현재 AWS
  상태에 안전하게 적용 가능한지 검증한다. 이 단계는 drift를 다시 감지해 배포를 막거나
  재수정을 요구할 수 있지만, 직접 변경하지는 않는다.
- **Post-Deploy Verification**: 승인된 Apply 뒤 실제 AWS에 수정이 반영됐는지 확인하고
  Actual Compliance와 Drift를 재평가한다.

이 단계는 Git Branch인 `dev`와 `main`의 의미와 분리된다.

## Model Profile and natural-language orchestration

Parent(Policy Q&A 포함), Assessment, Remediation, Deployment는 같은 모델을 공유해야 할
의무가 없다. 각 역할은 Golden Dataset과 반복 평가를 통해 정확성, Evidence
Reference 정확도, 일관성, 지연·비용을 비교해 승인된 Model Profile을 사용한다. Profile은
Model/Prompt/Rubric Version과 평가 근거를 고정하고, 실행 결과에는 사용한 Profile 정보를
기록한다. Parent는 자연어 요청의 Workflow 선택만 담당하며, 권한 판정·Job 생성·Apply
승인 권한을 갖지 않는다.

## Success criteria

- Golden Dataset과 반복 실행으로 Correctness, Evidence Reference Accuracy, Self-Agreement, Invariance, Sensitivity, 과대평가 여부를 검증한다. 초기 목표는 명확한 PASS/FAIL 정확도, Evidence Reference 정확도, 동일 Case 판정 일치율 각각 90% 이상 및 Score 반복 편차 ±10점 이내다.
- Prompt, Model, Rubric, Rule 버전을 결과와 함께 기록해 회귀 원인을 추적한다.
- 고객 경계 밖 데이터 접근 및 승인 없는 인프라 변경이 없어야 한다.
