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

**AUTOMATABLE 후보는 다섯 필드를 모두 가져야 한다** (2026-09-04): `mapped_control_key`,
`evaluation_type`, `resource_types`, `required_evidence`, `evaluation_rubric`. `ExtractedRequirement`
계약이 이 다섯을 강제하는데 prompt는 앞의 셋만 요구하고 있었다 — 규칙에도 예시에도
`required_evidence`와 `evaluation_rubric`이 없었다. 그래서 모델이 낸 AUTOMATABLE은 전부 계약
위반으로 폐기됐고, 저장된 추출 실행 세 건이 모두 `accepted: 0`이었다(자동 평가 Rule이 하나도
없는 Profile이 게시된 직접 원인). prompt가 다섯 필드를 모두 요구하도록 고쳤고 `PROMPT_VERSION`을
`policy-authoring/2026-09-04.2`로 올렸다. **배포 변수 `POLICY_AUTHORING_MODEL_PROFILE_JSON`의
`prompt_version`을 같은 값으로 바꾸지 않으면 추출기가 생성 단계에서 fail-closed한다.**

**업로드 문서의 구조가 추출 성공률을 좌우한다.** 완결성 게이트는 청크의 모든 locator가
요구사항이거나 `non_requirement_locators`로 설명되기를 요구하는데, 라이브 측정에서 모델이
빠뜨리는 unit은 예외 없이 **요구사항이 아닌 unit**이었다 — `##` 소제목과 서두의 적용범위
문단. 문서를 소제목 없이 쓰고 적용범위를 항목으로 바꾸자 그 실패가 사라졌다. 참고 문서는
`policies-local/internal-cloud-security-standard.md`(Git 제외)이며 Catalog의 자동 통제 15개를
모두 덮는다. 남은 실패는 확률적이다 — 20 unit·4 청크 문서에서 실행 성공률 2/8이며, 청크
하나가 실패하면 문서 전체가 실패하는 현재 설계 때문이다.

**Catalog 경계는 추출기가 아니라 `build_candidate`가 판정한다** (2026-09-04). 두 곳이 같은
검사를 서로 다른 무게로 하고 있었다 — `build_candidate`는 위반마다 코드를 붙여 후보 하나를
거절하는데(`UNKNOWN_CONTROL_KEY`, `UNSUPPORTED_RESOURCE_TYPE`, `UNSUPPORTED_EVALUATION_TYPE`,
`EVIDENCE_CAPABILITY_NOT_AVAILABLE`), 추출기는 같은 것을 예외로 올려 청크 전체를 죽였다.
카탈로그에 없는 통제를 지목한 요구사항은 **평가할 수 없는 요구사항**이지 믿을 수 없는 응답이
아니다. 추출기는 응답의 모양과 locator 출처만 판정하고, Catalog 경계는 그것을 코드로 표현할
수 있는 곳에 맡긴다. 경계 자체는 그대로다 — 그런 후보는 승인 가능한 Rule이 되지 못한다.

**청크는 최대 3회까지 물어본다** (`MAX_CHUNK_ATTEMPTS`). 완결성 게이트는 청크마다 걸리고
문서는 청크가 하나라도 실패하면 실패하므로, 청크 실패 확률이 조금만 있어도 긴 문서는 거의
확실히 실패한다. locator 회계가 틀린 경우(누락·중복)에는 **무엇이 틀렸는지 이름으로** 되돌려
묻는다(`unclassified_locators`, `double_classified_locators`) — 판정 결과의 인용이지 유도가
아니다. 평가 결과를 내놓으려 한 응답(`PoisonedResponseError`)은 재시도하지 않는다. 그것은
확률적 실수가 아니라 경계 위반이고, 다시 물어 통과시키면 그 사실이 사라진다. 버려진 시도는
모두 로그에 남는다.

라이브 측정(2026-09-04). 20 unit 참고 문서: 실행 성공률 2/8 → **5/5**. 고객이 올린 193 unit
문서(39 청크): 실패 청크 18/39 → **6/39**이며 문서 전체로는 아직 실패한다. 남은 6건은 SECTION
소제목 누락 3건, 응답 모양 오류 2건, chunk 밖 locator 인용 1건이다.

추출 Worker는 각 청크의 모든 정규화 unit locator를 반드시 설명하게 한다. locator는 하나 이상의
Requirement가 인용하거나 `non_requirement_locators`에서 heading/문맥으로 명시돼야 하며, 두 집합의
중복·누락·청크 밖 locator를 모두 거부한다. 응답 JSON, 후보 하나, 청크 하나라도 검증에 실패하면
문서 전체 실행을 실패시켜 부분 결과가 `READY`로 저장되지 않는다. 배포된 Model Profile의
`prompt_version`도 코드의 `PROMPT_VERSION`과 정확히 같아야 한다.

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

- `heading/access-control/item/5` — Markdown, DOCX. Markdown parser 1.1.0부터 빈 줄 없이 이어진
  최상위 목록 항목도 각각 별도 `item/{n}`이다(1.0.1은 tight list 전체를 unit 하나로 묶어 요구사항
  여러 개가 locator 하나를 공유했다). 들여쓴 하위 항목과 이어지는 줄은 상위 항목의 일부다.
  parser version이 바뀌었으므로 같은 원본도 새 Source version으로 정규화된다.
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

C의 Bedrock adapter는 모든 입력 locator의 분류가 완전한지 확인한 후에만 handoff를 만든다. 따라서
`READY`는 모든 unit이 후보 또는 비요구사항 문맥으로 처리됐음을 뜻하며, 일부 청크만 성공한 결과를
뜻하지 않는다.

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

**아직 결과가 없는 것은 장애가 아니다** (2026-09-04). 후보 조회는 세 가지를 구분해서 답한다.

| 상태 | 응답 |
| --- | --- |
| 요청은 있고 manifest가 아직 없음 | `200` + `status: QUEUED` |
| manifest가 있으나 완결 전 | `200` + manifest의 상태만 |
| 요청도 manifest도 없음(다른 고객의 판본 포함) | `404 NOT_FOUND` |

예전에는 첫 번째와 세 번째가 모두 `RepositoryError` → `503`이었다. `503`은 "잠시 후 다시"라는
뜻이라, 업로드 직후부터 worker가 결과를 쓸 때까지 콘솔이 내내 "요청 실패"를 표시했고 추출이 계속
실패하는 문서에서는 그 표시가 끝나지 않았다. `AuthoringRunNotFound`(=`LookupError`)를 저장소 오류와
분리해 이 셋을 갈랐다.

## Deleting a policy source version

삭제는 **판본 단위로 완결한다**. 한 판본은 `POLICY_INGESTION#{sid}#VERSION#{ver}` item 하나가
아니라, 그 item과 `POLICY_SOURCE#{sid}#VERSION#{ver}` 아래의 자식 전부(`#REQUEST`, `#AUTHORING`,
`#CANDIDATE#*`, `#UNSUPPORTED#*`, `#REJECTED#*`, 그리고 판본 자체의 `PolicySource`)와 S3의 원본·
정규화 객체로 이루어진다. 예전에는 맨 앞의 하나만 지웠고, 라이브 sandbox에서 한 source에 95개의
고아 item이 남았다. 남은 `#REQUEST`는 사라진 문서를 가리키는 추출 요청이라 더 나쁘다.

승인된 판본은 여전히 거부한다(`409`) — 승인된 Source는 게시된 Profile의 Rule을 뒷받침하므로
지우면 근거 추적이 끊어진다.

순서는 복구 가능성으로 정한다. 삭제는 ingestion record가 사라져야 관측되므로 그것이 먼저 간다.
그 뒤 자식이나 S3 정리가 실패하면 남는 것은 어떤 read 경로에도 보이지 않는 고아 데이터이고,
반대로 정렬하면 한 번의 실패가 "바이트 없는 살아 있는 문서"를 만든다. 다만 그 중간 실패는 같은
삭제를 다시 불러도 낫지 않는다(record가 이미 없어 `404`). 고아 정리는 운영 작업으로 남는다.

**Authoring Worker는 사라진 판본의 요청을 건너뛴다.** 삭제가 큐를 되돌리지는 못하므로, 이미
발행된 메시지는 문서가 없어진 뒤에 도착한다. 그때 예외를 올리면 SQS가 재시도하고 재시도해도 문서는
돌아오지 않아 결국 DLQ에 쌓인다 — 라이브 sandbox에서 실제로 그렇게 됐다. worker는 `LookupError`를
잡아 그 요청 하나만 기록과 함께 넘기고 같은 배치의 나머지는 그대로 처리한다.

**하나의 Profile은 여러 문서와 기준선을 담는다** (2026-09-04). 게시 요청은 `sources` 목록으로
여러 `(source_id, source_version)`을 받고, 선택적으로 `baseline`으로 이미 게시된 Profile
하나를 받는다. 예전 형태(`source_id`/`source_version` 한 쌍)도 그대로 받으며, 두 형태를 섞으면
거부한다. 문서 하나 제한은 처음부터 API 경계에만 있었다 — 아래 세 거부 조건은 Rule마다 그
Rule이 인용한 Source의 승인 record로 판정하므로 문서 수와 무관하다.

기준선(예: ISMS-P Registry)의 Rule에는 **고객 승인 record를 요구하지 않는다.** 고객이 올린
문서가 아니기 때문이다 — 저장소에 커밋되어 코드 리뷰를 거치고 운영자 배포가 고객 파티션에
게시한 Registry다. 승인 record를 요구하면 고객이 검토한 적 없는 문서에 대한 승인을 만들어
내야 하고, 그것은 승인 경계를 지키는 것이 아니라 흉내 내는 것이다. 대신 두 가지를 요구한다.

1. 기준선은 **이미 같은 고객 파티션에 게시된 Profile**이어야 한다. 임의의 Rule 목록은 받지
   않는다 — 그러면 승인 게이트를 우회하는 입구가 된다. 그 Profile의 Rule은 Catalog가 평가
   시점에 거는 것과 같은 검사(`entity_type == POLICY_RULE`, lifecycle `APPROVED`)를 통과해야
   한다. 두 곳의 검사가 다르면 게시는 통과하는데 평가는 실패하는 Profile이 생긴다.
2. 그 Rule이 인용하는 Source가 함께 읽혀야 한다. 원본을 이름 붙일 수 없는 Rule은 Segment에
   넣을 수 없고, Segment가 없으면 준비도를 원본별로 나눌 수 없다.

사람의 결정은 그대로 남는다. 기준선을 넣을지는 `PUBLISH_POLICY_PROFILE` 권한을 가진 사람이
게시 요청에서 명시적으로 고른다. 고를 대상은 `GET /policy-profiles`가 돌려준다.

게시된 Profile은 `segments`에 원본 구분을 기록한다. 보고 단계는 그것으로 준비도를 사내 정책과
ISMS-P로 나눈다 — **두 점수를 하나로 합치지 않는다**(`docs/CONTRACTS.md`,
`SegmentReadinessScore`). 모든 Rule의 모든 인용 Source를 이름 붙일 수 있을 때만 Segment를
만든다. 절반만 분류된 Profile은 나머지를 어느 점수에 넣을지 답할 수 없고, 그 상태로 나눈
점수는 조용히 일부를 빠뜨린 값이다.

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
