# ADR-0027 — 평가 대상 revision은 배포 시점의 고정 commit이 아니라 branch HEAD를 따른다

- 상태: 채택 (2026-09-05)
- 관련: ADR-0007(승인된 배포 경계), ADR-0017(Remediation 범위), ADR-0020(배포 후 검증)

## 맥락

M1 runtime configuration(`M1_ASSESSMENT_RUNTIME_JSON`)은 승인된 customer/repository마다 **하나의
정확한 Git commit**(`commit_sha`)을 고정했다. 의도는 "무엇을 읽는가"를 배포 승인에 묶는 것이었다.

라이브(2026-09-05)에서 그 의도가 사용자에게 보인 모습은 이랬다: 위반을 조치해 PR을 병합하고 평가를
다시 돌려도 **같은 FAIL**이 나왔다. 평가가 main의 최신 commit이 아니라 배포 때 넣은 옛 commit을
계속 읽었기 때문이다. 팀원이 Worker 환경변수의 commit을 손으로 최신으로 바꿔 확인했지만, 그 값은
secret에서 오므로 다음 배포(같은 날 06:49)가 되돌렸다. 고정 commit은 "저장소가 진행되는 동안의
평가"라는 제품의 기본 시나리오와 맞지 않는다.

## 결정

1. target은 revision을 **정확히 하나**로 선언한다: `commit_sha`(고정) 또는 `branch`(동적).
   둘 다면 "어느 commit을 읽는가"에 답이 두 개고, 둘 다 없으면 답이 없다 — 배포 gate와 runtime
   config 둘 다 거부한다.
2. `branch`면 Worker가 **Assessment 시작 시 그 HEAD를 한 번** 읽는다(`GET /repos/{repo}/commits/{branch}`
   → 40자 SHA). 그 값이 그 Assessment의 모든 리소스 작업과 모든 결과의 `assessed_commit_sha`가 되고,
   IaC 읽기는 전부 그 commit에 고정된다. 한 Assessment 안에서 HEAD가 움직여도 리소스마다 다른
   commit을 읽지 않는다.
3. 해석은 `DynamoM1WorkRepository`가 work를 만들 때 한 번 하고 works 캐시와 함께 Job·revision에
   묶인다. 해석기가 없거나 SHA가 아닌 값을 돌려주면 실패다(fail-closed). 고정 `commit_sha`
   target은 해석기를 부르지 않는다.
4. branch 이름은 `git check-ref-format --branch`의 규칙 중 API 경로에 필요한 것만 검사하고,
   40자 16진수는 branch로 받지 않는다 — 두 필드의 뜻이 섞이지 않게.
5. 문서와 sandbox 설정은 `branch: main`을 기본으로 한다. `commit_sha`는 감사·재현이 목적인
   배포를 위해 남는다.

## 영향

- Remediation은 finding의 `assessed_commit_sha`를 base로 patch를 만들므로(ADR-0017) 자동으로
  평가 시점 HEAD를 base로 삼는다. 옛 commit 위에 만든 PR이 "main과 사이에 commit 없음"으로
  거부되던 사례가 줄어든다.
- 배포 후 검증(ADR-0020)은 검증 시점의 HEAD를 읽는다 — 병합된 수정이 실제로 반영됐는지 보는 것이
  검증의 목적이므로 이것이 맞다.
- 운영자는 secret `M1_ASSESSMENT_RUNTIME_JSON`에서 `commit_sha`를 지우고 `"branch": "main"`을
  넣은 뒤 재배포해야 한다. 코드만 배포하면 기존 고정 commit이 그대로 유효하게 유지된다.
