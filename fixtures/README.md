# Fixtures

Rules, profiles, Terraform, assessment, finding, and remediation fixtures for deterministic tests.

`m0/s3_resource_snapshot.json` is an intentionally non-compliant, synthetic S3 IaC snapshot.
It exercises the M0 Assessment integration path only; it is not a customer artifact or an AWS
Resource Tool response.

`rules/` holds the committed MVP Rule Registry: policy sources, per-resource rule files
(`rules.<resource>.json`), the Control mapping, and policy profiles. Rule definitions and
`SourceReference` locators are committed; policy originals are not (ADR-0004). Digests are
verified against the local originals with `scripts/policy_source_digest.py --verify`.
