# Contributing

## Branch and PR

- `main`은 안정 통합·릴리스 기준이며 직접 push하지 않는다.
- `dev`는 개발 통합 브랜치다.
- 작업 브랜치는 최신 `dev`에서 `feature/`, `fix/`, `docs/`, `refactor/`, `test/`, `chore/` 접두사와 kebab-case 이름으로 만든다.
- 일반 개발 PR의 base는 `dev`다. 모든 기능 완료 뒤에만 사람이 `dev → main` 통합 PR을 한 번 만든다.
- 최소 1명의 승인과 필수 CI 통과 후 병합한다.
- 일정상 마일스톤 통합 PR을 사용할 때는 적용 순서와 종료 시점을 루트 `PROGRESS.md`에 먼저 기록한다.
  각 마일스톤 브랜치는 이전 마일스톤 PR이 `dev`에 병합된 뒤 최신 `dev`에서 새로 만들며, 일반
  Review·CI·Done 기준을 그대로 적용한다.

## Commit and quality

- Conventional Commits를 사용한다 (`feat`, `fix`, `docs`, `refactor`, `test`, `chore`).
- 통합 PR 안에서도 기능·Contract·문서·검증 등 하나의 검토 가능한 관심사마다 커밋을 나누고,
  각 커밋은 독립적으로 설명하고 되돌릴 수 있게 유지한다.
- `PROGRESS.md`가 세부 커밋 이력 보존을 요구하는 마일스톤 PR은 squash하지 않고 merge commit으로
  병합한다. 이는 플랫폼 저장소의 PR 이력 규칙이며 ADR-0019의 고객 Repository apply 대상 merge
  commit 불변식과는 별개다.
- PR에는 범위, 변경 이유, 검증 결과, Contract/Architecture 영향을 기록한다.
- Secret, Access Key, Token은 커밋하지 않는다.
- GitHub Issue/Project 및 Issue 번호 연결은 사용하지 않는다.

## Documentation and tasks

- 제품·설계 정본은 각각 `docs/PRD.md`, `docs/DESIGN.md`다.
- `.ai/`는 프로젝트를 처음 설정할 때 각 개발자/Agent가 로컬에 한 번 생성하는 Git 제외 디렉터리다. `task/`, `PROGRESS.md`, `HANDOFF.md`, `PLAN.md`를 둔다.
- 루트 `PROGRESS.md`는 Git 공유 팀 진행·의존성·차단 사항·마일스톤의 정본이다. `.ai/PROGRESS.md`는 개인 세부 진행·검증·다음 세션 메모로 공유하지 않는다.
- 기능마다 로컬 `.ai/task/taskN.md`를 새로 만들고 기존 task를 덮어쓰지 않는다. 완료 시 공유할 핵심 결과만 루트 `PROGRESS.md`에 요약한다.
- 지속적인 아키텍처·계약 결정은 `docs/decisions/` ADR로 남긴다.

## Dependencies, completion, and freshness

- 의존성은 `Blocked`, `Mockable`, `Integrated`로 기록한다. Mockable 작업은 확정 Contract와 Fixture/Mock으로 먼저 구현할 수 있으며, 실제 연결은 Contract가 `dev`에 Merge된 뒤 수행한다.
- 단, `PROGRESS.md`에 한시적 통합 PR 계획이 선언된 경우 같은 PR에서 Contract와 실제 연결을
  순차 진행할 수 있다. 이때 Contract 커밋은 의존 구현 커밋 전에 Producer/Consumer Owner의 승인을
  받아 동결하고, 구현 완료 뒤 전체 PR을 다시 Review하고 필수 CI를 재실행한다. PR 내부 승인은
  `dev` Merge나 Task Done을 대신하지 않는다.
- `Proposed` ADR이 차단하는 구현은 위 예외로 우회하지 않는다. 필요한 Owner 승인과 `Accepted`
  상태 변경을 먼저 완료한 뒤 해당 구현 커밋을 시작한다.
- Contract 변경은 작성자와 해당 Producer/Consumer Owner의 검토가 필요하다.
- Task Done은 Review, 필수 CI, `dev` Merge, 필요한 Test·문서 갱신, `PROGRESS.md` 갱신을 모두 만족해야 한다. Milestone Done은 관련 Task와 `dev` 통합 검증 완료, Final Release/Demo Done은 E2E·Release 검증 및 `dev → main` PR Merge까지를 뜻한다.
- Architecture 변경은 `docs/DESIGN.md`와 필요한 ADR, API 변경은 `docs/API.md`, Schema 변경은 `docs/CONTRACTS.md`를 같은 PR에서 갱신한다.
- Open Decision은 Decision, Owner, Needed by, Blocks, Proposed options, Final record를 남긴다.

## Release gate (`dev → main`)

아래 목록은 ADR-0021이 확정한 릴리스 게이트다.

- [ ] 데모 폐루프 E2E 실행 기록 (Assessment → Finding → Remediation → PR → plan → 승인 → apply →
      Post-Deploy Verification)
- [ ] Golden Dataset 반복 평가 리포트와 목표(정확도·Evidence·일치율 90% 이상, Score 편차 ±10점)
      대비 결과. 세 관점 중 측정하지 못한 관점이 있으면 통과 근거로 쓰지 않는다
- [ ] 관측·비용 기록: `EXECUTION_ERROR` 0건, DLQ depth 0, Queue age 최대값, checkpoint 재개 횟수,
      plan/apply 실패 0건, 승인 없는 apply 0건, 역할별 Bedrock 호출·토큰·p95 지연, 데모 1회 비용
- [ ] Secret scan과 Python/Frontend/Terraform 검증 결과
- [ ] 문서 Freshness: `docs/PRD.md`, `docs/DESIGN.md`, `docs/API.md`, `docs/CONTRACTS.md`,
      `docs/DATABASE.md`, `docs/architecture/`, `docs/decisions/`가 구현과 일치하고 `Proposed` ADR이
      남아 있지 않음
- [ ] `PROGRESS.md`의 M0–M3 Exit criteria 충족 상태

품질 목표 미달 시 목표를 낮추지 않는다. rubric/prompt/Golden Case를 재고정해 재실행하거나
ADR-0003 절차로 Anchor 전환을 결정한다.
