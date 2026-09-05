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
Access, ACL ownership, and TLS-only transport are automatic. A bucket policy's
intended network scope, a server access log destination, and an S3 encryption
algorithm or KMS key are not determined by their Rules; encrypting an existing
EBS volume requires replacing it. Those Rules are manual. The classification
lives in `fixtures/rules/remediation.json` next to the Rules it judges.

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

**An in-force exception is evaluated first.** It expresses "this Rule is not acted
on for this resource", so there is no action type left to compute. In force means
two separate comparisons, not one: the approval must precede the moment the
Finding was evaluated (`approved_at <= finding_evaluated_at`), and the exemption
must not yet have expired when the judgement runs (`at < expires_at`). Comparing
both against a single moment cannot hold both rules, because a remediation request
normally arrives later than the assessment it acts on — a human reads the report
and chooses. Using the decision time for the approval check would let an exception
registered today retroactively suppress a Finding evaluated before anyone approved
it; using the evaluation time for the expiry check would revive an exemption that
has since lapsed. Where several in-force exceptions cover one Finding, the
resource-scoped one wins, and ties break on `exception_id` so the audit record
does not depend on repository iteration order.

**An Actual or Drift Finding needs the IaC verdict for the same Resource × Rule,
from the commit being remediated.** `RemediationTarget` therefore carries
`rule_id`, `rule_version`, the perspective, and `iac_commit_sha`, all paired with
`iac_status` as one bundle. `decide()` refuses a target whose identity differs
from the Finding's, and the Contract only accepts `IAC` as the paired
perspective: a `PASS` from another Rule or an Actual evaluation on the same
resource is not evidence that this Rule's IaC is safe, and accepting one would
make unsafe IaC a deployment target.

The commit binding closes the same hole across time rather than across identity.
A repository advancing after an assessment is normal, so a verdict produced from
one commit can be paired with a newer snapshot; `ACTUAL_SYNC` must deploy the
commit that passed `IAC`, and an unevaluated newer commit is not that commit. A
verdict whose `iac_commit_sha` differs from the commit being remediated is
therefore unknown and produces `IAC_VERDICT_COMMIT_MISMATCH`, whether it says
`PASS` or `FAIL`: a stale `FAIL` may already be fixed, and synthesizing a patch
on top of it can revert the fix a human wrote. An `IAC` Finding does not re-read
its own perspective's verdict, so the comparison does not apply to it; its patch
is bound to the snapshot D was asked for.

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

A must supply three request-shaped values it already holds: the commit being
remediated, the moment the Finding was evaluated, and the moment of judgement.
`Finding` gains no timestamp — it stays C's immutable projection — so the
evaluation moment travels as an argument rather than inside the Contract. Callers
that cannot name the commit or the evaluation moment cannot obtain an actionable
decision, which is the intended direction of failure.

The boundary judges; it does not persist. A binds the decision to the Job state
and audit record, and D still validates that a generated patch matches the
Finding and snapshot it was asked for.

## 보완 2026-09-05 — 허용 범위는 Registry마다 커밋되고, 판정은 전부를 합쳐 본다

"The classification lives in `fixtures/rules/remediation.json` next to the Rules it judges"는
Registry가 하나였을 때의 문장이다. ISMS-P 기준선(ADR-0026)이 두 번째 Registry가 되면서 두 가지가
정해졌다.

- 허용 범위는 **각 Registry 안에서** 그 Registry의 Rule에 대해 커밋된다(loader가 다른 Registry의
  Rule을 가리키는 항목을 거부하므로). 조치 판정은 runtime이 게시하는 모든 Registry의 범위를 합쳐
  본다(`load_remediation_policy`). 한쪽만 읽으면 다른 쪽 Rule이 전부 "등록되지 않음"이 된다 —
  라이브에서 기준선 Rule 15개가 정확히 그렇게 `RULE_NOT_IN_SCOPE`로 닫혔다.
- 판단의 단위는 Rule version이지만 그 **근거**는 통제다. 같은 통제를 구현하는 Rule이 Registry마다
  다른 허용 범위를 가지면 같은 변경이 한쪽에서는 자동, 다른 쪽에서는 수동이 된다. 그래서 기준선
  Rule은 같은 통제의 legacy 판단을 물려받고(생성 스크립트가 강제), 새 판단은 legacy 쪽에서 내린다.
