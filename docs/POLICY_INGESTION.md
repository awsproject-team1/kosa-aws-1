# Customer Policy Ingestion

> Status: Implemented for the current M1 boundary — 형식 allow-list, 정규화 Schema와 5개 Parser,
> customer-scoped upload/finalize/status 저장, public Lambda route, 비동기 후보 추출 Worker,
> approval/profile DynamoDB 경로와 Assessment profile pin이 배선돼 있다. 관리자 콘솔은 완결된
> 후보 결과를 끝 페이지까지 읽고 `CandidateReviewEntry`의 Rule·평가·근거 형식을 표시한다.
>
> Boundary: “임의 문서”는 아래 allow-list의 지원 형식과 현재 Governance Control Catalog 범위 안을
> 뜻한다. `policies-local/`과 `fixtures/rules/`는 개발 seed일 뿐 고객 업로드를 대체하지 않는다.
>
> Decision record: `docs/decisions/ADR-0015-customer-policy-ingestion.md`

## Goal

사용자가 사내 정책 원문을 업로드하면 고객 Scope 안에서 원본을 보존하고, 파일 형식별 추출 결과를
공통 문서 구조로 정규화한 뒤, 검토·승인된 Control/Rule/Profile만 Policy Context에 제공한다.
현재 `policies-local/`과 `fixtures/rules/`는 이 흐름을 개발하기 위한 seed 자료이며 운영 입력이 아니다.

## Important boundary

파일을 S3에 바이트로 저장하는 것과 그 내용을 정책으로 해석하는 것은 서로 다른 기능이다.
서비스는 "모든 형식을 읽는다"고 가정하지 않는다. 지원 형식은 아래 Format policy의 allow-list가
전부이며, 목록에 없는 형식·암호화 문서·손상 문서·텍스트를 추출할 수 없는 문서는
`REVIEW_REQUIRED` 또는 `FAILED`로 처리한다. 원본 업로드 성공만으로 Policy Source를 승인하거나
Assessment에 사용해서는 안 된다.

## Target workflow

```text
Authenticated upload request
→ customer-scoped S3 original (immutable, checksum verified)
→ file signature/MIME/size/malware validation
→ format-specific parser
→ normalized Policy Document artifact
→ extraction request (queued) → Policy Authoring Worker
→ Control/Rule candidates with stable locator + content hash
→ human review and partial approval
→ version-pinned Policy Source/Rule/Profile publication
→ Assessment creation pins the Profile version
→ Policy Context → Assessment
```

후보 추출은 ADR-0023이 정한 경계를 따른다. 제품이 평가할 수 있는 범위는 code-owned Governance
Control Catalog(`apps/backend/policy/control_catalog.py`)가 정의하고, AI는 그 경계 안에서
Requirement를 제안할 뿐 판정·심각도·점수를 만들지 않는다. 자동 평가할 수 없는 요구사항은
`UNSUPPORTED`로 보존되며 승인 가능한 Rule이 되지 않는다.

원본과 정규화 결과는 Artifact로 분리한다. 평가기는 업로드 원본 전체를 임의로 읽지 않고 승인된
Profile의 Rule과 필요한 정규화 구간만 받는다. Parser, 정규화 Schema, Rule 또는 승인된 Policy
Document가 변경되면 Golden Dataset 품질 Gate를 다시 실행한다.

## Original finalization

S3 versioning만으로는 같은 key에 이후 object version이 추가되는 것을 막지 못한다. Parser, 검토,
승인이 **동일한 원본 바이트**를 본다는 보장은 다음 규칙에서 나온다.

- **Object identity는 서버가 만든다.** 업로드 세션 생성 시 Backend가 `source_id`,
  `source_version`, `artifact_id`, S3 key를 발급한다. Client는 이 값들을 제안하거나 덮어쓸 수
  없다.
- **Presigned upload URL은 1회용이고 만료된다.** 만료 시간과 허용 content-length 범위를 함께
  서명하고, 이미 finalize된 세션의 URL은 재사용을 거부한다.
- **Finalize는 checksum 검증을 통과한 정확한 S3 `version_id`를 영속화한다.** 업로드 완료 확인
  단계에서 실제 object의 checksum과 byte size를 다시 읽어 선언값과 대조하고, 통과한 그 시점의
  `version_id`를 ingestion record에 기록한다. 이후 같은 key에 다른 object version이 생겨도
  Parser는 기록된 `version_id`만 읽는다.
- **상태 전이는 그 tuple에 조건부로 묶인다.** `(source_id, source_version, artifact_id,
  s3_version_id, content_sha256)`을 ingestion record와 approval record에 immutable하게 기록하고,
  `VALIDATING → PARSING → REVIEW_REQUIRED → READY`와 승인 전이는 이 tuple이 일치할 때만 성공하는
  조건부 write로 수행한다. 값이 하나라도 다르면 전이는 실패한다.
- **승인은 검증된 그 판본에만 붙는다.** 승인 record는 위 tuple을 그대로 인용하며, 다른 판본으로
  승인을 옮겨 붙일 수 없다.

## Format policy

지원 형식은 아래 allow-list가 전부다. 목록에 없는 형식은 업로드가 성공하더라도 지원하지 않으며,
`REVIEW_REQUIRED` 또는 `FAILED`로 종료한다. 목록에 없는 형식을 처리하는 코드를 임의로 추가하지
않는다.

| 형식 | Media type | 비고 |
| --- | --- | --- |
| Markdown | `text/markdown` | heading 구조에서 locator가 직접 나온다 |
| Plain text | `text/plain` (UTF-8) | 문자 인코딩 오류를 명시적으로 처리한다 |
| CSV | `text/csv` | delimiter/인코딩 오류를 명시적으로 처리한다 |
| XLSX | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | 시트·행 단위 locator |
| DOCX | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | 문단·표 단위 locator |

이 목록은 두 가지 제약에서 나왔다.

- **런타임 의존성이 없어야 한다.** Backend는 서드파티 런타임 의존성이 없는 ZIP Lambda로 배포된다
  (`apps/backend/requirements.txt`, `scripts/package-m0-lambda.sh`). 위 형식은 모두 표준
  라이브러리(`zipfile`, `xml.etree`)만으로 처리된다. 라이브러리를 요구하는 형식을 지원하려면
  형식 결정이 아니라 배포 구조 결정이 먼저 필요하다.
- **추출기 근거가 있어야 한다.** Markdown과 XLSX는 현재 Policy Source 2건의 형식이고
  `scripts/policy_source_digest.py`에 추출기 원형이 있다. DOCX는 XLSX와 같은 OOXML zip 구조라
  같은 기법을 재사용한다.

확장자만 신뢰하지 않는다. 선언한 media type, 파일 signature로 탐지한 media type, Parser가 실제로
지원하는 형식을 함께 검증한다. 지원 형식 목록은 Backend와 Frontend가 같은 Contract를 사용해야 한다.
형식을 추가·제거하려면 이 문서와 Contract를 같은 변경에서 갱신한다.

## Normalized document contract

형식별 Parser는 최소한 다음 정보를 갖는 공통 결과를 생성해야 한다.

- `source_id`, `source_version`, 원본 `artifact_id`, S3 `version_id`와 `content_sha256`
- 원본 파일명, 선언/탐지 media type, byte size
- `parser_id`, `parser_version`, 처리 시각과 처리 상태
- 정규화 Artifact ID/hash와 추출 경고
- 문서의 section/paragraph/table/sheet 단위
- 각 단위의 stable `locator`, 정규화 text hash, 원본 위치

권장 처리 상태는 `UPLOADED`, `VALIDATING`, `PARSING`, `REVIEW_REQUIRED`, `READY`, `FAILED`,
`SUPERSEDED`다. `READY`이면서 사람이 승인한 정확한 Source version만 Rule과 Profile이 참조할 수 있다.

### Evidence identity

Rule과 Control의 `SourceReference`, 그리고 평가 결과의 Evidence는 모두 아래 canonical 형식을
사용한다. 이 형식은 `packages/contracts`의 `SourceReference.evidence_reference`가 정본이다.

```text
{source_id}@{source_version}#{locator}
```

- 모든 Rule/Control `SourceReference`는 **정확한 Source version**을 가리켜야 한다. locator와
  hash만으로는 같은 locator가 개정된 Source version과 잘못 연결될 수 있다.
- Profile publication과 평가 결과 Evidence는 승인된 `PolicySource(source_id, version)`에 교차
  검증한다. 승인되지 않은 Source, 승인된 것과 다른 Source version을 가리키는 참조는 거부한다.
- 정규화 문서가 만든 locator는 이 형식에 그대로 들어가므로, Parser가 바뀌어 locator 체계가
  달라지면 새 Source version으로 취급한다.

Locator는 파일 형식과 무관하게 추적 가능해야 한다. 지원 형식은 모두 문서 구조에서 locator가
직접 나오므로, 원문이 재조판돼도 같은 단위를 다시 가리킬 수 있다. 예시는 다음과 같다.

- `heading/access-control/item/5` — Markdown, DOCX
- `sheet/Security/row/27` — XLSX
- `table/2/row/8` — XLSX, DOCX

## Security and tenant isolation

이 절은 ADR-0014(artifact audit and tenant isolation)의 조건을 정책 원문 경로에 그대로
계승한다. prefix를 나누거나 JWT를 검증하는 것만으로는 Parser가 다른 tenant의 artifact에 접근하지
못한다고 보장할 수 없다.

- Backend는 verified JWT에서 `customer_id`를 결정하며 Client가 S3 key나 tenant key를 지정하지 않는다.
- 고객 artifact를 다루는 API와 Parser는 **trusted Job/customer context가 선택한 customer-scoped
  runtime identity만** 사용한다. caller는 customer ID, IAM role, session tag, prefix 중 무엇도
  선택할 수 없다.
- **`customers/*` pooled role은 tenant 경계가 아니므로 금지한다** (ADR-0014). 공용 role로 prefix만
  달리하는 접근은 허용하지 않는다.
- 고객 간 Artifact Get/Put 거부를 integration test로 보장한다. 고객 A의 자격으로 고객 B의 원본,
  정규화 Artifact, ingestion record, Source/Rule/Profile을 읽거나 쓸 수 없어야 한다.
- 원본은 `customers/{customer_id}/...` 경계에 암호화해 저장하고 공개 URL을 반환하지 않는다.
- 업로드 크기·개수·압축 해제 한도, zip bomb, 악성 파일, 암호화 파일을 fail-closed로 검증한다.
- Parser는 격리된 비동기 실행 환경에서 최소 권한으로 동작하고 원문·추출문을 로그에 남기지 않는다.
- Source version은 immutable하며 새 업로드는 기존 버전을 덮어쓰지 않는다.
- Rule 후보가 자동 생성돼도 사람 승인 전에는 Policy Profile이나 Assessment에 들어가지 않는다.

### C → A candidate extraction handoff

C는 protected normalized Artifact를 읽어 `PolicyCandidateExtraction`을 만든다. 이 값은 exact
`READY` `NormalizedPolicyDocument`, undecided `RuleCandidate` 목록, extractor ID/version을 묶으며
원문·정규화 text는 담지 않는다. 모든 Candidate의 `SourceReference`는 같은 source/version의
정규화 unit locator와 text hash를 정확히 인용해야 한다.

A는 이 handoff를 customer/source/version 단위로 영속화하고 `load_review()`에서 문서와 후보를,
`load_publication()`에서 후보·approval·PolicySource를 복원한다. 후보 생성/품질은 C가, DynamoDB
key·조건부 write·tenant-scoped read는 A가 소유한다. 이 handoff는 M1 policy ingestion 의존성이고
M3 plan/apply 시작 조건은 아니다.

## Ownership

| Role | Responsibility |
| --- | --- |
| A — Platform/Backend | 업로드 세션/API, presigned upload, tenant-scoped S3/DynamoDB, 검증·처리 Job, 상태 조회, 악성 파일·quota 경계 |
| B — Policy/Governance Boundary | 지원 형식 정책, 정규화 Schema, locator/hash, Policy Source/Control/Rule/Profile lifecycle 및 승인 조건 |
| C — AI Evaluation | AI 기반 Control/Rule 후보 추출이 필요한 경우의 모델·prompt·품질 Gate, 승인된 Context 소비 |
| Shared | Parser Adapter, 보안 검토, Contract/Integration/E2E Test, UI 업로드·검토 흐름 |

B가 문서 의미와 승인 경계를 소유하지만, public upload와 저장 인프라는 A와 Contract Review를 거쳐야
한다. AI가 Rule 후보를 생성하는 경우 B와 C가 함께 검토하며, C가 임의로 Profile을 활성화할 수 없다.

## Required public/API boundary

구현 전 `docs/API.md`와 `packages/contracts/`에 최소 다음 기능의 wire shape를 확정한다.

1. 고객 Scope가 고정된 Policy Source upload session 생성
2. 업로드 완료 확인과 비동기 validation/parsing 시작
3. Source version별 처리 상태·지원 불가 사유·검토 경고 조회
4. 후보 추출 요청(비동기, `202 Accepted`)과 그 결과 조회
5. 추출된 Control/Rule 후보 검토 및 승인
6. 승인된 Rule version으로 versioned Policy Profile 생성 또는 갱신(publication)

4번은 요청을 durable하게 남긴 뒤 Authoring Queue로 보낸다. 조회는 완결된 실행만 후보를 돌려준다 —
부분 결과를 보여주면 리뷰어가 그것을 전체로 착각하고 승인한다. 응답에는 정규화 문서의 원문이
들어가지 않으며, 리뷰어가 보는 문장은 모델이 쓴 재진술과 서버가 만든 `content_sha256`이다.

5번과 6번은 서로 다른 operation이다. 승인은 Source/Control/Rule version을 확정할 뿐이고, 그
Rule들을 실제 평가 경계로 만드는 것은 Profile publication이다. Profile publication은 다음을
거부해야 한다.

- 승인되지 않은 Source 또는 Rule을 참조하는 Profile
- 승인된 것과 다른 Source version을 가리키는 `SourceReference`
- 승인 record가 인용한 `(artifact_id, s3_version_id, content_sha256)`과 어긋나는 Rule

승인과 publication을 하나의 operation으로 합칠 경우에도 위 거부 조건은 그대로 적용하며, 승인과
게시를 audit record와 함께 원자적으로 수행해야 한다.

업로드 요청은 `customer_id`, bucket, object key, 처리 상태를 받을 수 없다. Backend가 이를 생성한다.

## Acceptance criteria

- [x] 지원 형식 allow-list와 파일 signature 검증이 Contract와 테스트로 고정된다.
- [x] 지원 형식 Parser가 서드파티 런타임 의존성 없이 동작한다.
- [x] XLSX Parser가 inline string(`t="inlineStr"`), 병합 셀, `xl/workbook.xml` 기반 시트 이름
      locator를 처리한다. `policy_source_digest.py`의 원형은 이 셋을 아직 다루지 않는다.
- [x] zip 기반 형식(XLSX, DOCX)은 압축 해제 크기 상한을 먼저 검사한 뒤 읽는다.
- [x] XML Parser가 DTD를 선언한 OOXML part를 파싱 전에 거부한다. zip 상한은 엔티티 확장을
      막지 못한다 — 증폭이 압축 해제 **이후** Parser 안에서 일어나므로 선언 크기도 읽은
      바이트도 작다. 정규화 unit 수 상한도 형식과 무관하게 강제한다.
- [x] 각 지원 형식이 동일한 Normalized Policy Document Contract를 생성한다.
- [x] 고객 A가 고객 B의 원문, 정규화 Artifact, Source/Rule/Profile을 조회할 수 없다.
      (모든 read가 호출자 partition만 사용하고,
      `tests/integration/test_policy_authoring_to_assessment.py`가 이를 고정한다.)
- [x] 암호화·손상·미지원·텍스트 없는 문서가 명확한 상태와 오류로 종료된다.
- [x] Source version, Parser version, locator, 원문/정규화 hash가 Evidence까지 추적된다.
      (정규화 unit → `source_reference_for()` → 승인 record의 finalization tuple →
      게시된 Profile → `PolicyContext.allows_evidence()`까지 테스트로 이어져 있다.)
- [x] 사람 승인 전 Rule은 Profile 및 Assessment Context에 들어가지 않는다.
- [x] 업로드 → 정규화 → 후보 추출 → 승인 → Profile → Assessment 통합 테스트가 통과한다.
      (`tests/integration/test_policy_authoring_to_assessment.py`. 평가되는 Rule이 커밋된
      fixture Rule이 아니라 업로드한 정책에서 나온 것임을 함께 확인한다.)
- [x] 정책 원문이나 추출 텍스트가 Git diff, Queue payload, 운영 로그에 노출되지 않는다.
      (Contract가 텍스트를 담을 수 없고, `tests/security/test_policy_ingestion_boundary.py`가
      직렬화·실패 코드·오류 메시지에 원문이 없음을 고정한다.)
- [ ] 구현 PR에서 `docs/architecture/C4-CONTAINER.md`에 업로드/Parser/정규화 Artifact 경로를
      반영한다. 계획 단계인 지금은 `docs/DESIGN.md` flow만 갱신하고 C4는 의도적으로 미룬다.
