# Fixtures

Rules, profiles, Terraform, assessment, finding, and remediation fixtures for deterministic tests.

`m0/s3_resource_snapshot.json` is an intentionally non-compliant, synthetic S3 IaC snapshot.
It exercises the M0 Assessment integration path only; it is not a customer artifact or an AWS
Resource Tool response.

`rules/` holds the committed MVP Rule Registry: policy sources, per-resource rule files
(`rules.<resource>.json`), the Control mapping, and policy profiles. Rule definitions and
`SourceReference` locators are committed; policy originals are not (ADR-0004). Digests are
verified against the local originals with `scripts/policy_source_digest.py --verify`.

`terraform/` is intentionally empty. The WordPress/LAMP demo Terraform lives in a separate
customer sandbox repository, not in this platform repository, so the apply path is validated
through the real GitHub App / OIDC / approved-repository boundary (ADR-0021 §1). See
`fixtures/terraform/README.md` and `docs/M4-DEMO-IAC-REFERENCE.md`.
