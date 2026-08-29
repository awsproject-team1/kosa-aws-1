# Cloud Governance & Compliance Agent

AWS와 Terraform 환경을 대상으로 정책·ISMS-P 요구사항을 평가하고, Finding부터 Remediation, 승인된 Apply, 재평가까지 연결하는 Customer-Deployed 플랫폼입니다.

## 문서

- [제품 요구사항](docs/PRD.md)
- [기술 설계](docs/DESIGN.md)
- [API 계약](docs/API.md)
- [도메인 계약](docs/CONTRACTS.md)
- [데이터베이스 설계](docs/DATABASE.md)
- [협업 규칙](CONTRIBUTING.md)
- [팀 진행 현황](PROGRESS.md)

구현 전 최신 `dev`에서 작업 브랜치를 만들고, 모든 개발 PR은 `dev`를 대상으로 합니다.

## Python M0 bootstrap

Python 3.12 이상을 사용합니다. 개발 의존성을 설치한 뒤 아래 명령으로 M0 검증을 실행합니다.

```bash
python3 -m pip install -r requirements-dev.txt -r apps/backend/requirements.txt
python3 -m unittest discover --start-directory tests/unit --pattern 'test_*.py' --verbose
python3 -m unittest discover --start-directory tests/contract --pattern 'test_*.py' --verbose
python3 -m unittest discover --start-directory tests/security --pattern 'test_*.py' --verbose
```
