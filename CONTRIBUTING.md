# Contributing

## Branch and PR

- `main`은 안정 통합·릴리스 기준이며 직접 push하지 않는다.
- `dev`는 개발 통합 브랜치다.
- 작업 브랜치는 최신 `dev`에서 `feature/`, `fix/`, `docs/`, `refactor/`, `test/`, `chore/` 접두사와 kebab-case 이름으로 만든다.
- 일반 개발 PR의 base는 `dev`다. 모든 기능 완료 뒤에만 사람이 `dev → main` 통합 PR을 한 번 만든다.
- 최소 1명의 승인과 필수 CI 통과 후 병합한다.

## Commit and quality

- Conventional Commits를 사용한다 (`feat`, `fix`, `docs`, `refactor`, `test`, `chore`).
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
- Contract 변경은 작성자와 해당 Producer/Consumer Owner의 검토가 필요하다.
- Task Done은 Review, 필수 CI, `dev` Merge, 필요한 Test·문서 갱신, `PROGRESS.md` 갱신을 모두 만족해야 한다. Milestone Done은 관련 Task와 `dev` 통합 검증 완료, Final Release/Demo Done은 E2E·Release 검증 및 `dev → main` PR Merge까지를 뜻한다.
- Architecture 변경은 `docs/DESIGN.md`와 필요한 ADR, API 변경은 `docs/API.md`, Schema 변경은 `docs/CONTRACTS.md`를 같은 PR에서 갱신한다.
- Open Decision은 Decision, Owner, Needed by, Blocks, Proposed options, Final record를 남긴다.
