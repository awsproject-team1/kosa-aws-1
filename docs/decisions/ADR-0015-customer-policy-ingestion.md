# ADR-0015: Customer policy ingestion and approval boundary

## Context

ADR-0004는 승인된 정책 지식을 AI 평가에 전달하는 방법(RAG 없이 구조화된 Source/Rule/Control과
Source Reference)을 정했지만, 그 Source가 **어떻게 시스템에 들어오는지**는 정하지 않았다.
현재 `policies-local/`과 `fixtures/rules/`는 개발자가 로컬 원문에서 직접 도출해 커밋한 seed이며,
고객이 자신의 사내 정책 문서를 올리는 경로는 없다.

제품 범위는 고객이 사내 정책으로 평가받는 것이므로 업로드 경로가 필요하다. 동시에 문서 형식은
통제되지 않는다 — DOCX, XLSX, PDF, HWP/HWPX, 스캔 이미지가 섞여 들어온다. 파일을 S3에 저장하는
것과 그 내용을 정책으로 해석하는 것은 난이도가 전혀 다른 문제인데, 이를 구분하지 않으면 "업로드에
성공했으니 지원한다"는 잘못된 기대가 생기고, 추출 실패나 저품질 OCR 결과가 그대로 평가 근거로
쓰일 수 있다.

## Decision

고객 사내 정책은 정적 Registry 파일로 운영하지 않는다. 고객별 immutable 원본 업로드 후 파일
signature/MIME/크기/보안 검증, 형식별 Parser, 공통 Policy Document 정규화, Control/Rule 검토와
사람 승인을 거쳐 version-pinned Profile에 게시한다.

- 업로드 성공과 해석·승인은 별개 상태다. 원본 저장만으로 Policy Source를 승인하거나 Assessment에
  사용하지 않는다.
- 지원 형식은 버전 관리되는 allow-list와 Parser Capability로 명시한다. 지원하지 않는 형식,
  암호화·손상 문서, 텍스트를 추출할 수 없는 문서는 `REVIEW_REQUIRED` 또는 `FAILED`로 종료한다.
  "모든 형식을 읽는다"고 가정하지 않는다.
- 형식은 구현 난이도가 아니라 선행 조건으로 나눈다. 서드파티 런타임 의존성이 필요한 형식(PDF)은
  형식 지원 결정이 아니라 Lambda 배포 구조 결정에 묶이므로 초기 대상에서 제외한다.
- 형식별 Parser는 형식과 무관한 stable locator를 갖는 공통 Normalized Policy Document를 생성한다.
  Evidence 추적성은 이 locator와 content hash 위에서 유지된다 (ADR-0004의 추적성 요구 계승).
- 사람이 승인한 정확한 Source version에서 생성된 Rule만 Policy Profile이 참조할 수 있다.
- `policies-local/`과 `fixtures/rules/`는 이 경계를 개발·검증하기 위한 seed로만 유지한다.

세부 workflow, 형식 정책, 정규화 Contract, 보안 기준, 역할 분담과 인수 조건은
`docs/POLICY_INGESTION.md`를 정본으로 한다.

## Consequences

Parser, 정규화 Schema, Source/Rule version 또는 Policy Document가 바뀌면 locator/hash 추적성과
Golden Dataset 품질 Gate를 다시 검증한다. 지원되지 않거나 텍스트 추출 신뢰도가 낮은 문서는
`REVIEW_REQUIRED`로 보내며 사람 승인 전에는 Assessment Context에 포함하지 않는다.

업로드·파싱은 A의 tenant-scoped Storage/API와 비동기 처리, B의 형식 정책·정규화 Schema·승인
조건, C의 AI 추출 품질 Gate를 모두 요구한다. 이 경계가 구현되고 통합 테스트를 통과하기 전에는
사용자 업로드 정책 지원을 제품 기능으로 표시하지 않는다.

Rule 후보를 AI가 생성하더라도 사람 승인 없이는 Profile에 들어갈 수 없으므로, 승인 UI와 감사
기록이 이 기능의 필수 구성 요소가 된다.

## Open decision (when applicable)

- Owner: B (형식 정책·정규화 Schema), A (업로드/Storage 경계)
- Needed by: 고객 정책 업로드 구현 Task 시작 전
- Blocks: Parser Adapter 구현 범위, 정규화 Contract 필드, 지원 형식 allow-list
- Proposed options: (1) TXT/MD/CSV + DOCX/XLSX + text PDF를 초기 대상으로 하고 HWP/HWPX·OCR을
  후속 트랙으로 분리 (2) HWP/HWPX를 초기 대상에 포함 (3) PDF도 후속 트랙으로 분리
- Final record (2026-08-31): **옵션 3.** 초기 대상은 Markdown/XLSX(검증됨)와 TXT/CSV/DOCX
  (의존성 0)이며, **PDF는 별도 트랙**이다.

  근거: 현재 배포는 서드파티 런타임 의존성이 없는 ZIP Lambda다. Markdown·XLSX·DOCX는 stdlib
  (`zipfile` + `xml.etree`)만으로 처리되고 XLSX 추출기 원형이 이미 저장소에 있다. PDF만 유일하게
  라이브러리를 요구해 Layer 또는 컨테이너 배포 결정을 선행으로 만든다. 그 결정은 A의 인프라
  범위이고 현재 로드맵에 없으므로, 형식 지원 범위가 배포 구조 결정을 기다리게 두지 않는다.
  DOCX는 XLSX와 같은 OOXML zip+XML 기법을 재사용하므로 추가 비용이 사실상 없다.
