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

`terraform/` is intentionally empty. The WordPress/LAMP demo Terraform lives in a separate
customer sandbox repository, not in this platform repository, so the apply path is validated
through the real GitHub App / OIDC / approved-repository boundary (ADR-0021 §1). See
`fixtures/terraform/README.md` and `docs/M4-DEMO-IAC-REFERENCE.md`.
