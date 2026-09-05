# Fixtures

Rules, profiles, Terraform, assessment, finding, and remediation fixtures for deterministic tests.

`m0/s3_resource_snapshot.json` is an intentionally non-compliant, synthetic S3 IaC snapshot.
It exercises the M0 Assessment integration path only; it is not a customer artifact or an AWS
Resource Tool response.

`rules/` holds the committed MVP Rule Registry: policy sources, per-resource rule files
(`rules.<resource>.json`), the Control mapping, and policy profiles. Rule definitions and
`SourceReference` locators are committed; policy originals are not (ADR-0004). Digests are
verified against the local originals with `scripts/policy_source_digest.py --verify`.

**이 Registry는 Runtime의 정본이 아니다.** 살아 있는 M1 Assessment는 고객 partition의 승인된
Rule만 읽는다(ADR-0023). `rules/`는 두 가지 용도로만 남는다.

1. `DynamoDbPolicyCatalogBootstrap`이 고객 Catalog를 최초로 채우는 운영 seed
2. 테스트 입력

여기 있는 Rule은 전부 **legacy Rule**이다 — `evaluation_type`이 없고, 따라서 지금까지처럼
IAC + AWS_ACTUAL + DRIFT 세 관점으로 평가된다. authoring이 만드는 Rule은 실행 의미를 갖고
`CUST-` 접두사를 쓰므로 두 종류는 ID로도 구별된다. 여기에 새 Rule을 더하는 것은 고객 승인
경계를 우회하는 방법이 아니다 — bootstrap으로 심은 Rule도 `lifecycle = APPROVED` item으로
저장되며, 그것이 무엇을 뜻하는지는 운영자가 책임진다.

`baselines/isms-p-2023/`는 **ISMS-P 인증기준 기준선 Registry**다(ADR-0026). 같은 네 파일 모양을
`load_rule_registry`가 읽고 같은 bootstrap이 게시하지만(`publish_policy_catalog.py --registry
isms-p-2023`), 내용은 다르다 — 인증기준 101개 항목마다 MANUAL Rule 하나(`ISMSP-x.y.z`)와
Control 하나(`ISMS-P-x.y.z`), Catalog의 자동 판정 통제마다 그 통제가 근거가 되는 항목들을
인용하는 Rule 하나(`ISMSP-<CONTROL_KEY>`, 15개 · 11개 항목 · ADR-0026 §5), 그리고 그 전부를
`ISMS_P` Segment로 담은 `profile-isms-p-baseline@v2`. 고객은 ISMS-P를 업로드하지 않고 Profile
게시 때 이 기준선을 고른다. Profile 판본 item은 불변이고 bootstrap은 current pointer만 옮긴다 —
내용이 바뀌면 version을 올린다. `remediation.json`은 자동 판정 Rule 15개의 자동 patch 허용 범위이며
같은 통제를 구현하는 legacy Rule의 판단을 그대로 물려받는다(ADR-0026 §6); API의 조치 판정은 두
Registry의 범위를 합쳐 본다(`load_remediation_policy`). 손으로 편집하지 않는다: `scripts/build_isms_p_baseline.py`가 로컬 원문에서 결정적으로
생성하며 `--check`가 커밋본과 대조한다. `sources.json`의 `isms-p-2023` 항목은 `rules/`의 것과
바이트가 같아야 한다 — 다르면 두 번째 bootstrap이 불변 key 충돌로 fail-closed한다.

`terraform/` is intentionally empty. The WordPress/LAMP demo Terraform lives in a separate
customer sandbox repository, not in this platform repository, so the apply path is validated
through the real GitHub App / OIDC / approved-repository boundary (ADR-0021 §1). See
`fixtures/terraform/README.md` and `docs/M4-DEMO-IAC-REFERENCE.md`.
