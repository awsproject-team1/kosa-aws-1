# fixtures/terraform (의도적으로 비어 있음)

WordPress/LAMP 데모 Terraform은 이 플랫폼 저장소에 두지 않는다. ADR-0021 §1에 따라 팀이 소유한
**별도 고객 sandbox repository**에 두고, 이 저장소에는 참조와 시나리오만 남긴다. 그래야 apply
경로가 실제 GitHub App / OIDC / 승인된 Repository 경계(ADR-0007, ADR-0019 §6)를 통과하는 것으로
검증된다.

- 데모 IaC 위치·위반 토글·전제조건: `docs/M4-DEMO-IAC-REFERENCE.md`
- 폐루프 실행 절차와 관측·비용 기록: `docs/M4-DEMO-RUNBOOK.md`
- 고객이 설치하는 plan/apply workflow template: `ci/terraform/`

`.gitkeep`은 디렉터리 유지를 위한 것이며 데모 IaC seed가 아니다.
