# Customer Policy Ingestion

> Status: Planned — current M1 Rule Registry is a development seed, not a customer upload path.
>
> Delivery gate: this boundary must be implemented and integration-tested before the service claims
> that a customer can evaluate against an uploaded internal policy.
>
> Decision record: `docs/decisions/ADR-0015-customer-policy-ingestion.md`

## Goal

사용자가 사내 정책 원문을 업로드하면 고객 Scope 안에서 원본을 보존하고, 파일 형식별 추출 결과를
공통 문서 구조로 정규화한 뒤, 검토·승인된 Control/Rule/Profile만 Policy Context에 제공한다.
현재 `policies-local/`과 `fixtures/rules/`는 이 흐름을 개발하기 위한 seed 자료이며 운영 입력이 아니다.

## Important boundary

파일을 S3에 바이트로 저장하는 것과 그 내용을 정책으로 해석하는 것은 서로 다른 기능이다.
서비스는 "모든 형식을 읽는다"고 가정하지 않는다. 지원 형식은 버전 관리되는 allow-list와 Parser
Capability로 명시하고, 지원하지 않는 형식·암호화 문서·손상 문서·텍스트를 추출할 수 없는 문서는
`REVIEW_REQUIRED` 또는 `FAILED`로 처리한다. 원본 업로드 성공만으로 Policy Source를 승인하거나
Assessment에 사용해서는 안 된다.

## Target workflow

```text
Authenticated upload request
→ customer-scoped S3 original (immutable, checksum verified)
→ file signature/MIME/size/malware validation
→ format-specific parser
→ normalized Policy Document artifact
→ Control/Rule candidates with stable locator + content hash
→ human review and approval
→ version-pinned Policy Source/Rule/Profile publication
→ Policy Context → Assessment
```

원본과 정규화 결과는 Artifact로 분리한다. 평가기는 업로드 원본 전체를 임의로 읽지 않고 승인된
Profile의 Rule과 필요한 정규화 구간만 받는다. Parser, 정규화 Schema, Rule 또는 승인된 Policy
Document가 변경되면 Golden Dataset 품질 Gate를 다시 실행한다.

## Format policy

초기 구현 대상과 후속 대상을 Task 시작 시 확정하고 Contract Test로 고정한다.

| Capability | Initial target | Notes |
| --- | --- | --- |
| Plain text | UTF-8 `text/plain`, Markdown, CSV | 문자 인코딩과 delimiter 오류를 명시적으로 처리 |
| Office | DOCX, XLSX | 문단·표·sheet locator를 안정적으로 생성 |
| PDF | Text-based PDF | page 기반 locator; 추출 텍스트가 없으면 OCR 대상으로 분류 |
| Korean office | HWP/HWPX | Parser와 라이선스·보안 검토 후 별도 활성화 |
| Scanned/image documents | OCR pipeline | 초기 Parser와 분리하고 신뢰도 및 수동 검토를 요구 |

확장자만 신뢰하지 않는다. 선언한 media type, 파일 signature로 탐지한 media type, Parser가 실제로
지원하는 형식을 함께 검증한다. 지원 형식 목록은 Backend와 Frontend가 같은 Contract를 사용해야 한다.

## Normalized document contract

형식별 Parser는 최소한 다음 정보를 갖는 공통 결과를 생성해야 한다.

- `source_id`, `source_version`, 원본 `artifact_id`와 `content_sha256`
- 원본 파일명, 선언/탐지 media type, byte size
- `parser_id`, `parser_version`, 처리 시각과 처리 상태
- 정규화 Artifact ID/hash와 추출 경고
- 문서의 section/paragraph/table/sheet/page 단위
- 각 단위의 stable `locator`, 정규화 text hash, 원본 위치

권장 처리 상태는 `UPLOADED`, `VALIDATING`, `PARSING`, `REVIEW_REQUIRED`, `READY`, `FAILED`,
`SUPERSEDED`다. `READY`이면서 사람이 승인한 정확한 Source version만 Rule과 Profile이 참조할 수 있다.

Locator는 파일 형식과 무관하게 추적 가능해야 한다. 예시는 다음과 같다.

- `page/12/paragraph/3`
- `sheet/Security/row/27`
- `heading/access-control/item/5`
- `table/2/row/8`

## Security and tenant isolation

- Backend는 verified JWT에서 `customer_id`를 결정하며 Client가 S3 key나 tenant key를 지정하지 않는다.
- 원본은 `customers/{customer_id}/...` 경계에 암호화해 저장하고 공개 URL을 반환하지 않는다.
- 업로드 크기·개수·압축 해제 한도, zip bomb, 악성 파일, 암호화 파일을 fail-closed로 검증한다.
- Parser는 격리된 비동기 실행 환경에서 최소 권한으로 동작하고 원문·추출문을 로그에 남기지 않는다.
- Source version은 immutable하며 새 업로드는 기존 버전을 덮어쓰지 않는다.
- Rule 후보가 자동 생성돼도 사람 승인 전에는 Policy Profile이나 Assessment에 들어가지 않는다.

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
4. 추출된 Control/Rule 후보 검토 및 승인
5. 승인된 Rule version으로 Policy Profile 생성 또는 갱신

업로드 요청은 `customer_id`, bucket, object key, 처리 상태를 받을 수 없다. Backend가 이를 생성한다.

## Acceptance criteria

- [ ] 지원 형식 allow-list와 파일 signature 검증이 Contract와 테스트로 고정된다.
- [ ] 각 지원 형식이 동일한 Normalized Policy Document Contract를 생성한다.
- [ ] 고객 A가 고객 B의 원문, 정규화 Artifact, Source/Rule/Profile을 조회할 수 없다.
- [ ] 암호화·손상·미지원·텍스트 없는 문서가 명확한 상태와 오류로 종료된다.
- [ ] Source version, Parser version, locator, 원문/정규화 hash가 Evidence까지 추적된다.
- [ ] 사람 승인 전 Rule은 Profile 및 Assessment Context에 들어가지 않는다.
- [ ] 업로드 → 정규화 → 승인 → Profile → Assessment 통합 테스트가 통과한다.
- [ ] 정책 원문이나 추출 텍스트가 Git diff, Queue payload, 운영 로그에 노출되지 않는다.
- [ ] 구현 PR에서 `docs/architecture/C4-CONTAINER.md`에 업로드/Parser/정규화 Artifact 경로를
      반영한다. 계획 단계인 지금은 `docs/DESIGN.md` flow만 갱신하고 C4는 의도적으로 미룬다.

