# ADR-0017: B decides what may be remediated automatically, and what a human must judge

## Context

`docs/PRD.md` Assessment stages and `docs/DESIGN.md` State and execution already
state the three outcomes of a Finding: patch the IaC, sync a drifted Actual
against an already-safe IaC, or leave it as `MANUAL_REVIEW`. ADR-0016 assigned
Finding production to C and noted that M2 remediation reads Drift to choose
between the two actionable outcomes.

Nothing decided which Findings are eligible in the first place. D's patch
generator takes a `finding_id` and produces a patch; no boundary asks whether
that Finding should be patched at all, whether the customer has an approved
exemption, or whether the rule even has a safe automatic fix. Without that
boundary, "safe automated remediation" is a property of whichever generator
happens to run.

Remediation eligibility is a governance judgement about a policy Rule, not a
property of a Terraform generator, so it belongs to B alongside the Rule Registry
and the approval boundary (ADR-0015).

## Decision

B owns a remediation policy boundary that judges one Finding and returns a value,
never performs an action. The judgement is one of `TERRAFORM_PATCH`,
`ACTUAL_SYNC`, `MANUAL_REVIEW`, or `SUPPRESSED`. Refusals carry an enumerated
`ManualReviewCode`, continuing the discipline of ADR-0015: a rejection reason is
an enumerated value, never free text that could quote a policy original.

**Eligibility is per Rule version and is committed, not inferred.** A Rule is
`AUTOMATIC` only when (1) the Rule alone determines a unique compliant target
state and (2) reaching that state does not require resource replacement or data
loss. Everything else is `MANUAL_ONLY`. Under this criterion S3 Block Public
Access, ACL ownership, default encryption, and TLS-only transport are automatic;
a bucket policy's intended network scope and a server access log destination are
not determined by the Rule, and encrypting an existing EBS volume requires
replacing it, so those are manual. The classification lives in
`fixtures/rules/remediation.json` next to the Rules it judges.

**Eligibility governs patch synthesis, not every automatic action.** Both criteria
ask whether a safe change can be derived from the Rule alone, so `MANUAL_ONLY`
refuses `TERRAFORM_PATCH` and nothing else. `ACTUAL_SYNC` derives nothing: it
deploys a commit a human wrote and the `IAC` perspective already judged
compliant. Blocking it because the Rule cannot be auto-patched would make the
PRD's "IaC is already safe, only the Actual drifted" path unreachable for exactly
the Rules where a human has already supplied the answer the Rule does not
determine. Whether applying that commit is destructive is decided later, by the
refreshed Terraform plan and the human approval that ADR-0007 already requires.

**A Rule with no entry blocks everything, not just patches.** The absence of a
judgement is not the judgement `MANUAL_ONLY`: nothing has been said about the
Rule, so neither action is opened. Forgetting to classify a new Rule must leave
automation closed rather than open. The registry loader refuses an entry pointing
at a Rule version that does not exist, so a typo cannot silently disable
automation for the Rule it was meant to enable.

**Exceptions are customer data, approved, narrow, and expiring.** A
`RemediationException` binds to `(customer_id, rule_id, rule_version)` and
optionally one `resource_id`. It carries an enumerated reason, an approver, and a
required expiry: an exemption that never expires removes a control permanently
and leaves no moment at which anyone re-examines it. An exception does not follow
a Rule to a new version — a revised requirement no one has read must not inherit
an old approval. Exceptions are not committed to the registry; A stores them per
customer and passes them to the judgement, because their lifetime is the
customer's, not the Rule's.

**An active exception is evaluated first.** It expresses "this Rule is not acted
on for this resource", so there is no action type left to compute.

**An Actual or Drift Finding needs the IaC verdict for the same Resource × Rule.**
`PASS` means the IaC is already safe, so the current commit is synced rather than
patched. `FAIL` means the IaC must change. Any other value — including
`OUT_OF_SCOPE` and `EXECUTION_ERROR` — is unknown, not safe, and produces
`MANUAL_REVIEW`; reading an unevaluated IaC as safe would make it a deployment
target.

## Consequences

D's generator and A's remediation API call one shared judgement instead of each
encoding scope rules. `RemediationDecision` gives the report a per-Finding reason
for inaction, so a suppressed or manual Finding is visible rather than missing.

Adding a Rule now requires a remediation classification before automation applies
to it, and the committed registry test fails while any Rule is unclassified.
Revising a Rule invalidates both its classification and any exception written
against the old version, which is the intended cost of a version pin.

Expiry comparison is the first place the contracts order timestamps, so
`approved_at` and `expires_at` must carry an explicit UTC offset. Other contract
timestamps remain opaque display strings.

The boundary judges; it does not persist. A binds the decision to the Job state
and audit record, and D still validates that a generated patch matches the
Finding and snapshot it was asked for.
