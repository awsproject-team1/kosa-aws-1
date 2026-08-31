# ADR-0014: Artifact access audit and tenant runtime isolation

## Context

Policy originals, IaC/AWS snapshots, reports, patches, and plans are immutable artifacts in the
customer deployment's S3 bucket. The platform must audit artifact reads and writes without
logging artifact bodies. The original M0 Worker IAM role also allowed `s3:GetObject` and
`s3:PutObject` under every `customers/*` prefix, although the deployed M0 worker only evaluates a
packaged synthetic fixture and is not wired to an artifact store. A shared role with that wildcard
is not an IAM tenant-isolation boundary.

## Decision

The M0 CloudFormation stack records ArtifactBucket S3 object read/write data events through a
single-region CloudTrail trail. The trail uses log-file validation and selects only
`AWS::S3::Object` data events under ArtifactBucket; it does not select management events or the
audit destination bucket. CloudTrail delivers to a separate account-qualified S3 audit bucket
with SSE-S3, versioning, bucket-owner-enforced ownership, public-access blocks, TLS denial, and
retention. The audit bucket policy grants CloudTrail only the required `GetBucketAcl` and scoped
`PutObject` delivery actions. The audit bucket and trail are retained so stack deletion does not
silently discard evidence.

M0 Worker roles do not receive ArtifactBucket permission. M0 uses packaged synthetic fixtures
only. Before a worker or API runtime handles customer artifacts, the system must introduce a
customer-scoped runtime identity whose S3 permissions are constrained to one authoritative
customer prefix. The trusted job/customer context must determine that identity; a caller must not
be able to choose a customer identifier, session tag, role, or prefix. A temporary pooled role
with `customers/*` is prohibited unless Security explicitly approves a documented exception with
an expiry and compensating controls.

## Consequences

CloudTrail Data Events incur event-based charges, and audit records can contain object-key
metadata. Artifact object keys must therefore use opaque identifiers/digests and must not embed
policy originals, prompts, or full IaC content. Customer deployment requires an approved sandbox
Get/Put test that verifies a delivered CloudTrail record and log-file validation; repository CI
can validate only the declared template controls.

The M0 deployment role must be able to create/update CloudTrail and its audit destination.
Retained trails and audit buckets can continue to incur storage or logging cost after a stack
delete request, so retention/Object Lock/lifecycle and evidence access are open Security
operational decisions. The repository's CloudFormation security test verifies the declared
controls on every relevant PR, but does not replace customer-account delivery verification.
