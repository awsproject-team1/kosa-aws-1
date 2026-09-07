# Cloud Governance & Compliance Agent

> AWS와 Terraform 환경을 정책·ISMS-P 기준으로 평가하고, **Finding → Remediation → 승인된 Apply → 재평가**까지 폐루프로 연결하는 Customer-Deployed 거버넌스 플랫폼

<p>
  <img alt="status" src="https://img.shields.io/badge/status-MVP-blue">
  <img alt="python" src="https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white">
  <img alt="react" src="https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black">
  <img alt="aws" src="https://img.shields.io/badge/AWS-Serverless-FF9900?logo=amazonaws&logoColor=white">
  <img alt="terraform" src="https://img.shields.io/badge/Terraform-IaC-7B42BC?logo=terraform&logoColor=white">
  <img alt="license" src="https://img.shields.io/badge/license-Internal-lightgrey">
</p>

---

## 이 프로젝트는 무엇인가

Terraform 기반 IaC, 실제 AWS 상태, 그리고 사내 정책·ISMS-P 요구사항을 **함께** 평가하는 플랫폼입니다. 단순히 규칙을 나열하는 것이 아니라, 실제 AWS 환경에서 아래 폐루프를 완주하는 것을 목표로 합니다.

```text
Policy / ISMS-P + Customer IaC + AWS Actual
   → Initial Assessment (IaC 준수 / Actual 준수 / Drift)
   → Finding / Evidence / Readiness Score / Coverage
   → Terraform Remediation → PR → plan
   → Human Approval → GitHub Actions apply → Re-Assessment
```

핵심 가치는 **AI 평가의 정확성·일관성·근거 추적성**을 검증하면서, 사람의 승인 없이는 어떤 인프라 변경도 일어나지 않는 안전한 경계를 지키는 데 있습니다.

### 설계 원칙

- **Customer-Deployed** — 플랫폼은 고객 AWS 계정(`us-east-1`)에 배포됩니다.
- **AWS + Terraform 집중** — 대상 리소스는 EC2, RDS, ALB, S3.
- **AI는 경계 안에서만 판단** — 적용할 Rule은 승인된 Profile이, Severity는 승인된 Rule이 정합니다. AI는 허용된 범위 안에서 판정(status), 0–100 점수, Rationale, 허용된 Evidence 부분집합만 선택합니다.
- **사실은 코드가, 판단만 모델이** — 권한·범위·스키마·Evidence 검증·Coverage 계산은 결정적 코드가 담당합니다.
- **사람 승인이 있어야 변경** — 고객 워크로드 변경은 Human Approval 뒤 GitHub Actions가 수행합니다.
- **MVP 범위** — RAG, Vector DB, Bedrock Knowledge Base는 사용하지 않습니다.

---

## 아키텍처 개요

```text
React SPA (S3 + CloudFront)
  → Cognito (JWT)
  → API Gateway
      ├─ Parent Router Lambda      (자연어 라우팅 + Policy Q&A, 동기 max 30s)
      ├─ Assessment API  → SQS → Assessment Worker   (LangGraph subgraph)
      ├─ Remediation API → SQS → Remediation Worker  (LangGraph subgraph)
      └─ Deployment API  → SQS → Deployment Worker    (LangGraph subgraph)

AI / Tools : Bedrock 역할별 Model Profile, Policy Context, AWS Resource Tool(RO), GitHub App
State      : DynamoDB (Job / checkpoint / metadata)
Artifact   : S3 (policy 원본 / IaC·AWS snapshot / report / patch / plan)
Deploy     : GitHub App + GitHub Actions(OIDC) CI / plan / approved apply
             → EventBridge → Deployment Queue
```

- **명시적 UI/API 요청**은 해당 Workflow로 직접 진입합니다.
- **자연어 요청**만 Parent Orchestrator가 의도·후보 Scope를 해석해 적절한 Workflow로 라우팅합니다.
- 상태의 정본은 DynamoDB와 S3이며, SQS는 전달 수단입니다.

자세한 내용은 [기술 설계 문서](docs/DESIGN.md)를 참고하세요.

---

## 기술 스택

| 영역 | 사용 기술 |
| --- | --- |
| Frontend | React 19, TypeScript, Vite |
| Backend | Python 3.12+, AWS Lambda |
| AI / Orchestration | Amazon Bedrock, LangGraph |
| Storage | DynamoDB, Amazon S3 |
| Auth / Edge | Cognito, API Gateway, CloudFront |
| Async | SQS (+ DLQ), EventBridge |
| IaC / Deploy | Terraform, CloudFormation, GitHub Actions (OIDC) |
| Quality | Ruff, unittest, cfn-lint, TFLint, Checkov |

---

## 평가 모델

- **평가 단위**: `Resource × Rule`
- **관점**: `IAC`, `AWS_ACTUAL`, `DRIFT`
- **상태**: `PASS`, `FAIL`, `MANUAL_REVIEW`, `INSUFFICIENT_EVIDENCE`, `OUT_OF_SCOPE`, `EXECUTION_ERROR`
- **점수**: 기본 0–100 연속 점수. 사실이 명확한 항목은 코드가 결정적으로 판정하고, 해석이 필요한 항목만 모델이 판단합니다.
- **Readiness Score**: 공식 인증 점수가 아닌 `ISMS-P Readiness Score`이며, Coverage는 AI가 아니라 코드가 기계적으로 계산합니다.
- Evidence는 정책 원문 위치 또는 content hash를 참조해 추적성을 유지합니다.

---

## 프로젝트 구조

```text
kosa-aws-1/
├── apps/
│   ├── backend/        # 기능별 Lambda (auth, jobs, assessment, policy, remediation, deployment)
│   └── frontend/       # React SPA 콘솔
├── agent/              # LangGraph Parent Orchestrator / subgraph
├── packages/           # 공용 Contract, 도메인 스키마
├── infrastructure/     # CloudFormation, IAM, 파라미터
├── ci/                 # Terraform plan/apply template
├── fixtures/           # 개발용 seed (rules 등)
├── docs/               # PRD, DESIGN, API, CONTRACTS, DATABASE, ADR
├── tests/              # unit / contract / integration / security
└── scripts/            # 운영·측정 스크립트
```

---

## 시작하기

### 사전 요구사항

- Python 3.12 이상
- Node.js 20 이상 (Frontend)

### Backend (Python) 부트스트랩

```bash
python3 -m pip install -r requirements-dev.txt -r apps/backend/requirements.txt

# M0 검증
python3 -m unittest discover --start-directory tests/unit --pattern 'test_*.py' --verbose
python3 -m unittest discover --start-directory tests/contract --pattern 'test_*.py' --verbose
python3 -m unittest discover --start-directory tests/security --pattern 'test_*.py' --verbose

# 린트
python3 -m ruff check .
python3 -m ruff format --check .
```

### Frontend

```bash
cd apps/frontend
npm install
npm run dev      # 개발 서버
npm run build    # 프로덕션 빌드
```

환경 변수는 [`.env.example`](.env.example)을 참고하세요. 실제 값(토큰·시크릿)은 커밋하지 않습니다.

---

## 문서

| 문서 | 내용 |
| --- | --- |
| [PRD](docs/PRD.md) | 제품 가치, 사용자, MVP 범위, 평가 의미 |
| [DESIGN](docs/DESIGN.md) | 아키텍처, AWS, 보안, 워크플로, 관측성 |
| [API](docs/API.md) | HTTP API와 오류 형식 |
| [CONTRACTS](docs/CONTRACTS.md) | 도메인 / structured-output 스키마 |
| [DATABASE](docs/DATABASE.md) | DynamoDB / S3 Artifact 모델과 접근 패턴 |
| [decisions/](docs/decisions/) | 장기 기술 결정(ADR)과 이유 |
| [CONTRIBUTING](CONTRIBUTING.md) | 브랜치, PR, 리뷰, Done, 문서 Freshness |
| [PROGRESS](PROGRESS.md) | 팀 진행 현황·의존성·차단 사항 |

---

## 개발 규칙

- 작업 브랜치는 최신 `dev`에서 만들고, 일반 PR의 base는 `dev`입니다.
- `main` 직접 push와 개발 중 `main` 대상 PR은 금지합니다.
- 정책 원문은 저장소에 커밋하지 않습니다. 저장소에는 Rule 정의와 `SourceReference` locator만 둡니다.
- AWS Resource Tool은 읽기 전용입니다. 실제 인프라 변경은 승인된 `commit_sha`·`plan_hash` 검증 뒤 Human Approval과 GitHub Actions를 통해서만 수행합니다.
- 역할 경계를 넘는 API·Schema 변경은 해당 Contract의 Producer/Consumer Owner가 검토합니다.

### 개발 역할

| 역할 | 책임 |
| --- | --- |
| **A — Platform/Backend** | 플랫폼 기반, 사용자, API, durable workflow state |
| **B — Policy/Governance Boundary** | 정책 문서 형식, Rule Registry/Profile/Context, remediation 허용 범위 |
| **C — AI Evaluation & Agent Orchestration** | Assessment/Remediation Agent, 평가 품질·evidence orchestration |
| **D — Integration & Deployment Execution** | GitHub/AWS Tool, Terraform Plan/Apply, Deployment Worker |
| **Shared** | Contracts, Integration Test, C4/ADR, E2E |

---

## 보안 경계

- AI와 Tool은 Customer / AWS Account / Repository / Policy Profile Scope 밖으로 접근할 수 없습니다.
- 고객 워크로드 변경은 사람 승인 뒤에만, GitHub Actions를 통해서만 이뤄집니다.
- 민감한 Prompt·정책 원문·IaC 전체는 로그에서 마스킹하거나 제외합니다.
- 모든 PR은 secret scan과 source gate를 통과해야 합니다.
